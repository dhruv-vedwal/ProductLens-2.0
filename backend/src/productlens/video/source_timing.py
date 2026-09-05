"""Align Browserbase's downloadable recording to ProductLens browser evidence.

The provider recording is source-faithful but its presentation timestamps are
not guaranteed to share the Playwright/CDP clock.  This module grounds each
trace event in the actual downloaded video before editorial cutting, cursor
placement, captions, or video QA consume it.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from productlens.contracts.models import DemoTrace

_WIDTH, _HEIGHT, _FPS = 32, 18, 2
_FRAME_BYTES = _WIDTH * _HEIGHT
_HASH_BITS = _FRAME_BYTES


def align_trace_to_recording(
    trace: DemoTrace, *, recording: Path, screencast_frames: Path
) -> tuple[DemoTrace, dict[str, object]]:
    """Return a trace whose clock is the downloaded recording's clock.

    The CDP screencast provides timestamped image witnesses at browser-event
    time.  We find their perceptual matches in the native Browserbase MP4 and
    preserve a durable per-event alignment report.  If confidence is not high
    enough, callers must reject/re-capture rather than invent timing.
    """
    timing_path = screencast_frames / "timing.json"
    if not trace.recording_started_at or not recording.is_file() or not timing_path.is_file():
        return trace, {"status": "unavailable", "reason": "missing_capture_timing_evidence"}
    try:
        timing = json.loads(timing_path.read_text(encoding="utf-8"))
        frames = [item for item in timing.get("frames", []) if item.get("received_at")]
        receipts = [
            (int(item["index"]), datetime.fromisoformat(str(item["received_at"])).astimezone(UTC))
            for item in frames
        ]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return trace, {"status": "unavailable", "reason": "invalid_capture_timing_evidence"}
    if len(receipts) < 2:
        return trace, {"status": "unavailable", "reason": "insufficient_capture_timing_evidence"}

    native = _video_hashes(recording)
    if not native:
        return trace, {"status": "unavailable", "reason": "native_recording_has_no_decodable_frames"}
    first_receipt, last_receipt = receipts[0][1], receipts[-1][1]
    wall_span = max(0.001, (last_receipt - first_receipt).total_seconds())
    source_duration = native[-1][0]
    witnesses: list[tuple[str, str, datetime]] = []
    for event in trace.events:
        if not event.success:
            continue
        witnesses.append((event.id, "action_at", event.action_at or event.occurred_at))
        witnesses.append((event.id, "occurred_at", event.occurred_at))

    by_event: dict[str, dict[str, float]] = {}
    findings: list[dict[str, object]] = []
    previous = 0.0
    confidence_values: list[float] = []
    for event_id, field, observed_at in witnesses:
        nearest_index = min(receipts, key=lambda item: abs((item[1] - observed_at).total_seconds()))[0]
        witness = screencast_frames / f"frame-{nearest_index:07d}.jpg"
        witness_hash = _image_hash(witness)
        if witness_hash is None:
            continue
        expected = max(0.0, min(source_duration, source_duration * (observed_at - first_receipt).total_seconds() / wall_span))
        # Compare scores in the same normalised unit. The image hash contains
        # one bit per sampled pixel (576 bits), not 64 bits. Treating it as a
        # 64-bit hash made visually equivalent Browserbase/CDP frames look
        # unrelated and caused valid native captures to be rejected.
        #
        # The modest temporal term resolves repeated static UI states without
        # allowing a visually identical end frame to steal an earlier event.
        # It is grounded in the recorder's own timestamps, not an invented
        # evenly-spaced scene clock.
        choices = [
            (
                distance / _HASH_BITS
                + min(1.0, abs(second - expected) / 20.0) * 0.35,
                second,
                distance,
            )
            for second, native_hash in native
            if second + 0.25 >= previous
            for distance in [_hamming(witness_hash, native_hash)]
        ]
        if not choices:
            continue
        _, source_second, distance = min(choices)
        previous = max(previous, source_second)
        confidence = max(0.0, 1.0 - distance / _HASH_BITS)
        confidence_values.append(confidence)
        by_event.setdefault(event_id, {})[field] = source_second
        findings.append({
            "event_id": event_id, "field": field, "witness_frame": nearest_index,
            "source_second": round(source_second, 3), "hash_distance": distance,
            "confidence": round(confidence, 3),
        })
    complete = len(by_event) == len([event for event in trace.events if event.success]) and all(
        {"action_at", "occurred_at"}.issubset(item) for item in by_event.values()
    )
    average = sum(confidence_values) / len(confidence_values) if confidence_values else 0.0
    low_confidence = sum(value < 0.55 for value in confidence_values)
    # A good average can hide a completely unrelated final chapter. Require
    # almost every action/reveal witness to match, because each one controls a
    # caption, cursor, and scene transition in the delivered journey.
    if not complete or average < 0.75 or low_confidence > max(1, len(confidence_values) // 10):
        return trace, {
            "status": "rejected", "reason": "source_timeline_alignment_low_confidence",
            "average_confidence": round(average, 3), "low_confidence_count": low_confidence,
            "findings": findings,
        }
    events = []
    for event in trace.events:
        mapped = by_event.get(event.id)
        if not mapped:
            events.append(event)
            continue
        action, reveal = mapped["action_at"], max(mapped["action_at"], mapped["occurred_at"])
        events.append(event.model_copy(update={
            "action_at": trace.recording_started_at + timedelta(seconds=action),
            "occurred_at": trace.recording_started_at + timedelta(seconds=reveal),
        }))
    return trace.model_copy(update={"events": events}), {
        "status": "aligned", "average_confidence": round(average, 3),
        "sample_rate": _FPS, "findings": findings,
    }


def _video_hashes(video: Path) -> list[tuple[float, int]]:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video), "-vf",
         f"fps={_FPS},scale={_WIDTH}:{_HEIGHT},format=gray", "-f", "rawvideo", "-"],
        capture_output=True, check=False,
    )
    payload = result.stdout
    return [
        (index / _FPS, _hash(payload[index * _FRAME_BYTES:(index + 1) * _FRAME_BYTES]))
        for index in range(len(payload) // _FRAME_BYTES)
    ]


def _image_hash(path: Path) -> int | None:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-frames:v", "1", "-vf",
         f"scale={_WIDTH}:{_HEIGHT},format=gray", "-f", "rawvideo", "-"],
        capture_output=True, check=False,
    )
    return _hash(result.stdout) if len(result.stdout) == _FRAME_BYTES else None


def _hash(values: bytes) -> int:
    average = sum(values) / len(values)
    return sum((value >= average) << index for index, value in enumerate(values))


def _hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()
