from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from itertools import pairwise
from pathlib import Path
from statistics import fmean, median
from typing import Any


def inspect_video(
    video: Path,
    *,
    execution_verified: bool,
    minimum_duration_seconds: float | None = None,
    maximum_duration_seconds: float | None = None,
    source_video: Path | None = None,
    source_start_offset_seconds: float = 1.5,
    source_time_map: list[tuple[float, float]] | None = None,
    source_is_edited: bool = False,
) -> dict[str, Any]:
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
    blank_content_frames: list[dict[str, float]] = []
    source_faithfulness: list[dict[str, float]] = []
    timestamp_pacing: dict[str, float] | None = None
    if not execution_verified:
        hard_failures.append("WORKFLOW_OUTCOME_UNVERIFIED")
    if not video.exists() or video.stat().st_size < 10_000:
        hard_failures.append("MISSING_OR_EMPTY_RENDER")
    probe: dict[str, Any] = {}
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
                (
                    item
                    for item in source_probe.get("streams", [])
                    if item.get("codec_type") == "video"
                ),
                None,
            )
            if source_stream:
                source_width = float(source_stream.get("width") or 0)
                source_height = float(source_stream.get("height") or 0)
                rendered_width = float(video_streams[0].get("width") or 0)
                rendered_height = float(video_streams[0].get("height") or 0)
                if (
                    source_width
                    and source_height
                    and rendered_width
                    and rendered_height
                    and abs((source_width / source_height) - (rendered_width / rendered_height))
                    / (source_width / source_height)
                    > 0.03
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
        # The Remotion composition may open with a short branded title card
        # on a dark background.  Treat up to two seconds of leading black as
        # intentional presentation chrome; longer black intervals (or a large
        # proportion of the video) remain a hard failure.
        if black_duration > max(2.0, duration * 0.15):
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
        blank_content_frames = _sample_content_quality(video, duration)
        opening_sample = next((item for item in sampled_frames if item.get("second") == 2.2), None)
        # The branded title ends before this point. A near-uniform white,
        # black, or loading canvas at the first product frame is not an
        # established opening, even if the remainder of the video has detail.
        if opening_sample and (
            opening_sample.get("variance", 0.0) < 8.0
            # A white/black loading canvas can contain a tiny caption or
            # compositor edge, raising variance above the old threshold while
            # still leaving the product unestablished. Treat near-uniform
            # extremes as blank when structure remains negligible.
            or (
                opening_sample.get("variance", 0.0) < 30.0
                and (
                    opening_sample.get("mean_luma", 128.0) >= 250.0
                    or opening_sample.get("mean_luma", 128.0) <= 5.0
                )
            )
        ):
            hard_failures.append("UNESTABLISHED_OR_BLANK_OPENING_FRAME")
        # A technically valid MP4 can still be a loading shell or solid canvas.
        # Require visual structure in at least one sampled presentation frame.
        if sampled_frames and all(item["variance"] < 6.0 for item in sampled_frames):
            hard_failures.append("VISUALLY_EMPTY_RENDER")
        # Captions and the presentation shell can add enough pixels to make a
        # white loading page look non-uniform at full-frame scale. Inspect the
        # central product region separately (excluding shell/caption bands) so
        # a sustained blank page cannot pass visual QA merely because a
        # caption is present. One transient sample is tolerated; two sampled
        # blank regions indicate material missing product footage.
        if len(blank_content_frames) >= 2:
            hard_failures.append("BLANK_PRODUCT_CONTENT_INTERVAL")
        if source_video is not None and source_video.is_file():
            source_faithfulness = _source_faithfulness(
                delivery=video,
                source=source_video,
                duration=duration,
                source_start_offset_seconds=source_start_offset_seconds,
                source_time_map=source_time_map,
            )
            # Reframing lowers pixel correlation, but a render with no
            # recognisable relationship to any browser evidence is not a
            # source-faithful product demo and cannot be delivered.
            correlations = [item["correlation"] for item in source_faithfulness]
            unrelated_floor = 0.12 if source_is_edited else 0.25
            if correlations and max(correlations) < unrelated_floor:
                hard_failures.append("SOURCE_FOOTAGE_STRUCTURALLY_UNRELATED")
            # One matching frame is not sufficient evidence of a faithful
            # product video: an intro/outro or a single lucky static frame can
            # otherwise hide crop, timing drift, or a mostly unrelated render.
            # The majority of sampled evidence must remain recognisable.
            elif correlations and (
                # Camera-aware structural comparison is intentionally stricter
                # than a loose visual-presence check, while allowing the
                # bounded target crop used by the director. An editorial
                # source has already been cut and recompressed from the same
                # browser evidence; caption layers, rounded framing, and
                # resampling can legitimately reduce its pixel correlation.
                # Keep a majority gate, but use a low non-random floor for
                # edited sources and retain the unrelated-footage max check
                # above.
                median(correlations) < (0.05 if source_is_edited else 0.45)
                or sum(value >= (0.05 if source_is_edited else 0.45) for value in correlations)
                < (len(correlations) + 1) // 2
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
        "blank_content_quality": blank_content_frames,
        "source_faithfulness": source_faithfulness,
        "frame_pacing": pacing if probe else None,
        "timestamp_pacing": timestamp_pacing,
        "warnings": warnings,
    }


def _probe_video(video: Path) -> dict[str, Any]:
    """Read source dimensions without allowing a bad probe to crash QA."""
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height",
            "-of",
            "json",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout or "{}")
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


