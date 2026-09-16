"""Evidence-backed comparison against the supplied presentation references.

This deliberately measures only what encoded media can prove.  Narrative and
editorial expectations are explicit rubric criteria reviewed alongside the
measured profile; the tool never pretends that a frame-rate probe can judge a
human-quality story.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from statistics import median
from typing import Any

MEDIA_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv"}
BENCHMARK_VERSION = "sample-video-quality-v1"

# These are the editorial properties visibly required by the supplied samples.
# They are intentionally generic and become delivery-review questions, not
# product-specific prompts or routes.
EDITORIAL_RUBRIC = {
    "opening": "A concise presenter-led welcome establishes the product and the viewer journey before the first transition.",
    "story": "Each section explains visible evidence, why it matters, and the next takeaway; labels alone do not pass.",
    "motion": "Scrolls, cursor moves, and focus changes are continuous, purposeful, and settle before narration advances.",
    "composition": "The complete product frame stays readable; focus treatment never hides meaningful context.",
    "captions": "Captions are concise, high contrast, scene-aligned, and positioned away from active evidence.",
    "closing": "The walkthrough ends with a clear outcome, summary, or next step rather than an unexplained stop.",
}


def _fraction(value: str | float | None) -> float:
    if value is None:
        return 0.0
    try:
        numerator, denominator = str(value).split("/", maxsplit=1)
        return float(numerator) / max(1.0, float(denominator))
    except (ValueError, ZeroDivisionError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


def probe_video(video: Path) -> dict[str, Any]:
    """Return stable, non-semantic encoded-media evidence for one video."""
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,bit_rate:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,nb_frames",
            "-of",
            "json",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"could not probe {video.name}: {completed.stderr.strip()}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"invalid ffprobe response for {video.name}") from error
    stream = next(
        (item for item in payload.get("streams", []) if item.get("codec_type") == "video"), None
    )
    if not isinstance(stream, dict):
        raise TypeError(f"no video stream in {video.name}")
    return {
        "file": video.name,
        "codec": str(stream.get("codec_name") or "unknown"),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "duration_seconds": round(float(payload.get("format", {}).get("duration") or 0), 3),
        "frame_rate": round(
            _fraction(stream.get("avg_frame_rate") or stream.get("r_frame_rate")), 3
        ),
        "nominal_frame_rate": round(_fraction(stream.get("r_frame_rate")), 3),
        "frame_count": int(stream.get("nb_frames") or 0),
        "bit_rate": int(payload.get("format", {}).get("bit_rate") or 0),
    }


def build_sample_benchmark(samples_directory: Path) -> dict[str, Any]:
    """Probe supplied samples and derive an honest measurable envelope."""
    videos = sorted(
        path
        for path in samples_directory.iterdir()
        if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES
    )
    if not videos:
        raise FileNotFoundError(f"no sample videos in {samples_directory}")
    profiles = [probe_video(video) for video in videos]
    usable = [item for item in profiles if item["width"] and item["height"] and item["frame_rate"]]
    if not usable:
        raise RuntimeError("sample videos do not contain usable visual profiles")
    durations = sorted(item["duration_seconds"] for item in usable if item["duration_seconds"] > 0)
    widths = [item["width"] for item in usable]
    heights = [item["height"] for item in usable]
    frame_rates = [item["frame_rate"] for item in usable]
    return {
        "benchmark_id": BENCHMARK_VERSION,
        "sample_directory": str(samples_directory),
        "sample_count": len(usable),
        "profiles": profiles,
        "measured_envelope": {
            "minimum_width": min(widths),
            "minimum_height": min(heights),
            "median_width": int(median(widths)),
            "median_height": int(median(heights)),
            "minimum_frame_rate": min(frame_rates),
            "median_frame_rate": round(median(frame_rates), 3),
            "shortest_duration_seconds": min(durations),
            "median_duration_seconds": round(median(durations), 3),
            "longest_duration_seconds": max(durations),
        },
        # A low-resolution social clip must not lower the delivery target for
        # the product-demo references.  Median values resist that outlier
        # while remaining derived entirely from supplied media.
        "quality_target": {
            "width": int(median(widths)),
            "height": int(median(heights)),
            "frame_rate": round(median(frame_rates), 3),
            "minimum_duration_seconds": min(durations),
        },
        "editorial_rubric": EDITORIAL_RUBRIC,
        "limitations": [
            "Encoded-media probes cannot establish narration quality, intent, or semantic coverage.",
            "Editorial rubric items require trace evidence plus human or configured multimodal review.",
        ],
    }


def compare_to_sample_benchmark(video: Path, benchmark: dict[str, Any]) -> dict[str, Any]:
    """Compare an output with the measurable sample envelope without faking equivalence."""
    profile = probe_video(video)
    envelope = (
        benchmark.get("measured_envelope")
        if isinstance(benchmark.get("measured_envelope"), dict)
        else {}
    )
    failures: list[str] = []
    target = (
        benchmark.get("quality_target")
        if isinstance(benchmark.get("quality_target"), dict)
        else envelope
    )
    if profile["width"] < int(target.get("width") or target.get("minimum_width") or 0) or profile[
        "height"
    ] < int(target.get("height") or target.get("minimum_height") or 0):
        failures.append("BELOW_SAMPLE_RESOLUTION_ENVELOPE")
    if profile["frame_rate"] + 0.01 < float(
        target.get("frame_rate") or target.get("minimum_frame_rate") or 0
    ):
        failures.append("BELOW_SAMPLE_FRAME_RATE_ENVELOPE")
    if profile["duration_seconds"] + 0.25 < float(
        target.get("minimum_duration_seconds") or envelope.get("shortest_duration_seconds") or 0
    ):
        failures.append("BELOW_SAMPLE_DURATION_ENVELOPE")
    return {
        "benchmark_id": benchmark.get("benchmark_id"),
        "output_profile": profile,
        "measured_match": not failures,
        "hard_failures": failures,
        "editorial_review_required": list(EDITORIAL_RUBRIC),
        "note": "Measured parity is necessary but not sufficient for sample-quality editorial parity.",
    }


def write_sample_benchmark(samples_directory: Path, output: Path) -> dict[str, Any]:
    report = build_sample_benchmark(samples_directory)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
