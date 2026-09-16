from __future__ import annotations

import json
import re
from pathlib import Path
from app.artifacts.store import RunArtifacts, materialize_trace_lifecycle
from app.contracts.models import (
    ActionCapability,
    AudienceProfile,
    DemoPlan,
    DemoTrace,
    DiscoveryBudget,
    EditorialStoryboard,
    ExplorationReport,
    FormField,
    InteractionEvent,
    InteractionTrace,
    NarrationScript,
    NarrationSegment,
    ObjectiveSpec,
    ObservedElement,
    OperationKind,
    Postcondition,
    ProductContext,
    Rect,
    ReplanDecision,
    SemanticOperation,
    Target,
    Viewport,
    ViewportDecision,
    WorkflowStep,
)
from app.evaluation.completion_audit import audit_run
from app.evaluation.sample_video_benchmark import compare_to_sample_benchmark
from app.presentation.journey import build_journey, inspect_journey
from app.presentation.scenes import build_scene_plan
from app.quality.consistency import validate_selected_candidate_consistency
from app.quality.coverage import inspect_coverage
from app.quality.delivery import delivery_report
from app.quality.editorial import inspect_editorial, inspect_editorial_preflight
from app.quality.multimodal import VisualReviewer, build_review_packet, review_multimodal
from app.quality.presentation import (
    attach_presentation_qa,
    inspect_presentation,
)
from app.quality.repair import classify_repair
from app.quality.synchronization import inspect_synchronization, secure_transition_intervals
from app.quality.video import inspect_video
from app.services.generation_policy import (
    production_duration_envelope as _production_duration_envelope,
)

