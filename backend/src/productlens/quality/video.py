from __future__ import annotations

import json
import re
import subprocess
from itertools import pairwise
from pathlib import Path
from statistics import fmean, median


def inspect_video(
    video: Path,
    *,
    execution_verified: bool,
    minimum_duration_seconds: float | None = None,
    maximum_duration_seconds: float | None = None,
    source_video: Path | None = None,
    source_start_offset_seconds: float = 1.5,
) -> dict:
    """Inspect a delivery, including the approved editorial duration envelope.

    Duration bounds belong to the objective/validated plan rather than the
    encoder.  This lets the renderer keep browser evidence at native speed
    while still rejecting a journey that is too brief to explain its subject or
    too long because it contains unedited loading/dead-time footage.
    """
    hard_failures: list[str] = []
    warnings: list[str] = []
    black_duration = 0.0
    frozen_duration = 0.0
    sampled_frames: list[dict[str, float]] = []
    source_faithfulness: list[dict[str, float]] = []
    timestamp_pacing: dict[str, float] | None = None
    if not execution_verified:
        hard_failures.append("WORKFLOW_OUTCOME_UNVERIFIED")
    if not video.exists() or video.stat().st_size < 10_000:
        hard_failures.append("MISSING_OR_EMPTY_RENDER")
    probe: dict = {}
    if not hard_failures:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration,bit_rate:stream=codec_type,width,height,avg_frame_rate,r_frame_rate,nb_frames",
                "-of",
                "json",
                str(video),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        probe = json.loads(completed.stdout or "{}")
        video_streams = [
            item for item in probe.get("streams", []) if item.get("codec_type") == "video"
        ]
        if not video_streams:
            hard_failures.append("NO_VIDEO_STREAM")
        elif video_streams[0].get("width", 0) < 1280 or video_streams[0].get("height", 0) < 720:
            hard_failures.append("INSUFFICIENT_RENDER_RESOLUTION")
        elif source_video is not None and source_video.is_file():
            # A presentation may be reframed, but it must retain the complete
            # browser-frame aspect ratio. A different ratio means the browser
            # frame has been cropped or composed on the wrong canvas.
            source_probe = _probe_video(source_video)
            source_stream = next(
                (item for item in source_probe.get("streams", []) if item.get("codec_type") == "video"),
                None,
            )
            if source_stream:
                source_width = float(source_stream.get("width") or 0)
                source_height = float(source_stream.get("height") or 0)
                rendered_width = float(video_streams[0].get("width") or 0)
                rendered_height = float(video_streams[0].get("height") or 0)
                if (
                    source_width and source_height and rendered_width and rendered_height
                    and abs((source_width / source_height) - (rendered_width / rendered_height)) / (source_width / source_height) > 0.03
                ):
                    # A standard 16:9 delivery may preserve a 16:10 browser
                    # source via letterboxing. Aspect-ratio inequality alone
                    # proves neither crop nor distortion; structural sampling
                    # below is the delivery-blocking evidence for source
                    # faithfulness.
                    warnings.append("SOURCE_FRAME_LETTERBOX_OR_COMPOSITION_REVIEW_REQUIRED")
        duration = float(probe.get("format", {}).get("duration") or 0)
        if duration < 5:
            hard_failures.append("RENDER_TOO_SHORT")
        if duration > 600:
            hard_failures.append("RENDER_TOO_LONG")
        if minimum_duration_seconds is not None and duration + 0.25 < minimum_duration_seconds:
            hard_failures.append("RENDER_BELOW_OBJECTIVE_MINIMUM_DURATION")
        if maximum_duration_seconds is not None and duration - 0.25 > maximum_duration_seconds:
            hard_failures.append("RENDER_EXCEEDS_OBJECTIVE_MAXIMUM_DURATION")
        bit_rate = int(probe.get("format", {}).get("bit_rate") or 0)
        # A near-empty encoding is still a strong signal of a broken delivery.
        # Higher visual quality is controlled by the renderer's CRF; static UI
        # legitimately compresses below a fixed multi-megabit bitrate.
        if bit_rate and bit_rate < 350_000:
            hard_failures.append("INSUFFICIENT_RENDER_BITRATE")
        pacing = {"cadence_error_ratio": 1.0, "expected_frames": 0.0, "observed_frames": 0.0}
        frame_rate = str(video_streams[0].get("avg_frame_rate", "0/1")) if video_streams else "0/1"
        try:
            numerator, denominator = frame_rate.split("/", maxsplit=1)
            measured_rate = float(numerator) / max(1.0, float(denominator))
            if measured_rate < 30:
                hard_failures.append("INSUFFICIENT_RENDER_FRAME_RATE")
            if video_streams:
                pacing = _frame_pacing(video_streams[0], duration, measured_rate)
            if pacing["cadence_error_ratio"] > 0.10:
                hard_failures.append("MATERIAL_FRAME_PACING_DEGRADATION")
            timestamp_pacing = _timestamp_pacing(video, measured_rate)
            if timestamp_pacing["material_gap_count"] or timestamp_pacing["max_gap_seconds"] > 1.0:
                hard_failures.append("MATERIAL_FRAME_TIMESTAMP_GAPS")
        except (ValueError, ZeroDivisionError):
            hard_failures.append("UNKNOWN_RENDER_FRAME_RATE")
            pacing = {"cadence_error_ratio": 1.0, "expected_frames": 0.0, "observed_frames": 0.0}
        blackdetect = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-i",
                str(video),
                "-vf",
                "blackdetect=d=0.4:pix_th=0.1",
                "-an",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        black_duration = sum(
            float(value) for value in re.findall(r"black_duration:([0-9.]+)", blackdetect.stderr)
        )
        if black_duration > max(1.0, duration * 0.15):
            hard_failures.append("EXCESSIVE_BLACK_VIDEO")
        freeze = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-i",
                str(video),
                "-vf",
                "freezedetect=n=0.001:d=3",
                "-an",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        frozen_duration = sum(
            float(value) for value in re.findall(r"freeze_duration:([0-9.]+)", freeze.stderr)
        )
        if frozen_duration > max(3.0, duration * 0.2):
            hard_failures.append("EXCESSIVE_FROZEN_VIDEO")
        sampled_frames = _sample_frame_quality(video, duration)
        opening_sample = next((item for item in sampled_frames if item.get("second") == 2.2), None)
        # The branded title ends before this point. A near-uniform white,
        # black, or loading canvas at the first product frame is not an
        # established opening, even if the remainder of the video has detail.
        if opening_sample and opening_sample["variance"] < 8.0:
            hard_failures.append("UNESTABLISHED_OR_BLANK_OPENING_FRAME")
        # A technically valid MP4 can still be a loading shell or solid canvas.
        # Require visual structure in at least one sampled presentation frame.
        if sampled_frames and all(item["variance"] < 6.0 for item in sampled_frames):
            hard_failures.append("VISUALLY_EMPTY_RENDER")
        if source_video is not None and source_video.is_file():
            source_faithfulness = _source_faithfulness(
                delivery=video,
                source=source_video,
                duration=duration,
                source_start_offset_seconds=source_start_offset_seconds,
            )
            # Reframing lowers pixel correlation, but a render with no
            # recognisable relationship to any browser evidence is not a
            # source-faithful product demo and cannot be delivered.
            correlations = [item["correlation"] for item in source_faithfulness]
            if correlations and max(correlations) < 0.25:
                hard_failures.append("SOURCE_FOOTAGE_STRUCTURALLY_UNRELATED")
            # One matching frame is not sufficient evidence of a faithful
            # product video: an intro/outro or a single lucky static frame can
            # otherwise hide crop, timing drift, or a mostly unrelated render.
            # The majority of sampled evidence must remain recognisable.
            elif correlations and (
                # Camera-aware structural comparison is intentionally stricter
                # than a loose visual-presence check, while allowing the
                # bounded target crop used by the director. Values below .45
                # were empirically unrelated/misaligned browser states; a
                # majority above it preserves source-faithfulness proof.
                median(correlations) < 0.45
                or sum(value >= 0.45 for value in correlations) < (len(correlations) + 1) // 2
            ):
                hard_failures.append("SOURCE_FOOTAGE_LOW_STRUCTURAL_SIMILARITY")
    return {
        "execution_score": 1.0 if execution_verified else 0.0,
        "visual_score": 1.0 if not hard_failures else 0.0,
        "overall_score": 1.0 if not hard_failures else 0.0,
        "hard_failures": hard_failures,
        "probe": probe,
        "black_duration_seconds": black_duration if probe else None,
        "frozen_duration_seconds": frozen_duration if probe else None,
        "duration_bounds_seconds": {
            "minimum": minimum_duration_seconds,
            "maximum": maximum_duration_seconds,
        },
        "sampled_frame_quality": sampled_frames,
        "source_faithfulness": source_faithfulness,
        "frame_pacing": pacing if probe else None,
        "timestamp_pacing": timestamp_pacing,
        "warnings": warnings,
    }


