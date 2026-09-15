"""Rebuild a delivery MP4 from retained verified evidence without providers."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from productlens.artifacts.store import RunArtifacts
from productlens.contracts.models import (
    DemoTrace,
    EditorialStoryboard,
    PresentationPlan,
    ViewportDecision,
)
from productlens.presentation.director import build_presentation_plan
from productlens.presentation.journey import build_journey, inspect_journey
from productlens.presentation.scenes import build_scene_plan, inspect_scene_plan
from productlens.video.render import render_remotion


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--target-duration-seconds", type=int)
    parser.add_argument(
        "--verify", action="store_true", help="Run provider-free delivery QA after rendering"
    )
    parser.add_argument(
        "--verify-only", action="store_true", help="Run provider-free delivery QA without rendering"
    )
    parser.add_argument(
        "--refresh-editorial",
        action="store_true",
        help="Regenerate deterministic evidence-grounded captions from retained discovery and trace evidence.",
    )
    parser.add_argument(
        "--refresh-editorial-only",
        action="store_true",
        help="Refresh retained-evidence editorial artifacts without rendering a new MP4.",
    )
    parser.add_argument(
        "--refresh-journey",
        action="store_true",
        help="Rebuild scene, presentation, and journey direction from the retained verified trace.",
    )
    parser.add_argument(
        "--materialize-captions",
        action="store_true",
        help="Create caption artifacts from the approved storyboard without rewriting its editorial content.",
    )
    args = parser.parse_args()
    # Operators naturally paste either ``artifacts/`` or the concrete
    # ``artifacts/runs/<run-id>/`` path.  Normalize both forms so a targeted
    # repair cannot fail before it reaches the retained evidence.
    artifact_root = args.artifact_root
    if artifact_root.name == args.run_id and artifact_root.parent.name == "runs":
        artifact_root = artifact_root.parent.parent
    artifacts = RunArtifacts(artifact_root, args.run_id)
    trace = DemoTrace.model_validate(
        json.loads((artifacts.execution / "trace.json").read_text(encoding="utf-8"))
    )
    narration = artifacts.root / "audio" / "narration.mp3"
    if args.verify_only and args.target_duration_seconds is not None:
        parser.error("--target-duration-seconds cannot be used with --verify-only")
    if args.verify_only and args.refresh_editorial:
        parser.error("--refresh-editorial cannot be used with --verify-only")
    if args.refresh_editorial_only and (
        args.verify
        or args.verify_only
        or args.refresh_editorial
        or args.refresh_journey
        or args.materialize_captions
    ):
        parser.error(
            "--refresh-editorial-only cannot be combined with render or verification flags"
        )
    if args.refresh_editorial or args.refresh_editorial_only or args.materialize_captions:
        from productlens.services.runtime import build_job_service

        _, job_service = build_job_service()
        if job_service.url_generator is None:
            parser.error(
                "An OpenRouter-configured generation service is required to refresh editorial copy"
            )
        asyncio.run(
            job_service.url_generator.narration_stage(
                run_id=args.run_id,
                artifact_root=artifact_root,
                # Captions are normally materialized from the approved
                # storyboard. Editorial regeneration is an explicit repair,
                # never an accidental side effect of rendering retained trace.
                refresh_editorial=args.refresh_editorial or args.refresh_editorial_only,
            )
        )
    if args.refresh_editorial_only:
        print(artifacts.presentation / "narration-script.json")
        return
    if args.refresh_journey:
        # Rebuild after any editorial repair so camera, dwell and caption
        # intent are aligned with the final approved storyboard.
        storyboard_path = artifacts.presentation / "storyboard.json"
        storyboard = (
            EditorialStoryboard.model_validate(
                json.loads(storyboard_path.read_text(encoding="utf-8"))
            )
            if storyboard_path.exists()
            else None
        )
        scenes = build_scene_plan(trace, storyboard=storyboard)
        scene_report = inspect_scene_plan(trace, scenes)
        if scene_report["hard_failures"]:
            raise RuntimeError(f"Scene-plan repair rejected trace: {scene_report['hard_failures']}")
        viewport = ViewportDecision.model_validate(
            json.loads(
                (artifacts.root / "discovery" / "viewport-decision.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        presentation = build_presentation_plan(
            trace,
            viewport_width=viewport.viewport.width,
            viewport_height=viewport.viewport.height,
            allow_camera_zoom=True,
            scene_plan=scenes,
        )
        journey = build_journey(trace, scenes)
        journey_report = inspect_journey(journey)
        if journey_report["hard_failures"]:
            raise RuntimeError(f"Journey repair rejected trace: {journey_report['hard_failures']}")
        artifacts.write_json("presentation/scene-plan.json", scenes)
        artifacts.write_json(
            "presentation/presentation-plan.json", presentation.model_dump(mode="json")
        )
        artifacts.write_json("presentation/validated-scene-plan.json", journey)
        artifacts.write_json("presentation/cursor-plan.json", {"paths": presentation.cursor_paths})
        artifacts.write_json("qa/journey-report.json", journey_report)
    else:
        presentation = PresentationPlan.model_validate(
            json.loads(
                (artifacts.presentation / "presentation-plan.json").read_text(encoding="utf-8")
            )
        )
    # Always pass the validated scene contract to Remotion.  The old
    # trace-only path rendered with an empty ``scenes`` array, so captions
    # fell back to the bottom of the frame even after a scene-level safety
    # repair.  Prefer the validated plan, and fall back to the freshly
    # materialized scene plan only for legacy artifacts.
    scenes_path = artifacts.presentation / "validated-scene-plan.json"
    if scenes_path.exists():
        scenes = json.loads(scenes_path.read_text(encoding="utf-8"))
    else:
        scene_plan_path = artifacts.presentation / "scene-plan.json"
        scenes = (
            json.loads(scene_plan_path.read_text(encoding="utf-8"))
            if scene_plan_path.exists()
            else build_scene_plan(trace)
        )
    if not isinstance(scenes, list):
        raise TypeError("validated scene plan must be a list")
    storyboard_path = artifacts.presentation / "storyboard.json"
    storyboard = (
        EditorialStoryboard.model_validate(json.loads(storyboard_path.read_text(encoding="utf-8")))
        if storyboard_path.exists()
        else None
    )
    # A narration-only repair writes a new authoritative caption timeline.
    # Read it after that repair rather than accidentally rendering stale copy.
    captions = json.loads((artifacts.presentation / "captions.json").read_text(encoding="utf-8"))
    # Rerenders must preserve the validated duration envelope from the plan.
    # Previously this standalone repair path passed only the CLI override,
    # allowing an overlong native capture to bypass the same editorial bounds
    # enforced by the worker render stage.
    plan_payload: dict[str, object] = {}
    for plan_path in (artifacts.root / "plan.json", artifacts.root / "planning" / "plan.json"):
        if plan_path.exists():
            try:
                loaded = json.loads(plan_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                plan_payload = loaded
                break
    target_duration = args.target_duration_seconds
    if target_duration is None and isinstance(
        plan_payload.get("target_duration_seconds"), (int, float)
    ):
        target_duration = int(plan_payload["target_duration_seconds"])
    maximum_duration = plan_payload.get("maximum_duration_seconds")
    if not isinstance(maximum_duration, (int, float)):
        maximum_duration = None
    output = artifacts.root / "final" / "demo.mp4"
    if not args.verify_only:
        output = render_remotion(
            trace,
            presentation,
            artifacts,
            narration_path=narration if narration.exists() else None,
            captions=captions,
            scenes=scenes,
            target_duration_seconds=target_duration,
            maximum_duration_seconds=(
                int(maximum_duration) if maximum_duration is not None else None
            ),
            storyboard=storyboard,
        )
    if args.verify or args.verify_only:
        # QA reads only retained trace/presentation evidence and the local MP4.
        # It never opens a browser or calls a model/provider.
        from productlens.services.generation import UrlGenerationService

        report = UrlGenerationService(None).qa_stage(
            run_id=args.run_id, artifact_root=artifact_root
        )
        if not report["deliverable"]:
            raise RuntimeError(f"Re-render delivery QA rejected output: {report['hard_failures']}")
        # Standalone repairs run outside the worker's stage loop. Persist the
        # same terminal state the normal VIDEO_QA worker would write so resume,
        # cancellation, and API status views remain consistent with artifacts.
        from productlens.services.runtime import build_job_service

        repository, _ = build_job_service()
        repository.update_run(args.run_id, stage="COMPLETE", status="COMPLETE")
    print(output)


if __name__ == "__main__":
    main()