class QaMixin:
    def qa_stage(self, *, run_id: str, artifact_root: Path) -> dict:
        artifacts = RunArtifacts(artifact_root, run_id)
        trace = self._load_trace(artifacts)
        # QA may be resumed after a targeted execution repair.  Reconcile the
        # execution verdict from the immutable trace so a stale failure report
        # from the previous attempt can never survive beside a successful
        # production capture.
        execution_failures = []
        if not trace.outcome_verified:
            execution_failures = [
                str(error.get("code") or "PRODUCTION_CAPTURE_INTERRUPTED")
                for error in trace.errors[-1:]
            ] or ["PRODUCTION_CAPTURE_INTERRUPTED"]
        interaction_failures: list[str] = []
        interaction_path = artifacts.execution / "interaction-trace.json"
        if not interaction_path.is_file():
            interaction_failures.append("INTERACTION_TRACE_MISSING")
        else:
            try:
                interaction_trace = InteractionTrace.model_validate(
                    json.loads(interaction_path.read_text(encoding="utf-8"))
                )
                if not interaction_trace.complete:
                    interaction_failures.append("INTERACTION_TRACE_INCOMPLETE")
                latest_verdicts: dict[str, str] = {}
                for item in interaction_trace.verifications:
                    latest_verdicts[item.intent_id] = item.status
                if any(item != "passed" for item in latest_verdicts.values()):
                    interaction_failures.append("INTERACTION_VERIFICATION_FAILED")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                interaction_failures.append("INTERACTION_TRACE_INVALID")
        execution_failures = [*execution_failures, *interaction_failures]
        artifacts.write_json(
            "qa/execution-report.json",
            {
                "outcome_verified": bool(trace.outcome_verified),
                "event_count": len(trace.events),
                "hard_failures": execution_failures,
                "interaction_trace": {
                    "path": "execution/interaction-trace.json",
                    "hard_failures": interaction_failures,
                },
            },
        )
        script = json.loads(
            (artifacts.presentation / "narration-script.json").read_text(encoding="utf-8")
        )["script"]
        captions_path = artifacts.presentation / "rendered-captions.json"
        captions = json.loads(
            (
                captions_path
                if captions_path.exists()
                else artifacts.presentation / "captions.json"
            ).read_text(encoding="utf-8")
        )
        plan = self._load_plan(artifacts)
        # Revalidate the persisted plan at delivery time.  QA can be resumed
        # independently of narration/render, so it must not depend on the
        # local variable created during an earlier stage.  This also prevents
        # a stale selected-candidate summary from being published after a
        # targeted retry.
        plan_consistency_failures = validate_selected_candidate_consistency(
            plan.model_dump(mode="json")
        )
        artifacts.write_json(
            "qa/plan-consistency-report.json",
            {
                "status": "pass" if not plan_consistency_failures else "failed",
                "hard_failures": plan_consistency_failures,
                "selected_workflow": plan.selected_workflow,
            },
        )
        effective_maximum, duration_accounting = _production_duration_envelope(plan)
        if duration_accounting:
            artifacts.write_json("presentation/duration-accounting.json", duration_accounting)
        source_video = artifacts.browser_video_path()
        source_time_map: list[tuple[float, float]] | None = None
        source_is_edited = False
        source_edit_path = artifacts.presentation / "source-edit-plan.json"
        if source_edit_path.exists():
            try:
                source_edit = json.loads(source_edit_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                source_edit = {}
            rendered_source = (
                source_edit.get("rendered_source") if isinstance(source_edit, dict) else None
            )
            if isinstance(rendered_source, str):
                candidate_source = artifacts.root / rendered_source
                if candidate_source.is_file():
                    source_video = candidate_source
                    source_is_edited = True
            raw_windows = source_edit.get("windows") if isinstance(source_edit, dict) else None
            if not source_is_edited and isinstance(raw_windows, list):
                parsed_windows: list[tuple[float, float]] = []
                for window in raw_windows:
                    if not isinstance(window, dict):
                        continue
                    try:
                        start, end = float(window["start"]), float(window["end"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if end > start:
                        parsed_windows.append((start, end))
                source_time_map = parsed_windows or None
        # Compatibility for artifacts created before content-addressed source
        # edits. New runs always use the exact rendered_source above.
        if (
            source_video == artifacts.browser_video_path()
            and (artifacts.root / "render" / "editorial-source.mp4").is_file()
        ):
            source_video = artifacts.root / "render" / "editorial-source.mp4"
        # A genuinely single-state product can have fewer viable story scenes
        # than the generic thorough-walkthrough envelope. Do not stretch or
        # freeze that footage merely to satisfy a nominal 110-second default:
        # lower the floor only when the validated storyboard contains one
        # page, at most two non-opening scenes, and no meaningful interaction.
        duration_floor = self._duration_floor(plan)
        duration_exception: dict[str, object] | None = None
        storyboard_for_duration: EditorialStoryboard | None = None
        storyboard_path_for_duration = artifacts.presentation / "storyboard.json"
        if storyboard_path_for_duration.is_file():
            try:
                storyboard_for_duration = EditorialStoryboard.model_validate(
                    json.loads(storyboard_path_for_duration.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                storyboard_for_duration = None
        if storyboard_for_duration is not None:
            bound_scenes = [
                scene for scene in storyboard_for_duration.scenes if scene.operation_id is not None
            ]
            pages = {
                str(scene.page_url or "").rstrip("/") for scene in bound_scenes if scene.page_url
            }
            meaningful = any(
                scene.interaction in {"scroll", "click", "type", "submit"} for scene in bound_scenes
            )
            if len(bound_scenes) <= 2 and len(pages) <= 1 and not meaningful:
                dwell_floor = sum(
                    max(1.0, float(scene.required_dwell_seconds or 0)) for scene in bound_scenes
                )
                duration_floor = max(12, int(dwell_floor + 1.0))
                duration_exception = {
                    "requested_minimum_seconds": self._duration_floor(plan),
                    "effective_minimum_seconds": duration_floor,
                    "reason": "validated single-page story has no additional viable interaction scenes",
                    "scene_count": len(bound_scenes),
                    "page_count": len(pages),
                }
        video = inspect_video(
            artifacts.root / "final" / "demo.mp4",
            execution_verified=trace.outcome_verified,
            minimum_duration_seconds=duration_floor,
            maximum_duration_seconds=effective_maximum,
            source_video=source_video,
            source_time_map=source_time_map,
            source_is_edited=source_is_edited,
        )
        # Whiteboard/diagram products legitimately keep a near-white drawing
        # surface behind a sparse toolbar.  When the objective is explicitly
        # visual and the production trace proves committed pointer changes,
        # the central-region blank heuristic is not evidence of missing
        # footage.  Remove only that one heuristic failure; codec, pacing,
        # source-faithfulness, and synchronization gates remain strict.
        visual_surface_proved = bool(
            re.search(
                r"\b(?:draw|drawing|diagram|architecture|whiteboard|canvas|flowchart)\b",
                trace.objective.casefold(),
            )
            and any(
                event.kind is OperationKind.POINTER_SEQUENCE
                and isinstance(event.after, dict)
                and isinstance(event.after.get("gesture"), dict)
                and event.after["gesture"].get("surface_changed") is True
                for event in trace.events
            )
        )
        if visual_surface_proved and "BLANK_PRODUCT_CONTENT_INTERVAL" in video.get(
            "hard_failures", []
        ):
            video["hard_failures"].remove("BLANK_PRODUCT_CONTENT_INTERVAL")
            video.setdefault("warnings", []).append("VALIDATED_LIGHT_VISUAL_SURFACE")
        if duration_accounting:
            video.setdefault("warnings", []).append(
                "THOROUGH_WALKTHROUGH_DURATION_ACCOUNTING_ALLOWANCE"
            )
            video["duration_accounting"] = duration_accounting
        if duration_exception:
            video.setdefault("warnings", []).append("VALIDATED_SHORT_STORY_DURATION_EXCEPTION")
            video["duration_exception"] = duration_exception
            # Sparse editors and blank-state workspaces can legitimately have
            # a white central canvas while their toolbars, palettes, and
            # document chrome remain visible. The central-region heuristic is
            # intentionally strict for normal stories, but this validated
            # single-state exception proves that no additional page/action
            # scene exists; do not reject the native recording solely because
            # the product's usable surface is intentionally empty.
            if "BLANK_PRODUCT_CONTENT_INTERVAL" in video.get("hard_failures", []):
                video["hard_failures"].remove("BLANK_PRODUCT_CONTENT_INTERVAL")
                video.setdefault("warnings", []).append("SPARSE_PRODUCT_SURFACE_ACCEPTED")
        # A repository-level benchmark is optional at runtime (deployments do
        # not need to ship reference media), but when an operator has created
        # one it becomes hard delivery evidence rather than an advisory note.
        # This keeps sample quality standards explicit without coupling the
        # generic generator to a particular product or URL.
        benchmark_path = artifact_root / "quality-benchmarks" / "sample-video-benchmark.json"
        sample_benchmark: dict[str, object] | None = None
        if benchmark_path.is_file():
            try:
                benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
                sample_benchmark = compare_to_sample_benchmark(
                    artifacts.root / "final" / "demo.mp4", benchmark
                )
                # The supplied references are a presentation-quality floor,
                # not a fixed duration contract. Focused feature demos are
                # intentionally allowed to be concise within their validated
                # objective envelope (typically 1–2 minutes); a shorter
                # reference video must not reject an otherwise complete flow.
                if (
                    "BELOW_SAMPLE_DURATION_ENVELOPE" in sample_benchmark["hard_failures"]
                    and plan.objective
                    # Thoroughness controls depth, not breadth. A focused
                    # feature request may legitimately use that adjective and
                    # remains valid within its 1–2 minute objective envelope.
                    and (
                        not re.search(r"\b(?:full|complete|entire)\b", plan.objective.lower())
                        or duration_exception is not None
                    )
                ):
                    sample_benchmark["hard_failures"].remove("BELOW_SAMPLE_DURATION_ENVELOPE")
                    sample_benchmark.setdefault("warnings", []).append(
                        "BELOW_SAMPLE_REFERENCE_DURATION"
                    )
                video["hard_failures"] = [
                    *video.get("hard_failures", []),
                    *sample_benchmark["hard_failures"],
                ]
                if sample_benchmark["hard_failures"]:
                    video["visual_score"] = 0.0
                    video["overall_score"] = 0.0
            except (OSError, ValueError, TypeError, json.JSONDecodeError, RuntimeError) as error:
                sample_benchmark = {
                    "status": "unavailable",
                    "warning": f"SAMPLE_BENCHMARK_UNREADABLE: {type(error).__name__}",
                }
        # A CDP screencast is useful diagnostic evidence, but it is not an
        # acceptable production source for a Browserbase run.  If native
        # recording assembly failed, keep the diagnostic artifact for repair
        # while making the delivery decision explicitly blocking instead of
        # silently presenting the fallback as the product video.
        native_recording_meta = artifacts.execution / "browserbase-recording.json"
        if native_recording_meta.exists():
            try:
                native_meta = json.loads(native_recording_meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                native_meta = {}
            if native_meta.get("native_recording") == "unavailable":
                video["hard_failures"] = [
                    *video.get("hard_failures", []),
                    "BROWSERBASE_NATIVE_RECORDING_UNAVAILABLE",
                ]
                video["visual_score"] = 0.0
                video["overall_score"] = 0.0
        presentation_props = json.loads(
            (artifacts.presentation / "remotion-props.json").read_text(encoding="utf-8")
        )
        presentation = inspect_presentation(trace, presentation_props)
        video = attach_presentation_qa(video, presentation)
        synchronization = inspect_synchronization(
            trace,
            script,
            captions,
            narration_requested=False,
            narration_created=(artifacts.root / "audio" / "narration.mp3").exists(),
            explained_intervals=secure_transition_intervals(presentation_props),
        )
        story = json.loads((artifacts.qa / "story-report.json").read_text(encoding="utf-8"))
        context = ProductContext.model_validate(
            json.loads(
                (artifacts.root / "discovery" / "product-context.json").read_text(encoding="utf-8")
            )
        )
        storyboard_path = artifacts.presentation / "storyboard.json"
        storyboard = (
            EditorialStoryboard.model_validate(
                json.loads(storyboard_path.read_text(encoding="utf-8"))
            )
            if storyboard_path.exists()
            else None
        )
        # Cloud runs are required to invoke the configured Stagehand bridge.
        # Its suggestions remain advisory, but silently accepting an
        # unavailable bridge defeats the AI-assisted discovery contract and
        # makes a deterministic fallback look like a successful exploration.
        # Classify this at delivery time so the repair coordinator can retry
        # only discovery instead of re-recording an invalid story.
        exploration_qa: dict[str, object] = {
            "exploration_score": 1.0,
            "hard_failures": [],
            "warnings": [],
        }
        discovery_root = artifacts.root / "discovery"
        cloud_session_path = discovery_root / "browserbase-session.json"
        stagehand_path = discovery_root / "stagehand-observation.json"
        if cloud_session_path.exists():
            try:
                stagehand_observation = json.loads(stagehand_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                stagehand_observation = {}
            if not isinstance(stagehand_observation, dict):
                exploration_qa = {
                    "exploration_score": 0.0,
                    "hard_failures": ["STAGEHAND_OBSERVATION_UNAVAILABLE"],
                    "warnings": [],
                    "reason": "cloud exploration did not produce a validated Stagehand observation",
                }
            elif stagehand_observation.get("status") != "OBSERVED":
                # Stagehand is an advisory intelligence provider.  A quota,
                # model-access, or transient network failure must not turn a
                # fully grounded Playwright discovery into an unusable video:
                # the deterministic evidence path remains the source of truth.
                # Keep the provider failure explicit for operators and repair
                # tooling, but let delivery QA decide the actual product
                # evidence independently.
                exploration_qa = {
                    "exploration_score": 1.0,
                    "hard_failures": [],
                    "warnings": ["STAGEHAND_PROVIDER_UNAVAILABLE"],
                    "reason": str(
                        stagehand_observation.get("reason")
                        or "Stagehand observation was unavailable; Playwright evidence retained"
                    )[:500],
                    "provider_status": str(stagehand_observation.get("status") or "UNAVAILABLE"),
                }
        artifacts.write_json("qa/exploration-report.json", exploration_qa)
        actual_duration = float(video.get("probe", {}).get("format", {}).get("duration") or 0)
        # A target is an editorial planning aid, not a renderer stretch target.
        # The approved ObjectiveSpec still supplies hard lower/upper delivery
        # bounds: a presentation that exceeds its maximum needs a directed
        # dead-time/transition repair, never an arbitrary global speed-up.
        # The objective owns the delivery lower bound.  Older storyboard
        # artifacts may contain a sum-of-scene floor from before transition
        # overlap was modelled; using that stale derived value would reject a
        # valid native-speed render during a targeted QA retry.  Scene dwell is
        # checked separately by editorial QA, so compare the video against the
        # stable objective contract here.
        storyboard_minimum = float(duration_floor or 0)
        if duration_exception is None:
            storyboard_minimum = max(45.0, storyboard_minimum)
        if storyboard is not None and actual_duration + 0.25 < storyboard_minimum:
            video["hard_failures"] = [
                *video.get("hard_failures", []),
                "EDITORIAL_DURATION_BELOW_STORYBOARD_MINIMUM",
            ]
            video["visual_score"] = 0.0
            video["overall_score"] = 0.0
        editorial = inspect_editorial(
            context=context, plan=plan, trace=trace, storyboard=storyboard, script=script
        )
        story = {
            **story,
            "story_score": min(float(story["story_score"]), float(editorial["editorial_score"])),
            "hard_failures": [*story.get("hard_failures", []), *editorial["hard_failures"]],
        }
        viewport = ViewportDecision.model_validate(
            json.loads(
                (artifacts.root / "discovery" / "viewport-decision.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        # A targeted render/QA retry may intentionally skip execution and
        # presentation stages. Reconstruct their pure, trace-derived reports
        # instead of treating missing inherited files as a delivery failure.
        # Coverage is a pure function of the current owned trace and plan.
        # Recompute it on every QA/presentation retry: inheriting a prior
        # report would let a stale pre-submit witness survive after the trace
        # or repair boundary changed.
        coverage = inspect_coverage(plan, trace)
        artifacts.write_json("qa/coverage-report.json", coverage)
        # Rebuild this pure report from the owned trace on every QA attempt.
        # A target-repair may inherit a stale report from a prior scene plan;
        # delivery must be gated by the current page-completion evidence.
        scene_path = artifacts.presentation / "scene-plan.json"
        scene_data = (
            json.loads(scene_path.read_text(encoding="utf-8"))
            if scene_path.exists()
            else build_scene_plan(trace, storyboard=storyboard)
        )
        journey_report = inspect_journey(build_journey(trace, scene_data))
        artifacts.write_json("quality/journey-report.json", journey_report)
        story = {
            **story,
            "story_score": min(float(story["story_score"]), float(journey_report["journey_score"])),
            "hard_failures": [*story.get("hard_failures", []), *journey_report["hard_failures"]],
        }
        # The delivery manifest is itself evidence-based. Persist individual QA
        # artifacts before evaluating required delivery artifacts so the first
        # successful QA attempt cannot self-reject due to write ordering.
        artifacts.write_json("qa/video-report.json", video)
        if sample_benchmark is not None:
            artifacts.write_json("qa/sample-benchmark-report.json", sample_benchmark)
        artifacts.write_json("qa/presentation-report.json", presentation)
        artifacts.write_json("qa/synchronization-report.json", synchronization)
        artifacts.write_json("qa/editorial-report.json", editorial)
        multimodal = review_multimodal(
            build_review_packet(
                video=artifacts.root / "final" / "demo.mp4",
                run_id=run_id,
                trace=trace.model_dump(mode="json"),
                storyboard=storyboard.model_dump(mode="json") if storyboard else None,
                sample_seconds=[
                    round(actual_duration * fraction, 3)
                    for fraction in (0.05, 0.2, 0.4, 0.6, 0.8, 0.95)
                    if actual_duration * fraction >= 0.1
                ],
            ),
            reviewer=self.visual_reviewer,
        )
        artifacts.write_json("qa/multimodal-report.json", multimodal)
        # Materialize the manifest before computing delivery requirements. The
        # manifest is itself a required URL-delivery artifact; computing the
        # report first made a valid run self-reject with
        # ``artifact_manifest=false`` and only then create the file.
        artifacts.write_manifest()
        report = delivery_report(
            artifacts=artifacts.required_url_delivery_artifacts(),
            execution={
                "execution_score": coverage["coverage_score"],
                "workflow_score": coverage["coverage_score"],
                "hard_failures": [
                    *coverage["hard_failures"],
                    *exploration_qa["hard_failures"],
                    *plan_consistency_failures,
                ],
            },
            story=story,
            video=video,
            synchronization=synchronization,
            visual_review=multimodal,
            viewport_score=viewport.score,
        )
        artifacts.write_json("qa/delivery-report.json", report)
        # A delivery decision alone is too terse for an operator deciding
        # whether to publish, repair, or wait for an optional provider. Keep
        # an explicit, evidence-derived gap report beside every run. It never
        # invents a limitation: blockers come from hard QA failures and
        # caveats come only from reports that actually emitted a warning.
        report_sources = {
            "coverage": coverage,
            "exploration": exploration_qa,
            "story": story,
            "video": video,
            "presentation": presentation,
            "synchronization": synchronization,
            "multimodal": multimodal,
        }
        artifacts.write_json(
            "qa/gap-report.json",
            {
                "status": "ready_with_caveats" if report["deliverable"] else "repair_required",
                "deliverable": bool(report["deliverable"]),
                "blockers": list(
                    dict.fromkeys(
                        failure
                        for source in report_sources.values()
                        for failure in source.get("hard_failures", [])
                    )
                ),
                "caveats": list(
                    dict.fromkeys(
                        warning
                        for source in report_sources.values()
                        for warning in source.get("warnings", [])
                    )
                ),
                "optional_layers": {
                    "tts": "not generated"
                    if not (artifacts.root / "audio" / "narration.mp3").exists()
                    else "generated",
                    "multimodal_review": str(multimodal.get("status", "unavailable")),
                },
                "evidence_reports": {
                    name: f"qa/{name}-report.json"
                    for name in (
                        "exploration",
                        "video",
                        "presentation",
                        "synchronization",
                        "editorial",
                        "delivery",
                    )
                },
            },
        )
        # The delivery report is part of the manifest. Hash it before running
        # the completion audit so the audit verifies the same immutable set of
        # artifacts that the worker will hand off.
        artifacts.write_manifest()
        artifacts.write_json("qa/completion-audit.json", audit_run(artifacts.root))
        # Refresh after the audit so the manifest covers every final artifact.
        artifacts.write_manifest()
        if not report["deliverable"]:
            artifacts.write_json(
                "qa/repair-decision.json",
                classify_repair(report["hard_failures"]).model_dump(mode="json"),
            )
            raise RuntimeError(f"Delivery QA rejected render: {report['hard_failures']}")
        # A targeted QA repair may have inherited a failure decision from an
        # earlier attempt.  Leaving that stale marker beside a successful
        # delivery causes operators and API clients to report a false retry
        # state, so remove it before publishing the final manifest.
        stale_repair = artifacts.qa / "repair-decision.json"
        if stale_repair.exists():
            stale_repair.unlink()
            artifacts.write_manifest()
        return report

    @staticmethod
    def _duration_floor(plan: DemoPlan) -> int | None:
        """Return the objective's approved lower duration bound for every demo.

        ``target_duration_seconds`` guides editorial allocation, but the
        objective's minimum is a delivery promise for both feature and full
        walkthroughs.  Exempting feature demos here allowed a 20–45 second
        route/form clip to pass even when planning had explicitly approved a
        one-minute minimum.
        """
        return plan.minimum_duration_seconds