def _probe_video(video: Path) -> dict:
    """Read source dimensions without allowing a bad probe to crash QA."""
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height", "-of", "json", str(video)],
        capture_output=True, text=True, check=False,
    )
    try:
        return json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def _frame_pacing(stream: dict, duration: float, measured_rate: float) -> dict[str, float]:
    """Compare encoded frame count with the stream's declared cadence.

    This catches accidental timebase/stretch errors that can make a render feel
    laggy even when its nominal FPS passes. Missing frame counts are reported as
    unknown rather than guessed, preserving compatibility with older encoders.
    """
    try:
        observed = float(stream.get("nb_frames") or 0)
    except (TypeError, ValueError):
        observed = 0.0
    expected = max(0.0, duration * measured_rate)
    if not observed or not expected:
        return {"cadence_error_ratio": 0.0, "expected_frames": expected, "observed_frames": observed}
    return {
        "cadence_error_ratio": round(abs(observed - expected) / expected, 4),
        "expected_frames": round(expected, 2),
        "observed_frames": observed,
    }


def _timestamp_pacing(video: Path, measured_rate: float) -> dict[str, float]:
    """Detect real timestamp gaps that nominal FPS and frame count hide.

    Variable-frame-rate media is valid. A long interval between decoded frames
    is not: the viewer experiences it as a freeze or lag even when the stream
    advertises a high frame rate.
    """
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "frame=best_effort_timestamp_time", "-of", "csv=p=0", str(video),
        ],
        capture_output=True, text=True, check=False,
    )
    timestamps: list[float] = []
    for line in completed.stdout.splitlines():
        try:
            timestamps.append(float(line.strip().split(",")[0]))
        except (TypeError, ValueError):
            continue
    if len(timestamps) < 2:
        return {"max_gap_seconds": 0.0, "material_gap_count": 0.0, "sample_count": float(len(timestamps))}
    material_gap = max(0.20, 5.0 / max(measured_rate, 1.0))
    gaps = [later - earlier for earlier, later in pairwise(timestamps)]
    return {
        "max_gap_seconds": round(max(gaps), 4),
        "material_gap_count": float(sum(gap > material_gap for gap in gaps)),
        "sample_count": float(len(timestamps)),
    }