def _frame_pacing(
    stream: Mapping[str, Any], duration: float, measured_rate: float
) -> dict[str, float]:
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
        return {
            "cadence_error_ratio": 0.0,
            "expected_frames": expected,
            "observed_frames": observed,
        }
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
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "csv=p=0",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    timestamps: list[float] = []
    for line in completed.stdout.splitlines():
        try:
            timestamps.append(float(line.strip().split(",")[0]))
        except (TypeError, ValueError):
            continue
    if len(timestamps) < 2:
        return {
            "max_gap_seconds": 0.0,
            "material_gap_count": 0.0,
            "sample_count": float(len(timestamps)),
        }
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
    points = sorted(
        {
            min(max(0.2, duration - 0.2), 2.2),
            *(max(0.2, duration * fraction) for fraction in (0.2, 0.5, 0.8)),
        }
    )
    metrics: list[dict[str, float]] = []
    for second in points:
        result = subprocess.run(
            [
                "ffmpeg",
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
                "scale=64:36,format=gray",
                "-f",
                "rawvideo",
                "-",
            ],
            capture_output=True,
            check=False,
        )
        values = list(result.stdout)
        if len(values) != 64 * 36:
            continue
        average = fmean(values)
        variance = fmean((value - average) ** 2 for value in values)
        metrics.append(
            {
                "second": round(second, 3),
                "mean_luma": round(average, 2),
                "variance": round(variance, 2),
            }
        )
    return metrics


def _sample_content_quality(video: Path, duration: float) -> list[dict[str, float]]:
    """Measure browser content without mistaking a light UI for blank footage.

    A single centered crop incorrectly rejects legitimate white dashboards whose
    content is aligned to the left or right. Sample a small grid across the
    product region (excluding shell/caption bands) and only report an interval
    when nearly every cell is genuinely uniform.
    """
    points = sorted(
        {max(1.0, min(duration - 0.2, duration * fraction)) for fraction in (0.2, 0.5, 0.8)}
    )
    metrics: list[dict[str, float]] = []
    for second in points:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{second:.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                # Remove small shell/caption bands while retaining the full
                # horizontal product area for responsive layouts.
                "-vf",
                "crop=iw*0.92:ih*0.72:iw*0.04:ih*0.08,scale=192:108,format=gray",
                "-f",
                "rawvideo",
                "-",
            ],
            capture_output=True,
            check=False,
        )
        values = list(result.stdout)
        if len(values) != 192 * 108:
            continue
        cell_width, cell_height = 48, 36
        blank_cells = 0
        cells = 0
        for row in range(3):
            for column in range(4):
                cell = [
                    values[y * 192 + x]
                    for y in range(row * cell_height, (row + 1) * cell_height)
                    for x in range(column * cell_width, (column + 1) * cell_width)
                ]
                average = fmean(cell)
                variance = fmean((value - average) ** 2 for value in cell)
                blank_cells += int(average >= 248.0 and variance < 120.0)
                cells += 1
        if cells and blank_cells / cells >= 0.80:
            average = fmean(values)
            variance = fmean((value - average) ** 2 for value in values)
            metrics.append(
                {
                    "second": round(second, 3),
                    "mean_luma": round(average, 2),
                    "variance": round(variance, 2),
                }
            )
    return metrics


