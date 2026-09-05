"""Trace-to-presentation synchronization checks without a paid model."""

from __future__ import annotations

from productlens.contracts.models import DemoTrace


def inspect_synchronization(
    trace: DemoTrace, script: list[dict], captions: list[dict], *, narration_requested: bool, narration_created: bool
) -> dict:
    failures: list[str] = []
    warnings: list[str] = []
    event_ids = [event.id for event in trace.events if event.success]
    script_ids = [str(line.get("event_id", "")) for line in script]
    caption_ids = [str(line.get("scene_id", "")) for line in captions]
    if script_ids != event_ids:
        failures.append("SCRIPT_TRACE_MISMATCH")
    if caption_ids != event_ids:
        failures.append("CAPTION_TRACE_MISMATCH")
    previous_end = 0.0
    for caption in captions:
        start, end = float(caption.get("start", -1)), float(caption.get("end", -1))
        if start < previous_end or end <= start:
            failures.append("BROKEN_CAPTION_TIMING")
            break
        previous_end = end
    if narration_requested and not narration_created:
        failures.append("REQUESTED_NARRATION_MISSING")
    if not captions:
        failures.append("MISSING_CAPTIONS")
    if captions and previous_end < 8:
        warnings.append("SHORT_CAPTION_COVERAGE")
    return {
        "synchronization_score": 1.0 if not failures else 0.0,
        "audio_score": 1.0 if narration_created or not narration_requested else 0.0,
        "hard_failures": failures,
        "warnings": warnings,
    }
