from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from time import perf_counter
from typing import Any

from app.artifacts.store import RunArtifacts
from app.benchmark.fixture_planning import FixturePlanningService
from app.benchmark.runner import run as run_fixture_gate
from app.contracts.models import (
    ActionCapability,
    DemoPlan,
    DemoTrace,
    DiscoveryBudget,
    PresentationPlan,
    ProductKnowledge,
)
from app.narration.audio import audio_duration_seconds
from app.narration.script import (
    captions_from_duration,
    recommended_caption_duration,
    script_from_trace,
)
from app.narration.service import NarrationService, SpeechProvider
from app.observability.logging import (
    bind_run_context,
    clear_run_context,
    get_logger,
    safe_url,
)
from app.orchestration.lifecycle import RunStage
from app.persistence.repository import RunRepository
from app.planning.forms import infer_form_schema
from app.presentation.director import build_presentation_plan
from app.providers.errors import ProviderError
from app.quality.coverage import inspect_coverage
from app.quality.delivery import delivery_report
from app.quality.presentation import attach_presentation_qa, inspect_presentation
from app.quality.synchronization import inspect_synchronization, secure_transition_intervals
from app.quality.video import inspect_video
from app.services.generation import UrlGenerationService
from app.services.knowledge import product_knowledge_from_context
from app.services.stage_contracts import stage_lifecycle
from app.video.poster import write_video_poster
from app.video.render import render_remotion

logger = get_logger("app.jobs")


def _form_schemas_for_context(context: Any) -> list[Any]:
    """Prefer active-form capability evidence over broad page control dumps."""
    schemas = []
    for raw_capability in getattr(context, "capabilities", []):
        try:
            capability = ActionCapability.model_validate(raw_capability)
        except (TypeError, ValueError):
            continue
        if capability.form_schema is not None:
            schemas.append(capability.form_schema)
    return schemas or [infer_form_schema(getattr(context, "elements", []), context.url)]


def _product_knowledge_for_context(
    context: Any, *, project_id: str | None = None
) -> ProductKnowledge:
    """Compatibility alias for the canonical knowledge materializer."""
    return product_knowledge_from_context(context, project_id=project_id)