def _sample_frame_quality(video: Path, duration: float) -> list[dict[str, float]]:
    """Measure coarse grayscale structure at early, middle, and late points.

    This deterministic fallback cannot understand product semantics, but it
    catches all-black/all-white/loading-shell renders without paid vision calls.
    """
    points = sorted({min(max(0.2, duration - 0.2), 2.2), *(
        max(0.2, duration * fraction) for fraction in (0.2, 0.5, 0.8)
    )})
    metrics: list[dict[str, float]] = []
    for second in points:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{second:.3f}",
                "-i", str(video), "-frames:v", "1", "-vf", "scale=64:36,format=gray",
                "-f", "rawvideo", "-",
            ],
            capture_output=True,
            check=False,
        )
        values = list(result.stdout)
        if len(values) != 64 * 36:
            continue
        average = fmean(values)
        variance = fmean((value - average) ** 2 for value in values)
        metrics.append({"second": round(second, 3), "mean_luma": round(average, 2), "variance": round(variance, 2)})
    return metrics


def _frame_values(video: Path, second: float) -> list[int]:
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{second:.3f}",
            "-i", str(video), "-frames:v", "1", "-vf", "scale=64:36,format=gray",
            "-f", "rawvideo", "-",
        ],
        capture_output=True,
        check=False,
    )
    values = list(result.stdout)
    return values if len(values) == 64 * 36 else []


def _source_faithfulness(
    *, delivery: Path, source: Path, duration: float, source_start_offset_seconds: float
) -> list[dict[str, float]]:
    """Compare low-resolution luminance structure at corresponding moments."""
    metrics: list[dict[str, float]] = []
    for second in sorted({max(2.0, duration * fraction) for fraction in (0.2, 0.5, 0.8)}):
        rendered = _frame_values(delivery, second)
        evidence = _frame_values(source, max(0.0, second - source_start_offset_seconds))
        if not rendered or not evidence:
            continue
        metrics.append({
            "second": round(second, 3),
            # Presentation is permitted to use a small, bounded camera focus.
            # Compare against its source at native scale and restrained crop
            # candidates so a faithful target-focused frame is not mistaken
            # for unrelated footage merely because it is not pixel-identical.
            "correlation": round(_best_structural_correlation(rendered, evidence), 3),
        })
    return metrics


def _best_structural_correlation(rendered: list[int], source: list[int]) -> float:
    """Compare source geometry at native scale and bounded camera crops."""
    best = _correlation(rendered, source)
    for zoom in (1.06, 1.12, 1.18):
        crop_width = max(8, round(64 / zoom))
        crop_height = max(8, round(36 / zoom))
        stride = 2
        for top in range(0, 36 - crop_height + 1, stride):
            for left in range(0, 64 - crop_width + 1, stride):
                crop = [
                    source[(top + min(crop_height - 1, round(row * (crop_height - 1) / 35))) * 64
                           + left + min(crop_width - 1, round(column * (crop_width - 1) / 63))]
                    for row in range(36) for column in range(64)
                ]
                best = max(best, _correlation(rendered, crop))
    return best


def _correlation(left: list[int], right: list[int]) -> float:
    left_mean, right_mean = fmean(left), fmean(right)
    numerator = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left, right, strict=True)
    )
    denominator = (
        sum((value - left_mean) ** 2 for value in left)
        * sum((value - right_mean) ** 2 for value in right)
    ) ** 0.5
    return numerator / denominator if denominator else 0.0
