"""Provider-neutral visual review boundary.

The deterministic video checks remain authoritative when no vision provider is
configured.  This module defines the stable payload/normalisation boundary for
an optional multimodal reviewer without making delivery depend on a model call.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

VisualReviewer = Callable[[dict[str, Any]], dict[str, Any]]


def build_review_packet(
    *,
    video: Path,
    run_id: str,
    trace: dict[str, Any],
    storyboard: dict[str, Any] | None = None,
    sample_seconds: list[float] | None = None,
) -> dict[str, Any]:
    """Create a non-secret packet that an optional visual model may inspect."""
    if not video.is_file() or video.stat().st_size == 0:
        raise FileNotFoundError(video)
    raw_events: object = trace.get("events")
    events = raw_events if isinstance(raw_events, list) else []
    raw_scenes: object = (storyboard or {}).get("scenes", [])
    scenes = raw_scenes if isinstance(raw_scenes, list) else []
    return {
        "run_id": run_id,
        "video_path": str(video),
        "sample_seconds": [float(value) for value in (sample_seconds or [])],
        "event_count": len(events),
        "scene_count": len(scenes),
        "checks": [
            "frame_composition",
            "readability",
            "motion",
            "cursor_alignment",
            "caption_alignment",
            "story_coherence",
        ],
    }


def extract_review_frames(
    *,
    video: Path,
    output_directory: Path,
    sample_seconds: list[float],
    width: int = 640,
) -> list[dict[str, Any]]:
    """Extract stable PNG evidence for human or provider-neutral review.

    The reviewer gets real rendered frames, rather than an opaque MP4 path.
    This works with local ffmpeg and deliberately does not require a paid
    vision provider. Existing frames are overwritten only for this run's
    supplied output directory.
    """
    if not video.is_file():
        raise FileNotFoundError(video)
    output_directory.mkdir(parents=True, exist_ok=True)
    frames: list[dict[str, Any]] = []
    for index, second in enumerate(sorted({max(0.0, float(value)) for value in sample_seconds})):
        target = output_directory / f"frame-{index:03d}-{second:.3f}s.png"
        completed = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{second:.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-vf",
                f"scale={width}:-2",
                str(target),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0 and target.is_file() and target.stat().st_size:
            frames.append({"second": second, "path": str(target), "bytes": target.stat().st_size})
    return frames


def review_multimodal(
    packet: dict[str, Any],
    reviewer: VisualReviewer | None = None,
) -> dict[str, Any]:
    """Run an external reviewer or an always-available deterministic review.

    Missing provider credentials must never mean that visual QA silently did
    not happen. The local reviewer verifies that the render can be sampled and
    returns the concrete samples for manual inspection; semantic judgement can
    still be augmented by a provider callback.
    """
    video = Path(str(packet["video_path"]))
    seconds = [float(value) for value in packet.get("sample_seconds", [])]
    if not seconds:
        seconds = [2.0, 10.0, 30.0]
    review_root = video.parent / "review-frames"
    frames = extract_review_frames(
        video=video, output_directory=review_root, sample_seconds=seconds
    )
    review_packet = {**packet, "frames": frames}
    if reviewer is None:
        failures = [] if frames else ["RENDERED_FRAME_EXTRACTION_FAILED"]
        return {
            "status": "deterministic_complete",
            "provider": "local-ffmpeg",
            "hard_failures": failures,
            "warnings": ["SEMANTIC_VISUAL_REVIEW_REQUIRES_HUMAN_OR_CONFIGURED_MODEL"],
            "findings": [{"kind": "rendered_frame", **frame} for frame in frames],
            "packet": {
                key: value
                for key, value in review_packet.items()
                if key not in {"video_path", "frames"}
            },
        }
    try:
        result = reviewer(review_packet)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        return {
            "status": "unavailable",
            "provider": "reviewer",
            "hard_failures": [f"MULTIMODAL_REVIEW_UNAVAILABLE:{type(error).__name__}"],
            "warnings": [],
            "findings": [],
        }
    if not isinstance(result, dict):
        raise TypeError("multimodal reviewer must return a mapping")
    failures = [str(item) for item in result.get("hard_failures", [])]
    warnings = [str(item) for item in result.get("warnings", [])]
    unavailable = any("MULTIMODAL_REVIEW_UNAVAILABLE" in item for item in [*failures, *warnings])
    if unavailable and not any("MULTIMODAL_REVIEW_UNAVAILABLE" in item for item in failures):
        failures.append("MULTIMODAL_REVIEW_UNAVAILABLE")
    return {
        "status": "unavailable" if unavailable else "complete",
        "provider": result.get("provider"),
        "hard_failures": failures,
        "warnings": warnings,
        "findings": result.get("findings", []),
    }