class DemoJobService:
    """Resumable stage owner. Fixture gates are an explicit local support envelope."""

    def __init__(
        self,
        repository: RunRepository,
        artifact_root: Path,
        planner: FixturePlanningService | None = None,
        speech_provider: SpeechProvider | None = None,
        url_generator: UrlGenerationService | None = None,
        artifact_storage: object | None = None,
    ):
        self.repository, self.artifact_root = repository, artifact_root
        self.planner = planner or FixturePlanningService()
        self.speech_provider = speech_provider
        self.url_generator = url_generator
        self.artifact_storage = artifact_storage

    def _publish_delivery(self, run_id: str, artifacts: RunArtifacts) -> None:
        """Publish only accepted evidence; failed attempts stay retry-local."""
        if self.artifact_storage is None:
            return
        publish = getattr(self.artifact_storage, "publish_run", None)
        if not callable(publish):
            raise TypeError("artifact storage must provide publish_run(run_root)")
        manifest = publish(artifacts.root)
        location = str(manifest.get("manifest_location", ""))
        if location:
            self.repository.save_location(run_id, "artifact_manifest", location)

    def _checkpoint_stage(self, run_id: str, stage: RunStage) -> None:
        """Mirror lifecycle transitions into durable, independently inspectable checkpoints."""
        mapping: dict[RunStage, tuple[tuple[str, str], ...]] = {
            RunStage.DISCOVERING: (("DISCOVERY", "RUNNING"),),
            RunStage.PLAN_READY: (("DISCOVERY", "COMPLETE"), ("PLANNING", "RUNNING")),
            RunStage.PLAN_VALIDATED: (("PLANNING", "COMPLETE"),),
            RunStage.PRODUCTION_EXECUTION: (("EXECUTION", "RUNNING"),),
            RunStage.TRACE_READY: (("EXECUTION", "COMPLETE"),),
            RunStage.PRESENTATION_PLANNED: (("NARRATION", "RUNNING"),),
            RunStage.NARRATION_READY: (("NARRATION", "COMPLETE"),),
            RunStage.RENDERING: (("NARRATION", "SKIPPED"), ("RENDER", "RUNNING")),
            RunStage.RENDERED: (("RENDER", "COMPLETE"),),
            RunStage.VIDEO_QA: (("VIDEO_QA", "RUNNING"),),
            RunStage.QA_PASSED: (("VIDEO_QA", "COMPLETE"),),
        }
        for checkpoint, status in mapping.get(stage, ()):
            self.repository.update_stage_job(run_id, checkpoint, status=status)

    def _register_final_video(self, run_id: str, video_path: Path) -> None:
        """Persist the deliverable MP4 and a best-effort library poster frame."""
        self.repository.save_location(run_id, "final_video", str(video_path))
        poster = write_video_poster(video_path, video_path.parent / "poster.jpg")
        if poster is not None:
            self.repository.save_location(run_id, "poster", str(poster))

    async def _heartbeat_stage(self, run_id: str, stage: str) -> None:
        """Keep a long provider/render stage visibly alive for recovery tooling."""
        while True:
            await asyncio.sleep(15)
            try:
                self.repository.heartbeat_stage_job(run_id, stage)
            except Exception as error:  # noqa: BLE001 - heartbeat is advisory
                logger.warning(
                    "demo_job_heartbeat_failed",
                    run_id=run_id,
                    stage=stage,
                    error_type=type(error).__name__,
                )

    async def run_fixture_stage(self, run_id: str, stage: str, *, gate: int, render: bool) -> None:
        """Execute exactly one persisted fixture stage.

        Each stage reconstructs only the evidence it owns from the run artifact
        directory.  No in-memory pipeline state is required after a worker crash.
        This is the reference implementation for the URL pipeline's staged
        workers; fixtures deliberately avoid paid provider calls.
        """
        artifacts = RunArtifacts(self.artifact_root, run_id)
        run = self.repository.get_run(run_id)
        request = self.repository.get_request(run["request_id"])
        lifecycle = stage_lifecycle(stage)
        self.repository.update_run(run_id, stage=lifecycle, status="RUNNING")
        self._checkpoint_stage(run_id, lifecycle)

        if stage == "DISCOVERY":
            artifacts.write_json(
                "discovery/fixture-context.json", {"gate": gate, "objective": request["objective"]}
            )
        elif stage == "PLANNING":
            plan = self.planner.plan(request["objective"])
            self.repository.replace_json_artifact(
                "demo_plans", run_id, plan.model_dump(mode="json")
            )
            self.repository.connection.execute(
                "DELETE FROM workflow_steps WHERE run_id=?", (run_id,)
            )
            self.repository.connection.commit()
            self.repository.save_workflow_steps(
                run_id, [step.model_dump(mode="json") for step in plan.workflow_steps]
            )
            artifacts.write_json("plan.json", plan.model_dump(mode="json"))
        elif stage == "EXECUTION":
            trace = await run_fixture_gate(
                gate, self.artifact_root, render_final=False, run_id=run_id
            )
            self.repository.replace_interaction_events(
                run_id, [event.model_dump(mode="json") for event in trace.events]
            )
            presentation_path = artifacts.presentation / "presentation-plan.json"
            if not presentation_path.exists():
                presentation = build_presentation_plan(trace)
                artifacts.write_json(
                    "presentation/presentation-plan.json", presentation.model_dump(mode="json")
                )
            self.repository.replace_json_artifact(
                "presentation_plans",
                run_id,
                json.loads(presentation_path.read_text(encoding="utf-8")),
            )
        elif stage == "NARRATION":
            trace = self._load_trace(artifacts)
            script = script_from_trace(trace)
            captions = captions_from_duration(script, recommended_caption_duration(script))
            narration = None
            if self.speech_provider:
                try:
                    narration = await NarrationService().create(
                        trace, self.speech_provider, artifacts.root / "audio" / "narration.mp3"
                    )
                    script, captions = narration["script"], narration["captions"]
                except ProviderError:
                    # Caption-only output is a supported terminal narration mode.
                    narration = None
            artifacts.write_json("presentation/captions.json", captions)
            payload = {"mode": "tts" if narration else "caption_only", "script": script}
            self.repository.replace_json_artifact("narration_scripts", run_id, payload)
            artifacts.write_json("presentation/narration-script.json", payload)
            if narration:
                self.repository.save_audio_asset(
                    run_id,
                    narration["audio_path"],
                    duration_seconds=audio_duration_seconds(Path(narration["audio_path"])),
                    provider="elevenlabs",
                )
        elif stage == "RENDER":
            if not render:
                self.repository.update_stage_job(run_id, "RENDER", status="SKIPPED")
                self.repository.update_stage_job(run_id, "VIDEO_QA", status="SKIPPED")
                self.repository.update_run(run_id, stage=RunStage.COMPLETE, status="COMPLETE")
                return
            trace = self._load_trace(artifacts)
            presentation = PresentationPlan.model_validate(
                json.loads(
                    (artifacts.presentation / "presentation-plan.json").read_text(encoding="utf-8")
                )
            )
            captions = json.loads(
                (artifacts.presentation / "captions.json").read_text(encoding="utf-8")
            )
            narration_path = artifacts.root / "audio" / "narration.mp3"
            output = render_remotion(
                trace,
                presentation,
                artifacts,
                narration_path=narration_path if narration_path.exists() else None,
                captions=captions,
            )
            self._register_final_video(run_id, Path(output))
        elif stage == "VIDEO_QA":
            trace = self._load_trace(artifacts)
            plan = DemoPlan.model_validate(
                json.loads((artifacts.root / "plan.json").read_text(encoding="utf-8"))
            )
            script_payload = json.loads(
                (artifacts.presentation / "narration-script.json").read_text(encoding="utf-8")
            )
            captions = json.loads(
                (artifacts.presentation / "rendered-captions.json").read_text(encoding="utf-8")
            )
            video = inspect_video(
                artifacts.root / "final" / "demo.mp4", execution_verified=trace.outcome_verified
            )
            presentation_props = json.loads(
                (artifacts.presentation / "remotion-props.json").read_text(encoding="utf-8")
            )
            presentation_report = inspect_presentation(trace, presentation_props)
            video = attach_presentation_qa(video, presentation_report)
            synchronization = inspect_synchronization(
                trace,
                script_payload["script"],
                captions,
                narration_requested=False,
                narration_created=False,
                explained_intervals=secure_transition_intervals(presentation_props),
            )
            coverage = inspect_coverage(plan, trace)
            artifacts.write_json("qa/coverage-report.json", coverage)
            story = json.loads((artifacts.qa / "story-report.json").read_text(encoding="utf-8"))
            artifacts.write_json("qa/video-report.json", video)
            artifacts.write_json("qa/presentation-report.json", presentation_report)
            artifacts.write_json("qa/synchronization-report.json", synchronization)
            report = delivery_report(
                artifacts=artifacts.required_delivery_artifacts(),
                execution={
                    "execution_score": coverage["coverage_score"],
                    "workflow_score": coverage["coverage_score"],
                    "hard_failures": coverage["hard_failures"],
                },
                story=story,
                video=video,
                synchronization=synchronization,
            )
            artifacts.write_json("qa/delivery-report.json", report)
            self.repository.replace_json_artifact("quality_reports", run_id, report)
            if not report["deliverable"]:
                raise RuntimeError(f"Delivery QA rejected render: {report['hard_failures']}")
            self.repository.update_run(run_id, stage=RunStage.COMPLETE, status="COMPLETE")
        # Keep the durable database evidence ledger in lockstep with the
        # immutable run directory. This makes stage resume, audit, and repair
        # independent of worker memory or filesystem enumeration.
        self.repository.persist_run_documents(run_id, artifacts.root)
        self.repository.update_stage_job(run_id, stage, status="COMPLETE")

    @staticmethod
    def _load_trace(artifacts: RunArtifacts) -> DemoTrace:
        return DemoTrace.model_validate(
            json.loads((artifacts.execution / "trace.json").read_text(encoding="utf-8"))
        )

    async def run_url_stage(self, run_id: str, stage: str, *, payload: dict[str, Any]) -> None:
        """Run one durable stage and terminally classify any unhandled failure."""
        # Direct supervisors and broker workers can observe the same queued
        # stage concurrently (for example after a shell timeout).  Claim the
        # stage with the repository's compare-and-set before touching provider
        # state.  A completed/skipped stage or an already-running lease is
        # idempotently ignored; this prevents duplicate Browserbase sessions
        # and concurrent Remotion renders writing into one run directory.
        self.repository.ensure_stage_jobs(run_id)
        current = self.repository.stage_job(run_id, stage)
        if current["status"] in {"COMPLETE", "SKIPPED", "RUNNING"}:
            return
        if current["status"] == "FAILED":
            # A failed checkpoint is an explicit repair boundary.  Silently
            # treating it as a no-op lets a supervisor mark the whole run
            # complete while an earlier stage is still failed.  Callers must
            # create a targeted retry (which restores the run lease and queues
            # this boundary) before executing it again.
            raise RuntimeError(
                f"stage {stage} is FAILED; create a targeted retry from this checkpoint"
            )
        if self.repository.claim_stage_job(run_id, stage) is None:
            return
        heartbeat = asyncio.create_task(self._heartbeat_stage(run_id, stage))
        try:
            await self._run_url_stage_impl(run_id, stage, payload=payload)
        except Exception as error:
            # A direct CLI/supervised invocation does not have the broker's
            # exception wrapper. Without this checkpoint, validation failures
            # leave a stage RUNNING forever and invite an unsafe duplicate
            # resume. Persist a non-secret, owning-layer error code instead.
            error_code = f"{stage}_{type(error).__name__.upper()}"
            self.repository.fail_active_stage_jobs(run_id, error_code)
            self.repository.update_run(
                run_id,
                stage=stage,
                status="FAILED",
                error_code=error_code,
            )
            logger.error("demo_job_stage_failed", stage=stage, error_type=type(error).__name__)
            raise
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _run_url_stage_impl(
        self, run_id: str, stage: str, *, payload: dict[str, Any]
    ) -> None:
        """Execute one URL-generation stage from persisted artifacts only."""
        if self.url_generator is None:
            raise RuntimeError("OpenRouter structured planning provider is not configured")
        artifacts = RunArtifacts(self.artifact_root, run_id)
        run = self.repository.get_run(run_id)
        request = self.repository.get_request(run["request_id"])
        presentation_options = payload.get("presentation")
        if isinstance(presentation_options, dict):
            # Persist preferences before any provider work so a resumed render
            # cannot silently fall back to UI defaults.
            artifacts.write_json("presentation/options.json", presentation_options)
        lifecycle = stage_lifecycle(stage)
        # ``run_url_stage`` is shared by broker workers and supervised direct
        # runs.  Do not rely on API enqueueing to have created its ledger.
        self.repository.ensure_stage_jobs(run_id)
        self.repository.update_stage_job(run_id, stage, status="RUNNING")
        self.repository.update_run(run_id, stage=lifecycle, status="RUNNING")
        logger.info(
            "demo_job_stage_started",
            stage=stage,
            cloud_discovery=bool(payload.get("cloud_discovery", False)),
            cloud_production=bool(payload.get("cloud_production", False)),
        )
        if stage == "DISCOVERY":
            cached = self.repository.fresh_knowledge(request["url"])
            context = await self.url_generator.discover_stage(
                run_id=run_id,
                url=request["url"],
                objective=request["objective"],
                artifact_root=self.artifact_root,
                budget=DiscoveryBudget(max_pages=int(payload["max_pages"])),
                cloud_discovery=bool(payload["cloud_discovery"]),
                known_routes=(cached or {}).get("evidence", {}).get("relevant_routes", []),
                known_actions=(cached or {}).get("evidence", {}).get("successful_actions", []),
                known_product_fingerprint=(cached or {})
                .get("evidence", {})
                .get("product_fingerprint"),
                # Cloud discovery uses Stagehand automatically whenever its
                # provider is configured. It is not a caller-controlled mode.
                explore_visible_routes=True,
                credential_reference=payload.get("credential_reference"),
                allow_isolated_record_creation=bool(
                    payload.get("allow_isolated_record_creation", False)
                ),
            )
            knowledge = _product_knowledge_for_context(
                context, project_id=request.get("project_id")
            )
            # Keep a few legacy top-level aliases so existing cache readers
            # remain compatible, while the typed ProductKnowledge payload is
            # now the canonical reusable snapshot.
            knowledge_payload = {
                **knowledge.model_dump(mode="json"),
                "relevant_routes": getattr(context, "relevant_routes", []),
                "successful_actions": getattr(context, "successful_action_hints", []),
            }
            # Keep the typed, content-fingerprinted snapshot beside the
            # database row.  The DB is the query/index surface; the run
            # artifact is the immutable evidence handed to a retry, audit, or
            # operator.  Without this checkpoint a successful discovery could
            # be reused from SQLite while the run itself had no inspectable
            # ProductKnowledge artifact.
            artifacts.write_json("discovery/product-knowledge.json", knowledge_payload)
            artifacts.write_json(
                "discovery/capability-resolutions.json",
                [
                    item.model_dump(mode="json")
                    for item in getattr(context, "capability_resolutions", [])
                ],
            )
            self.repository.upsert_knowledge(request["url"], knowledge_payload, context.confidence)
            self.repository.upsert_page_knowledge(
                request["url"],
                [page.model_dump(mode="json") for page in context.page_knowledge],
                confidence=context.confidence,
            )
            # Form persistence must retain the same scoped schemas that a
            # later rehearsal/production plan can safely execute. A broad
            # whole-page DOM fallback is useful for an older discovery record,
            # but it can include anonymous design-system controls and must not
            # displace a freshly observed modal/form boundary.
            for form_schema in _form_schemas_for_context(context):
                self.repository.save_form_schema(run_id, form_schema.model_dump(mode="json"))
            session_path = artifacts.root / "discovery" / "browserbase-session.json"
            if session_path.exists():
                session = json.loads(session_path.read_text(encoding="utf-8"))
                self.repository.save_browser_session(
                    run_id, session["provider"], session["session_id"], "CLOSED"
                )
        elif stage == "PLANNING":
            if bool(payload.get("allow_isolated_record_creation", False)):
                # Rehearsal owns one authorised mutation in a fresh,
                # non-recorded context. Planning subsequently reloads its
                # persisted outcome witness; production never improvises it.
                await self.url_generator.rehearsal_stage(
                    run_id=run_id,
                    url=request["url"],
                    objective=request["objective"],
                    artifact_root=self.artifact_root,
                    credential_reference=payload.get("credential_reference"),
                    cloud_rehearsal=bool(payload.get("cloud_discovery", False)),
                    allow_isolated_record_creation=True,
                )
            plan = await self.url_generator.plan_stage(
                run_id=run_id,
                objective=request["objective"],
                artifact_root=self.artifact_root,
                allow_external_side_effects=(
                    bool(payload["allow_external_side_effects"])
                    or bool(payload.get("allow_isolated_record_creation", False))
                ),
                audience=str(payload.get("audience", "product prospect")),
                target_duration_seconds=int(payload.get("target_duration_seconds", 120)),
            )
            self.repository.replace_json_artifact(
                "demo_plans", run_id, plan.model_dump(mode="json")
            )
            self.repository.connection.execute(
                "DELETE FROM workflow_steps WHERE run_id=?", (run_id,)
            )
            self.repository.connection.commit()
            self.repository.save_workflow_steps(
                run_id, [item.model_dump(mode="json") for item in plan.workflow_steps]
            )
            self.repository.save_synthetic_dataset(run_id, plan.synthetic_data_plan)
        elif stage == "EXECUTION":
            trace = await self.url_generator.execute_stage(
                run_id=run_id,
                url=request["url"],
                objective=request["objective"],
                artifact_root=self.artifact_root,
                credential_reference=payload.get("credential_reference"),
                cloud_production=bool(
                    payload.get("cloud_production", payload.get("cloud_discovery", False))
                ),
            )
            self.repository.replace_interaction_events(
                run_id, [item.model_dump(mode="json") for item in trace.events]
            )
            self.repository.record_successful_actions(
                request["url"], [item.model_dump(mode="json") for item in trace.events]
            )
            production_session = artifacts.execution / "browserbase-session.json"
            if production_session.exists():
                session = json.loads(production_session.read_text(encoding="utf-8"))
                self.repository.save_browser_session(
                    run_id, session["provider"], session["session_id"], "CLOSED"
                )
            self.repository.replace_json_artifact(
                "presentation_plans",
                run_id,
                json.loads(
                    (artifacts.presentation / "presentation-plan.json").read_text(encoding="utf-8")
                ),
            )
            self._checkpoint_stage(run_id, RunStage.TRACE_READY)
        elif stage == "NARRATION":
            # A narration/editorial repair must rebuild the storyboard from the
            # persisted evidence. Reusing a previously rejected storyboard
            # would make a targeted retry deterministic but permanently stale.
            narration = await self.url_generator.narration_stage(
                run_id=run_id,
                artifact_root=self.artifact_root,
                refresh_editorial=bool(payload.get("refresh_editorial", False)),
                include_audio=bool(payload.get("presentation", {}).get("include_audio", True)),
            )
            self.repository.replace_json_artifact(
                "narration_scripts",
                run_id,
                {
                    "schema_version": narration.get("schema_version", 1),
                    "mode": narration["mode"],
                    "timing_owner": narration.get("timing_owner", "scene"),
                    "audience": narration.get(
                        "audience", request.get("audience", "product prospect")
                    ),
                    "audience_profile": narration.get("audience_profile", {}),
                    "script": narration["script"],
                },
            )
            if narration["audio_path"]:
                self.repository.save_audio_asset(
                    run_id,
                    narration["audio_path"],
                    duration_seconds=audio_duration_seconds(Path(narration["audio_path"])),
                    provider="elevenlabs",
                )
            self._checkpoint_stage(run_id, RunStage.NARRATION_READY)
        elif stage == "RENDER":
            if not bool(payload["render"]):
                self.repository.update_stage_job(run_id, "RENDER", status="SKIPPED")
                self.repository.update_stage_job(run_id, "VIDEO_QA", status="SKIPPED")
                self.repository.update_run(run_id, stage=RunStage.COMPLETE, status="COMPLETE")
                return
            output = self.url_generator.render_stage(
                run_id=run_id, artifact_root=self.artifact_root
            )
            self._register_final_video(run_id, Path(output))
            self.repository.save_video_render(
                run_id,
                str(output),
                status="COMPLETE",
                metadata=json.loads(
                    (artifacts.root / "render" / "status.json").read_text(encoding="utf-8")
                ),
            )
            self._checkpoint_stage(run_id, RunStage.RENDERED)
        elif stage == "VIDEO_QA":
            if not bool(payload["render"]):
                self.repository.update_stage_job(run_id, "VIDEO_QA", status="SKIPPED")
                self.repository.update_run(run_id, stage=RunStage.COMPLETE, status="COMPLETE")
                return
            report = self.url_generator.qa_stage(run_id=run_id, artifact_root=self.artifact_root)
            self.repository.replace_json_artifact("quality_reports", run_id, report)
            self._checkpoint_stage(run_id, RunStage.QA_PASSED)
            self.repository.update_run(run_id, stage=RunStage.COMPLETE, status="COMPLETE")
            self._publish_delivery(run_id, artifacts)
        # Every URL stage may create one or more architectural artifacts.
        # Persist all evidence written so far; later stages replace only their
        # own singleton snapshots.
        self.repository.persist_run_documents(run_id, artifacts.root)
        self.repository.update_stage_job(run_id, stage, status="COMPLETE")
        logger.info("demo_job_stage_completed", stage=stage)

    async def run_fixture(self, run_id: str, gate: int, *, render: bool) -> None:
        bind_run_context(run_id=run_id, provider="local_playwright", operation="fixture_generation")
        logger.info("demo_job_started", gate=gate, render=render)
        try:
            started = perf_counter()
            await self._run_fixture(run_id, gate, render=render)
            self.repository.record_provider_call(
                run_id=run_id,
                provider="playwright",
                operation="fixture_generation",
                status="COMPLETE",
                duration_ms=int((perf_counter() - started) * 1000),
            )
            logger.info("demo_job_completed", gate=gate, status="COMPLETE")
        except Exception as error:
            self.repository.record_provider_call(
                run_id=run_id,
                provider="playwright",
                operation="fixture_generation",
                status="FAILED",
                error_code=type(error).__name__,
            )
            logger.exception("demo_job_failed", gate=gate, error_code=type(error).__name__)
            raise
        finally:
            clear_run_context()

    async def _run_fixture(self, run_id: str, gate: int, *, render: bool) -> None:
        attempt = self.repository.start_attempt(run_id, RunStage.FEASIBILITY_CHECK)
        run = self.repository.get_run(run_id)
        request = self.repository.get_request(run["request_id"])
        bind_run_context(
            request_id=request["request_id"],
            project_id=request.get("project_id"),
            attempt_id=attempt,
        )
        self.repository.update_run(run_id, stage=RunStage.FEASIBILITY_CHECK, status="RUNNING")
        self.repository.update_run(run_id, stage=RunStage.DISCOVERING, status="RUNNING")
        self._checkpoint_stage(run_id, RunStage.DISCOVERING)
        plan = self.planner.plan(request["objective"])
        self.repository.save_json_artifact("demo_plans", run_id, plan.model_dump(mode="json"))
        self.repository.save_workflow_steps(
            run_id, [step.model_dump(mode="json") for step in plan.workflow_steps]
        )
        self._checkpoint_stage(run_id, RunStage.PLAN_READY)
        self.repository.update_run(run_id, stage=RunStage.PLAN_VALIDATED, status="RUNNING")
        self._checkpoint_stage(run_id, RunStage.PLAN_VALIDATED)
        self.repository.update_run(run_id, stage=RunStage.PRODUCTION_EXECUTION, status="RUNNING")
        self._checkpoint_stage(run_id, RunStage.PRODUCTION_EXECUTION)
        try:
            trace = await run_fixture_gate(
                gate,
                self.artifact_root,
                render_final=render,
                run_id=run_id,
                speech_provider=self.speech_provider,
            )
            artifact_dir = self.artifact_root / "runs" / run_id
            self.repository.save_interaction_events(
                run_id, [event.model_dump(mode="json") for event in trace.events]
            )
            self.repository.update_run(run_id, stage=RunStage.TRACE_READY, status="RUNNING")
            self._checkpoint_stage(run_id, RunStage.TRACE_READY)
            presentation_path = artifact_dir / "presentation" / "presentation-plan.json"
            if presentation_path.exists():
                self.repository.save_json_artifact(
                    "presentation_plans",
                    run_id,
                    json.loads(presentation_path.read_text(encoding="utf-8")),
                )
            self.repository.update_run(
                run_id, stage=RunStage.PRESENTATION_PLANNED, status="RUNNING"
            )
            self._checkpoint_stage(run_id, RunStage.PRESENTATION_PLANNED)
            narration_path = artifact_dir / "presentation" / "narration-script.json"
            if narration_path.exists():
                self.repository.save_json_artifact(
                    "narration_scripts",
                    run_id,
                    json.loads(narration_path.read_text(encoding="utf-8")),
                )
            if self.speech_provider is not None:
                self.repository.update_run(run_id, stage=RunStage.NARRATION_READY, status="RUNNING")
                self._checkpoint_stage(run_id, RunStage.NARRATION_READY)
            for path in (artifact_dir / "qa").glob("*-report.json"):
                self.repository.save_json_artifact(
                    "quality_reports", run_id, json.loads(path.read_text(encoding="utf-8"))
                )
            for kind, path in {
                "trace": artifact_dir / "execution" / "trace.json",
                # Browserbase production capture is assembled from Session
                # Replay into MP4. Keep the diagnostic CDP WebM separately;
                # never register it as the deliverable browser recording.
                "browser_recording": artifact_dir / "execution" / "browser-recording.mp4",
                "browser_recording_diagnostic": artifact_dir
                / "execution"
                / "browser-recording.webm",
                "playwright_trace": artifact_dir / "execution" / "playwright-trace.zip",
                "final_video": artifact_dir / "final" / "demo.mp4",
            }.items():
                if path.exists():
                    if kind == "final_video":
                        self._register_final_video(run_id, path)
                    else:
                        self.repository.save_location(run_id, kind, str(path))
            self._record_delivery_assets(run_id, artifact_dir)
            if render:
                self.repository.update_run(run_id, stage=RunStage.RENDERED, status="RUNNING")
                self.repository.update_run(run_id, stage=RunStage.QA_PASSED, status="RUNNING")
                self._checkpoint_stage(run_id, RunStage.RENDERED)
                self._checkpoint_stage(run_id, RunStage.VIDEO_QA)
                self._checkpoint_stage(run_id, RunStage.QA_PASSED)
            else:
                # A trace-only fixture is an intentional no-render benchmark,
                # not a stuck narration/render/QA pipeline.
                for stage in ("NARRATION", "RENDER", "VIDEO_QA"):
                    self.repository.update_stage_job(run_id, stage, status="SKIPPED")
            self.repository.update_run(
                run_id,
                stage=RunStage.COMPLETE,
                status="COMPLETE" if trace.outcome_verified else "FAILED",
            )
            self.repository.finish_attempt(run_id, attempt, status="COMPLETE")
        except Exception as error:
            self.repository.update_run(
                run_id,
                stage=RunStage.FAILED,
                status="FAILED",
                error_code="PROVIDER_FAILURE"
                if isinstance(error, ProviderError)
                else "EXECUTION_OR_RENDER_FAILURE",
            )
            self.repository.finish_attempt(
                run_id,
                attempt,
                status="FAILED",
                failure_code="PROVIDER_FAILURE"
                if isinstance(error, ProviderError)
                else "EXECUTION_OR_RENDER_FAILURE",
            )
            raise

    async def run_url(
        self,
        run_id: str,
        *,
        allow_external_side_effects: bool,
        render: bool,
        cloud_discovery: bool,
        budget: DiscoveryBudget | None = None,
        stagehand_assist: bool = False,
        credential_reference: str | None = None,
        audience: str = "product prospect",
        target_duration_seconds: int = 120,
        allow_isolated_record_creation: bool = False,
    ) -> None:
        # A cloud discovery session always has the Stagehand bridge available
        # as an AI observation/re-grounding aid.  Keep the legacy argument for
        # API compatibility, but do not let callers accidentally disable the
        # capability on Browserbase runs; local fixture runs may still opt in.
        stagehand_assist = bool(stagehand_assist or cloud_discovery)
        run = self.repository.get_run(run_id)
        request = self.repository.get_request(run["request_id"])
        bind_run_context(request_id=request["request_id"], project_id=request.get("project_id"))
        bind_run_context(
            run_id=run_id,
            provider="browserbase" if cloud_discovery else "local_playwright",
            operation="url_generation",
            url=safe_url(request["url"]),
        )
        logger.info(
            "demo_job_started",
            render=render,
            cloud_discovery=cloud_discovery,
            stagehand_assist=stagehand_assist,
        )
        try:
            started = perf_counter()
            await self._run_url(
                run_id,
                allow_external_side_effects=allow_external_side_effects,
                render=render,
                cloud_discovery=cloud_discovery,
                budget=budget,
                stagehand_assist=stagehand_assist,
                credential_reference=credential_reference,
                audience=audience,
                target_duration_seconds=target_duration_seconds,
                allow_isolated_record_creation=allow_isolated_record_creation,
            )
            self.repository.record_provider_call(
                run_id=run_id,
                provider="playwright",
                operation="url_generation",
                status="COMPLETE",
                duration_ms=int((perf_counter() - started) * 1000),
            )
            logger.info("demo_job_completed", status="COMPLETE")
        except Exception as error:
            self.repository.record_provider_call(
                run_id=run_id,
                provider="playwright",
                operation="url_generation",
                status="FAILED",
                error_code=type(error).__name__,
            )
            logger.exception("demo_job_failed", error_code=type(error).__name__)
            raise
        finally:
            clear_run_context()

    async def _run_url(
        self,
        run_id: str,
        *,
        allow_external_side_effects: bool,
        render: bool,
        cloud_discovery: bool,
        budget: DiscoveryBudget | None = None,
        stagehand_assist: bool = False,
        credential_reference: str | None = None,
        audience: str = "product prospect",
        target_duration_seconds: int = 120,
        allow_isolated_record_creation: bool = False,
    ) -> None:
        stagehand_assist = bool(stagehand_assist or cloud_discovery)
        attempt = self.repository.start_attempt(run_id, RunStage.FEASIBILITY_CHECK)
        bind_run_context(attempt_id=attempt, stage=RunStage.FEASIBILITY_CHECK)
        artifacts = RunArtifacts(self.artifact_root, run_id)
        if self.url_generator is None:
            self.repository.update_run(
                run_id,
                stage=RunStage.FAILED,
                status="FAILED",
                error_code="PLANNING_PROVIDER_UNAVAILABLE",
            )
            self.repository.finish_attempt(
                run_id, attempt, status="FAILED", failure_code="PLANNING_PROVIDER_UNAVAILABLE"
            )
            raise RuntimeError("OpenRouter structured planning provider is not configured")
        # URL generation has one durable execution path.  Earlier revisions
        # retained a monolithic `UrlGenerationService.run()` fallback here;
        # that meant API-triggered runs could bypass the independently
        # resumable discovery, plan, execution, narration, render and QA
        # checkpoints used by workers.  Keep this coordinator deliberately
        # thin: each stage reconstructs its input from persisted artifacts.
        stage_payload: dict[str, Any] = {
            "allow_external_side_effects": allow_external_side_effects,
            "allow_isolated_record_creation": allow_isolated_record_creation,
            "render": render,
            "cloud_discovery": cloud_discovery,
            "cloud_production": cloud_discovery,
            "max_pages": int(getattr(budget, "max_pages", 6)),
            "stagehand_assist": stagehand_assist,
            "credential_reference": credential_reference,
            "audience": audience,
            "target_duration_seconds": target_duration_seconds,
            "presentation": {},
        }
        # Direct/supervised jobs do not pass through API enqueueing.  Provision
        # the same durable stage rows before discovery begins so artifacts,
        # progress, cancellation and recovery always agree.
        self.repository.ensure_stage_jobs(run_id)
        try:
            for stage in ("DISCOVERY", "PLANNING", "EXECUTION", "NARRATION", "RENDER", "VIDEO_QA"):
                await self.run_url_stage(run_id, stage, payload=stage_payload)
            self.repository.finish_attempt(run_id, attempt, status="COMPLETE")
            return
        except Exception as error:
            code = (
                "PROVIDER_FAILURE"
                if isinstance(error, ProviderError)
                else "AUTH_FAILURE"
                if "AUTH_REQUIRED" in str(error)
                else "URL_GENERATION_FAILURE"
            )
            self.repository.update_run(
                run_id, stage=RunStage.FAILED, status="FAILED", error_code=code
            )
            self.repository.fail_active_stage_jobs(run_id, code)
            for session_path in (
                artifacts.root / "execution" / "browserbase-session.json",
                artifacts.root / "discovery" / "browserbase-session.json",
            ):
                if session_path.exists():
                    session = json.loads(session_path.read_text(encoding="utf-8"))
                    self.repository.save_browser_session(
                        run_id, session["provider"], session["session_id"], "FAILED"
                    )
            self.repository.finish_attempt(run_id, attempt, status="FAILED", failure_code=code)
            raise

    def _record_delivery_assets(self, run_id: str, artifact_dir: Path) -> None:
        audio = artifact_dir / "audio" / "narration.mp3"
        if audio.exists():
            self.repository.save_audio_asset(
                run_id,
                str(audio),
                duration_seconds=audio_duration_seconds(audio),
                provider="elevenlabs",
            )
        final_video = artifact_dir / "final" / "demo.mp4"
        report = artifact_dir / "qa" / "delivery-report.json"
        if final_video.exists():
            metadata = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
            self.repository.save_video_render(
                run_id, str(final_video), status="COMPLETE", metadata=metadata
            )
