from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from time import perf_counter, time
from typing import Any

from app.artifacts.store import RunArtifacts
from app.benchmark.fixture_planning import FixturePlanningService
from app.benchmark.runner import run as run_fixture_gate
from app.contracts.harness import (
    HarnessLimits,
    HarnessMode,
    HarnessStatus,
    InteractionHarnessRequest,
)
from app.contracts.models import (
    ActionCapability,
    DemoPlan,
    DemoTrace,
    DiscoveryBudget,
    PresentationPlan,
    ProductKnowledge,
)
from app.interaction.harness import (
    load_and_validate,
    persist_validation,
    validation_for_exception,
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
from app.quality.repair import classify_repair
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
        repair_wall_clock_budget_seconds: int = 3_600,
    ):
        self.repository, self.artifact_root = repository, artifact_root
        self.planner = planner or FixturePlanningService()
        self.speech_provider = speech_provider
        self.url_generator = url_generator
        self.artifact_storage = artifact_storage
        self.repair_wall_clock_budget_seconds = max(
            60, int(repair_wall_clock_budget_seconds)
        )

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

    def _persist_failed_browser_session(self, run_id: str) -> None:
        """Keep cloud session identity after a failed stage, including repairs."""

        artifacts = RunArtifacts(self.artifact_root, run_id)
        for session_path in (
            artifacts.root / "execution" / "browserbase-session.json",
            artifacts.root / "discovery" / "browserbase-session.json",
        ):
            if not session_path.is_file():
                continue
            try:
                session = json.loads(session_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            provider = session.get("provider")
            session_id = session.get("session_id")
            if provider and session_id:
                self.repository.save_browser_session(
                    run_id, str(provider), str(session_id), "FAILED"
                )

    def _harness_request_for_run(
        self, run_id: str, payload: dict[str, Any]
    ) -> InteractionHarnessRequest:
        """Materialize a secret-free harness request from the durable job."""

        run = self.repository.get_run(run_id)
        request = self.repository.get_request(run["request_id"])
        mode = HarnessMode(str(payload.get("harness_mode") or "production_trace"))
        return InteractionHarnessRequest.model_validate(
            {
                "request_id": request.get("request_id"),
                "url": request["url"],
                "objective": request["objective"],
                "mode": mode.value,
                "audience": str(payload.get("audience") or "product prospect"),
                "auth_reference": payload.get("credential_reference"),
                "side_effect_policy": (
                    "explicitly_authorized"
                    if payload.get("allow_external_side_effects")
                    or payload.get("allow_isolated_record_creation")
                    else "read_only"
                ),
                "limits": HarnessLimits(
                    max_pages=int(payload.get("max_pages", 6)),
                    max_steps=int(payload.get("max_steps", 24)),
                    max_exploration_steps=int(payload.get("max_exploration_steps", 12)),
                    max_model_calls=int(payload.get("max_model_calls", 3)),
                    max_duration_seconds=int(payload.get("max_duration_seconds", 900)),
                ).model_dump(mode="json"),
                "required_outcomes": list(payload.get("required_outcomes") or []),
                "excluded_actions": list(payload.get("excluded_actions") or []),
                "cloud_browser": payload.get("cloud_discovery"),
                "project_id": request.get("project_id"),
            }
        )

    def finalize_interaction_harness(self, run_id: str, payload: dict[str, Any]) -> bool:
        """Promote a verified trace or terminally block the harness run."""

        artifacts = RunArtifacts(self.artifact_root, run_id)
        harness_request = self._harness_request_for_run(run_id, payload)
        validation = load_and_validate(artifacts, harness_request)
        if validation.result.status != HarnessStatus.VERIFIED:
            code = (
                validation.result.failure.code.value
                if validation.result.failure is not None
                else "OUTCOME_UNVERIFIED"
            )
            error_code = f"HARNESS_{code}"
            self.repository.fail_active_stage_jobs(run_id, error_code)
            self.repository.update_stage_job(
                run_id, "EXECUTION", status="FAILED", error_code=error_code
            )
            self.repository.update_run(
                run_id, stage="EXECUTION", status="FAILED", error_code=error_code
            )
            job = self.repository.job_for_run(run_id)
            if job:
                self.repository.finish_job(job["id"], status="FAILED", error_code=error_code)
            self.repository.persist_run_documents(run_id, artifacts.root)
            return False
        for downstream in ("NARRATION", "RENDER", "VIDEO_QA"):
            self.repository.update_stage_job(run_id, downstream, status="SKIPPED")
        self.repository.update_run(run_id, stage="COMPLETE", status="COMPLETE")
        job = self.repository.job_for_run(run_id)
        if job:
            self.repository.finish_job(job["id"], status="COMPLETE")
        self.repository.persist_run_documents(run_id, artifacts.root)
        return True

    def _schedule_automatic_repair(
        self,
        run_id: str,
        stage: str,
        *,
        payload: dict[str, Any],
        error: Exception,
    ) -> str | None:
        """Create a bounded targeted child run without consumer intervention."""

        repair_attempt = int(payload.get("_repair_attempt", 0))
        repair_started_epoch = float(payload.get("_repair_started_epoch", time()))
        repair_elapsed_seconds = max(0.0, time() - repair_started_epoch)
        artifacts = RunArtifacts(self.artifact_root, run_id)
        decision_path = artifacts.qa / "repair-decision.json"
        try:
            decision_payload = (
                json.loads(decision_path.read_text(encoding="utf-8"))
                if decision_path.is_file()
                else classify_repair(
                    [f"{stage}_{type(error).__name__}".upper(), str(error).upper()]
                ).model_dump(mode="json")
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        action = str(decision_payload.get("action") or "fail")
        category = str(decision_payload.get("category") or "internal")
        attempts_by_category = {
            str(key): int(value)
            for key, value in dict(payload.get("_repair_attempts_by_category") or {}).items()
        }
        category_attempt = attempts_by_category.get(category, 0)
        repair_budget_exhausted = (
            category_attempt >= 3
            or repair_elapsed_seconds >= self.repair_wall_clock_budget_seconds
        )
        if action in {"fail", "needs_input"} or repair_budget_exhausted:
            artifacts.write_json(
                "qa/repair-exhausted.json",
                {
                    "attempt": repair_attempt,
                    "category_attempt": category_attempt,
                    "max_attempts": 3,
                    "action": action,
                    "elapsed_seconds": round(repair_elapsed_seconds, 3),
                    "reasons": decision_payload.get("reasons", []),
                },
            )
            if action == "needs_input" or repair_budget_exhausted:
                blocker_code = (
                    "EXTERNAL_INPUT_REQUIRED"
                    if action == "needs_input"
                    else "REPAIR_BUDGET_EXHAUSTED"
                )
                self.repository.update_run(
                    run_id,
                    stage=stage,
                    status="NEEDS_INPUT",
                    error_code=blocker_code,
                )
                self._promote_repair_blocker(run_id, stage, blocker_code)
            return None
        stage_aliases = {
            "RENDERING": "RENDER",
            "PRODUCTION_EXECUTION": "EXECUTION",
            "VIDEO_QA": "VIDEO_QA",
        }
        requested_stage = str(decision_payload.get("retry_from_stage") or stage).upper()
        retry_from_stage = stage_aliases.get(requested_stage, requested_stage)
        # A repair decision may be produced by an editorial/planning check
        # before production has created a trace.  Never create a child whose
        # durable ledger marks NARRATION/RENDER as runnable while the evidence
        # those stages consume is absent; that turns a recoverable planning
        # rejection into a misleading FileNotFoundError.  Move the boundary
        # back to the earliest stage with the persisted prerequisite.  This is
        # intentionally artifact-based and therefore applies to every product,
        # not just a particular workflow or site.
        parent_root = artifacts.root
        has_plan = (parent_root / "plan.json").is_file() or (
            parent_root / "planning" / "validated-scene-plan.json"
        ).is_file()
        has_trace = (parent_root / "execution" / "trace.json").is_file()
        has_narration = (parent_root / "narration" / "narration-script.json").is_file() or (
            parent_root / "presentation" / "narration-script.json"
        ).is_file()
        stage_order = {
            "DISCOVERY": 0,
            "PLANNING": 1,
            "EXECUTION": 2,
            "NARRATION": 3,
            "RENDER": 4,
            "VIDEO_QA": 5,
        }
        boundary_is_downstream = stage_order.get(retry_from_stage, 99) > stage_order.get(stage, 99)
        if boundary_is_downstream and retry_from_stage in {"NARRATION", "RENDER", "VIDEO_QA"} and not has_trace:
            retry_from_stage = "EXECUTION" if has_plan else "PLANNING"
        elif boundary_is_downstream and retry_from_stage in {"RENDER", "VIDEO_QA"} and not has_narration:
            retry_from_stage = "NARRATION"
        if retry_from_stage not in {
            "DISCOVERY",
            "PLANNING",
            "EXECUTION",
            "NARRATION",
            "RENDER",
            "VIDEO_QA",
        }:
            return None
        try:
            retry = self.repository.create_retry_run(run_id, str(self.artifact_root))
            retry_id = str(retry["id"])
            RunArtifacts.clone_for_targeted_retry(
                self.artifact_root,
                run_id,
                retry_id,
                start_stage=retry_from_stage,
            )
            self.repository.ensure_stage_jobs(retry_id)
            self.repository.prepare_targeted_retry(retry_id, retry_from_stage)
            self.repository.copy_run_evidence(
                run_id, retry_id, through_stage=retry_from_stage
            )
            retry_payload = {
                **payload,
                "_repair_attempt": repair_attempt + 1,
                "_repair_started_epoch": repair_started_epoch,
                "_repair_parent_run_id": run_id,
                "_repair_category": decision_payload.get("category"),
                "_repair_attempts_by_category": {
                    **attempts_by_category,
                    category: category_attempt + 1,
                },
                "refresh_editorial": retry_from_stage == "NARRATION",
            }
            self.repository.enqueue_job(retry_id, "url", retry_payload)
            artifacts.write_json(
                "qa/repair-scheduled.json",
                {
                    "retry_run_id": retry_id,
                    "retry_from_stage": retry_from_stage,
                    "attempt": repair_attempt + 1,
                    "category": decision_payload.get("category"),
                    "reasons": decision_payload.get("reasons", []),
                },
            )
            RunArtifacts(self.artifact_root, retry_id).write_json(
                "repair/lineage.json",
                {
                    "parent_run_id": run_id,
                    "attempt": repair_attempt + 1,
                    "retry_from_stage": retry_from_stage,
                },
            )
            self.repository.update_run(
                run_id,
                stage=stage,
                status="REPAIRING",
                error_code=None,
            )
            return retry_id
        except Exception:
            logger.exception(
                "demo_job_repair_scheduling_failed",
                run_id=run_id,
                stage=stage,
            )
            return None

    def _promote_repair_success(self, run_id: str, artifacts: RunArtifacts) -> None:
        """Make the original consumer-visible run resolve to its repaired child."""

        child_id = run_id
        parent_id = self.repository.run_details(child_id).get("retry_of")
        while parent_id:
            self.repository.update_run(
                parent_id,
                stage=RunStage.COMPLETE,
                status="COMPLETE",
                error_code=None,
            )
            final_video = artifacts.root / "final" / "demo.mp4"
            if final_video.is_file():
                self.repository.save_location(
                    parent_id, "final_video", str(final_video)
                )
            manifest = artifacts.root / "artifact-manifest.json"
            if manifest.is_file():
                self.repository.save_location(
                    parent_id, "artifact_manifest", str(manifest)
                )
            child_id = str(parent_id)
            parent_id = self.repository.run_details(child_id).get("retry_of")

    def _promote_repair_blocker(
        self, run_id: str, stage: str, error_code: str
    ) -> None:
        child_id = run_id
        parent_id = self.repository.run_details(child_id).get("retry_of")
        while parent_id:
            self.repository.update_run(
                parent_id,
                stage=stage,
                status="NEEDS_INPUT",
                error_code=error_code,
            )
            child_id = str(parent_id)
            parent_id = self.repository.run_details(child_id).get("retry_of")

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

    async def run_url_stage(
        self,
        run_id: str,
        stage: str,
        *,
        payload: dict[str, Any],
        claimed_stage: dict[str, Any] | None = None,
    ) -> None:
        """Run one durable stage and terminally classify any unhandled failure."""
        # Direct supervisors and broker workers can observe the same queued
        # stage concurrently (for example after a shell timeout).  Claim the
        # stage with the repository's compare-and-set before touching provider
        # state.  A completed/skipped stage or an already-running lease is
        # idempotently ignored; this prevents duplicate Browserbase sessions
        # and concurrent Remotion renders writing into one run directory.
        self.repository.ensure_stage_jobs(run_id)
        current = self.repository.stage_job(run_id, stage)
        if current["status"] in {"COMPLETE", "SKIPPED"}:
            return
        owns_claim = False
        if current["status"] == "RUNNING":
            # A broker/local worker may pass the lease it just acquired. A
            # second delivery sees RUNNING but does not own that lease and
            # must not enter the provider path or duplicate a browser session.
            if claimed_stage is None or claimed_stage.get("status") != "RUNNING":
                return
            # ``claimed_stage`` is the compare-and-set result returned to the
            # worker.  Match its lease timestamp when available so a stale
            # delivery cannot take over a lease that has since been reclaimed.
            claimed_at = claimed_stage.get("claimed_at")
            current_claimed_at = current.get("claimed_at")
            if (
                claimed_at is not None
                and current_claimed_at is not None
                and claimed_at != current_claimed_at
            ):
                return
            owns_claim = True
        if current["status"] == "FAILED":
            # A failed checkpoint is an explicit repair boundary.  Silently
            # treating it as a no-op lets a supervisor mark the whole run
            # complete while an earlier stage is still failed.  Callers must
            # create a targeted retry (which restores the run lease and queues
            # this boundary) before executing it again.
            raise RuntimeError(
                f"stage {stage} is FAILED; create a targeted retry from this checkpoint"
            )
        if not owns_claim and self.repository.claim_stage_job(run_id, stage) is None:
            return
        job = self.repository.job_for_run(run_id)
        heartbeat = asyncio.create_task(self._heartbeat_stage(run_id, stage))
        try:
            await self._run_url_stage_impl(run_id, stage, payload=payload)
            # Interaction jobs deliberately stop at the verified-trace
            # boundary. They never enter narration, rendering, or video QA.
            if job is not None and job["kind"] == "interaction" and stage == "EXECUTION":
                self.finalize_interaction_harness(run_id, payload)
        except Exception as error:
            if job is not None and job["kind"] == "interaction":
                # Capability runs terminate at the typed harness boundary. Do
                # not schedule a full-generation repair that could replay a
                # dispatched mutation; persist the owning failure and leave
                # the run at its safe checkpoint instead.
                try:
                    harness_request = self._harness_request_for_run(run_id, payload)
                    artifacts = RunArtifacts(self.artifact_root, run_id)
                    existing_failure_path = artifacts.root / "harness" / "failure.json"
                    existing_failure = None
                    if existing_failure_path.is_file():
                        try:
                            existing_failure = json.loads(
                                existing_failure_path.read_text(encoding="utf-8")
                            )
                        except (OSError, ValueError):
                            existing_failure = None
                    if isinstance(existing_failure, dict) and existing_failure.get("code"):
                        # A strict promotion gate may already have persisted a
                        # more specific witness failure. Preserve it instead
                        # of replacing it with the wrapper RuntimeError.
                        error_code = f"HARNESS_{existing_failure['code']}"
                        validation = None
                    else:
                        validation = None
                    if validation is None and existing_failure is None:
                        try:
                            trace = self._load_trace(artifacts)
                        except (FileNotFoundError, OSError, ValueError):
                            trace = None
                        validation = validation_for_exception(
                            harness_request, trace, error, run_id=run_id
                        )
                        persist_validation(artifacts, harness_request, validation)
                        error_code = f"HARNESS_{validation.result.failure.code.value}"
                except Exception:  # noqa: BLE001 - preserve a typed terminal state
                    error_code = "HARNESS_INTERNAL_ERROR"
                self.repository.fail_active_stage_jobs(run_id, error_code)
                self.repository.update_stage_job(
                    run_id, stage, status="FAILED", error_code=error_code
                )
                self.repository.update_run(
                    run_id, stage=stage, status="FAILED", error_code=error_code
                )
                self.repository.finish_job(job["id"], status="FAILED", error_code=error_code)
                self.repository.persist_run_documents(run_id, RunArtifacts(self.artifact_root, run_id).root)
                logger.error("interaction_harness_blocked", stage=stage, error_code=error_code)
                return
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
            self._persist_failed_browser_session(run_id)
            retry_id = self._schedule_automatic_repair(
                run_id, stage, payload=payload, error=error
            )
            if retry_id is None:
                raise
            logger.info(
                "demo_job_repair_scheduled",
                stage=stage,
                retry_run_id=retry_id,
            )
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
        # Production follows the same configured cloud browser as discovery
        # unless an operator explicitly overrides it.  Keep the diagnostic
        # truthful as well as the executor (which already uses this fallback),
        # otherwise operators incorrectly conclude that a local browser was
        # used for the capture.
        effective_cloud_production = bool(
            payload.get("cloud_production", payload.get("cloud_discovery", False))
        )
        logger.info(
            "demo_job_stage_started",
            stage=stage,
            cloud_discovery=bool(payload.get("cloud_discovery", False)),
            cloud_production=effective_cloud_production,
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
                    bool(payload.get("allow_external_side_effects", False))
                    or bool(payload.get("allow_isolated_record_creation", False))
                ),
                audience=str(payload.get("audience", "product prospect")),
                target_duration_seconds=int(payload.get("target_duration_seconds", 120)),
                trace_only=bool(payload.get("trace_only", False)),
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
            if bool(payload.get("harness_gate", False)):
                # The trace-only harness is the promotion boundary. A normal
                # URL job may proceed to narration/rendering only after the
                # same certificate used by capability runs has passed.
                harness_request = self._harness_request_for_run(run_id, payload)
                validation = load_and_validate(artifacts, harness_request)
                if validation.result.status != HarnessStatus.VERIFIED:
                    failure = validation.result.failure
                    code = failure.code.value if failure is not None else "OUTCOME_UNVERIFIED"
                    raise RuntimeError(f"HARNESS_{code}: trace was not certified")
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
            # Remotion/FFmpeg rendering is synchronous and can take many
            # minutes for a 1080p walkthrough. Keep it off the async event
            # loop so the stage heartbeat continues to refresh its durable
            # lease; otherwise the recovery worker can incorrectly requeue a
            # healthy render while the original process is still writing it.
            output = await asyncio.to_thread(
                self.url_generator.render_stage,
                run_id=run_id,
                artifact_root=self.artifact_root,
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
            self._promote_repair_success(run_id, artifacts)
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
        queued_job = self.repository.job_for_run(run_id)
        if isinstance(queued_job, dict) and isinstance(queued_job.get("payload"), dict):
            stage_payload.update(
                {
                    key: value
                    for key, value in queued_job["payload"].items()
                    if str(key).startswith("_repair_") or key == "refresh_editorial"
                }
            )
        # Direct/supervised jobs do not pass through API enqueueing.  Provision
        # the same durable stage rows before discovery begins so artifacts,
        # progress, cancellation and recovery always agree.
        self.repository.ensure_stage_jobs(run_id)
        try:
            for stage in ("DISCOVERY", "PLANNING", "EXECUTION", "NARRATION", "RENDER", "VIDEO_QA"):
                await self.run_url_stage(run_id, stage, payload=stage_payload)
                current = self.repository.get_run(run_id)
                if current["status"] == "REPAIRING":
                    scheduled_path = artifacts.qa / "repair-scheduled.json"
                    scheduled = json.loads(scheduled_path.read_text(encoding="utf-8"))
                    retry_id = str(scheduled["retry_run_id"])
                    await self._run_url(
                        retry_id,
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
                    self.repository.finish_attempt(run_id, attempt, status="COMPLETE")
                    return
                if current["status"] == "NEEDS_INPUT":
                    raise RuntimeError(
                        current.get("error_code") or "EXTERNAL_INPUT_REQUIRED"
                    )
            self.repository.finish_attempt(run_id, attempt, status="COMPLETE")
            return
        except Exception as error:
            current = self.repository.get_run(run_id)
            if current["status"] == "NEEDS_INPUT":
                self.repository.finish_attempt(
                    run_id,
                    attempt,
                    status="FAILED",
                    failure_code=current.get("error_code") or "EXTERNAL_INPUT_REQUIRED",
                )
                raise
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
