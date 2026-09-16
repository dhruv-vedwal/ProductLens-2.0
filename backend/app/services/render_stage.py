"""Render-stage orchestration isolated from browser generation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, cast

from app.artifacts.store import RunArtifacts
from app.contracts.models import DemoPlan, DemoTrace, EditorialStoryboard, PresentationPlan
from app.presentation.scenes import build_scene_plan
from app.quality.repair import classify_repair
from app.services.generation_policy import production_duration_envelope, safe_render_error
from app.video.render import CaptureDurationError, render_remotion


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def render_run(*, run_id: str, artifact_root: Path) -> Path:
    """Render one already-approved trace without owning generation concerns."""
    artifacts = RunArtifacts(artifact_root, run_id)
    trace = DemoTrace.model_validate(_load_json(artifacts.execution / "trace.json"))
    plan = DemoPlan.model_validate(_load_json(artifacts.root / "plan.json"))
    presentation = PresentationPlan.model_validate(
        _load_json(artifacts.presentation / "presentation-plan.json")
    )
    captions = cast(list[dict[str, Any]], _load_json(artifacts.presentation / "captions.json"))
    scenes_path = artifacts.presentation / "validated-scene-plan.json"
    scenes = (
        cast(list[dict[str, Any]], _load_json(scenes_path))
        if scenes_path.exists()
        else build_scene_plan(trace)
    )
    storyboard_path = artifacts.presentation / "storyboard.json"
    storyboard = (
        EditorialStoryboard.model_validate(_load_json(storyboard_path))
        if storyboard_path.exists()
        else None
    )
    effective_maximum, duration_accounting = production_duration_envelope(plan)
    if duration_accounting:
        artifacts.write_json("presentation/duration-accounting.json", duration_accounting)
    audio = artifacts.root / "audio" / "narration.mp3"
    artifacts.write_json(
        "render/status.json",
        {
            "status": "RUNNING",
            "run_id": run_id,
            "target_duration_seconds": plan.target_duration_seconds,
            "started_at": datetime.now(UTC).isoformat(),
            "resume_from": "execution/trace.json",
            "candidate_location": str(artifacts.root / "render" / "demo.candidate.mp4"),
        },
    )
    started = perf_counter()
    try:
        output = render_remotion(
            trace,
            presentation,
            artifacts,
            narration_path=audio if audio.exists() else None,
            captions=captions,
            scenes=scenes,
            target_duration_seconds=plan.target_duration_seconds,
            maximum_duration_seconds=effective_maximum,
            storyboard=storyboard,
        )
    except Exception as error:
        failure = (
            "CAPTURE_DURATION_EXCEEDS_OBJECTIVE_MAXIMUM"
            if isinstance(error, CaptureDurationError)
            else type(error).__name__
        )
        artifacts.write_json(
            "render/status.json",
            {
                "status": "FAILED",
                "run_id": run_id,
                "error_code": failure,
                "error_message": safe_render_error(str(error)),
            },
        )
        artifacts.write_json(
            "qa/repair-decision.json",
            classify_repair([failure]).model_dump(mode="json"),
        )
        raise
    artifacts.write_json(
        "render/status.json",
        {
            "status": "COMPLETE",
            "run_id": run_id,
            "location": str(output),
            "duration_ms": int((perf_counter() - started) * 1000),
            "completed_at": datetime.now(UTC).isoformat(),
        },
    )
    return output