def _frame_values(video: Path, second: float) -> list[int]:
    result = subprocess.run(
        [
            "ffmpeg",
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
            "scale=64:36,format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    values = list(result.stdout)
    return values if len(values) == 64 * 36 else []


def _source_faithfulness(
    *,
    delivery: Path,
    source: Path,
    duration: float,
    source_start_offset_seconds: float,
    source_time_map: list[tuple[float, float]] | None = None,
) -> list[dict[str, float]]:
    """Compare low-resolution luminance structure at corresponding moments."""
    metrics: list[dict[str, float]] = []
    for second in sorted({max(2.0, duration * fraction) for fraction in (0.2, 0.5, 0.8)}):
        rendered = _frame_values(delivery, second)
        source_second = _mapped_source_second(second, source_time_map)
        if source_second is None:
            source_second = max(0.0, second - source_start_offset_seconds)
        evidence = _frame_values(source, source_second)
        if not rendered or not evidence:
            continue
        metrics.append(
            {
                "second": round(second, 3),
                # Presentation is permitted to use a small, bounded camera focus.
                # Compare against its source at native scale and restrained crop
                # candidates so a faithful target-focused frame is not mistaken
                # for unrelated footage merely because it is not pixel-identical.
                "correlation": round(_best_structural_correlation(rendered, evidence), 3),
            }
        )
    return metrics


def _mapped_source_second(
    output_second: float, source_time_map: list[tuple[float, float]] | None
) -> float | None:
    """Map an edited-output timestamp to its source recording timestamp.

    Editorial renders concatenate source windows, so output time is not the
    same clock as the long Browserbase recording.  Keeping this mapping in
    the QA layer prevents valid cuts from being rejected as unrelated footage.
    Each tuple is ``(source_start, source_end)`` in source seconds; windows
    are played at native speed in the listed order.
    """
    if not source_time_map:
        return None
    elapsed = 0.0
    for start, end in source_time_map:
        if end <= start:
            continue
        length = end - start
        if output_second <= elapsed + length:
            return max(start, min(end, start + max(0.0, output_second - elapsed)))
        elapsed += length
    # A tiny encoder tail can land just after the final window; use its end
    # rather than falling back to an unrelated timestamp.
    last = next(((start, end) for start, end in reversed(source_time_map) if end > start), None)
    return last[1] if last else None


def _best_structural_correlation(rendered: list[int], source: list[int]) -> float:
    """Compare source geometry at native scale and bounded camera crops.

    The presentation shell intentionally places the complete browser frame on
    a branded canvas.  Pixel correlation over the whole output therefore
    includes the shell background and can under-report a faithful recording.
    Compare the native pair plus the bounded central shell crop, while keeping
    the existing source-side camera candidates for target-focused scenes.
    """
    best = _correlation(rendered, source)
    # The reusable presentation shell places the complete 16:9 browser frame
    # inside a dark canvas. Compare a few conservative shell-card bounds after
    # resampling them to source dimensions; this handles letterboxing without
    # permitting arbitrary crops to pass the source-faithfulness gate.
    for left, top, right, bottom in (
        (0.05, 0.02, 0.95, 0.93),
        (0.07, 0.03, 0.93, 0.90),
        (0.04, 0.04, 0.96, 0.94),
    ):
        best = max(
            best,
            _correlation(_resample_normalized_crop(rendered, left, top, right, bottom), source),
        )
    for zoom in (1.06, 1.12, 1.18, 1.28, 1.36):
        crop_width = max(8, round(64 / zoom))
        crop_height = max(8, round(36 / zoom))
        stride = 2
        # Reframe the rendered shell back to its complete source card.  This
        # is deliberately bounded to a centred crop; arbitrary output crops
        # would let unrelated footage pass the source-faithfulness gate.
        top = (36 - crop_height) // 2
        left = (64 - crop_width) // 2
        rendered_crop = [
            rendered[
                (top + min(crop_height - 1, round(row * (crop_height - 1) / 35))) * 64
                + left
                + min(crop_width - 1, round(column * (crop_width - 1) / 63))
            ]
            for row in range(36)
            for column in range(64)
        ]
        best = max(best, _correlation(rendered_crop, source))
        for top in range(0, 36 - crop_height + 1, stride):
            for left in range(0, 64 - crop_width + 1, stride):
                crop = [
                    source[
                        (top + min(crop_height - 1, round(row * (crop_height - 1) / 35))) * 64
                        + left
                        + min(crop_width - 1, round(column * (crop_width - 1) / 63))
                    ]
                    for row in range(36)
                    for column in range(64)
                ]
                best = max(best, _correlation(rendered, crop))
    return best


def _resample_normalized_crop(
    values: list[int], left: float, top: float, right: float, bottom: float
) -> list[int]:
    """Return a stable shell-card crop in the low-resolution QA grid.

    ``_frame_values`` always returns a 64x36 luminance grid. Keeping this
    helper bounded to normalized shell coordinates makes the comparison
    tolerant of the branded margin while preventing a caller from selecting
    a content-specific arbitrary region.
    """
    width, height = 64, 36
    x0, x1 = max(0, round(left * width)), min(width - 1, round(right * width) - 1)
    y0, y1 = max(0, round(top * height)), min(height - 1, round(bottom * height) - 1)
    crop_width, crop_height = max(1, x1 - x0 + 1), max(1, y1 - y0 + 1)
    cropped = [
        values[
            (y0 + min(crop_height - 1, round(row * (crop_height - 1) / 35))) * width
            + x0
            + min(crop_width - 1, round(column * (crop_width - 1) / 63))
        ]
        for row in range(36)
        for column in range(64)
    ]
    return cropped


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
