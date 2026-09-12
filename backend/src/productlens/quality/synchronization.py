"""Trace-to-presentation synchronization checks without a paid model."""

from __future__ import annotations

from collections.abc import Iterable

from productlens.contracts.models import DemoTrace


def _ordered_subset(items: list[str], expected: list[str]) -> bool:
    cursor = 0
    for item in items:
        try:
            cursor = expected.index(item, cursor) + 1
        except ValueError:
            return False
    return bool(items)


def secure_transition_intervals(presentation_props: dict) -> list[tuple[float, float]]:
    """Extract renderer-declared credential-card intervals in seconds.

    This is intentionally derived from render props, rather than from a page
    name or operation type, so a QA retry reaches the same decision as the
    original generation path.
    """
    frame_rate = float(presentation_props.get("frameRate", 30))
    if frame_rate <= 0:
        return []
    return [
        (float(item["start"]) / frame_rate, float(item["end"]) / frame_rate)
        for item in presentation_props.get("redactions", [])
        if item.get("mode") == "secure-full-frame"
        and isinstance(item.get("start"), (int, float))
        and isinstance(item.get("end"), (int, float))
    ]


def inspect_synchronization(
    trace: DemoTrace,
    script: list[dict],
    captions: list[dict],
    *,
    narration_requested: bool,
    narration_created: bool,
    explained_intervals: Iterable[tuple[float, float]] = (),
) -> dict:
    """Check synchronization while allowing explicit secure transitions.

    A credential-redaction card is deliberately readable visual communication,
    not unspoken product footage.  It may bridge a caption-only gap, but only
    when the renderer supplied a bounded interval for that card.  Ordinary
    loading, empty footage, and arbitrary presentation gaps remain delivery
    failures.
    """
    failures: list[str] = []
    warnings: list[str] = []
    event_ids = [event.id for event in trace.events if event.success]
    script_ids = [str(line.get("event_id", "")) for line in script]
    caption_ids = [str(line.get("scene_id", "")) for line in captions]
    if not _ordered_subset(script_ids, event_ids):
        failures.append("SCRIPT_TRACE_MISMATCH")
    if caption_ids != script_ids:
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
    # Caption-only delivery needs reader-sized dwell, not merely non-overlap.
    # The floor is deliberately conservative: a 20-word sentence needs about
    # six seconds at normal silent-reading pace, while a short line still
    # receives enough time to register before the next visual transition.
    for caption in captions:
        words = len(str(caption.get("text", "")).split())
        required = max(2.4, words / 3.2 + 0.25)
        if float(caption.get("end", 0)) - float(caption.get("start", 0)) + 0.02 < required:
            failures.append("CAPTION_READING_DWELL_TOO_SHORT")
            break
    if not narration_created:
        protected = sorted(
            (max(0.0, float(start)), max(0.0, float(end)))
            for start, end in explained_intervals
            if float(end) > float(start)
        )

        def has_long_unexplained_gap(start: float, end: float) -> bool:
            cursor = start
            for protected_start, protected_end in protected:
                if protected_end <= cursor:
                    continue
                if protected_start >= end:
                    break
                if protected_start - cursor > 6.0:
                    return True
                cursor = max(cursor, protected_end)
                if cursor >= end:
                    return False
            return end - cursor > 6.0

        previous_end = 0.0
        for caption in captions:
            start = float(caption.get("start", 0))
            if has_long_unexplained_gap(previous_end, start):
                failures.append("CAPTION_SILENCE_GAP_TOO_LONG")
                break
            previous_end = float(caption.get("end", previous_end))
    return {
        "synchronization_score": 1.0 if not failures else 0.0,
        "audio_score": 1.0 if narration_created or not narration_requested else 0.0,
        "hard_failures": failures,
        "warnings": warnings,
    }
