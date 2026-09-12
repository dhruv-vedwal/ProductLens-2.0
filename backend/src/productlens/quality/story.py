from __future__ import annotations

from typing import Any

from productlens.contracts.models import DemoTrace


def _ordered_subset(items: list[str], expected: list[str]) -> bool:
    """Accept intentional editorial compression, never reordering or invention."""
    cursor = 0
    for item in items:
        try:
            cursor = expected.index(item, cursor) + 1
        except ValueError:
            return False
    return bool(items)


def inspect_story(
    trace: DemoTrace, *, objective: str, script: list[dict[str, Any]] | None = None
) -> dict:
    failures: list[str] = []
    if not trace.outcome_verified:
        failures.append("OBJECTIVE_OUTCOME_UNVERIFIED")
    if not trace.events:
        failures.append("EMPTY_DEMOTRACE")
    if any(not event.success for event in trace.events):
        failures.append("FAILED_TRACE_EVENT")
    if not objective.strip():
        failures.append("MISSING_OBJECTIVE")
    if script is not None:
        expected = [event.id for event in trace.events if event.success]
        actual = [str(line.get("event_id", "")) for line in script]
        if not _ordered_subset(actual, expected):
            failures.append("NARRATION_TRACE_MISMATCH")
        for line in script:
            text = str(line.get("text", "")).strip()
            if not text:
                failures.append("EMPTY_NARRATION_LINE")
                break
        raw_intents = {event.intent.strip().rstrip(".") for event in trace.events if event.success}
        if any(str(line.get("text", "")).strip().rstrip(".") in raw_intents for line in script):
            failures.append("RAW_ACTION_LABEL_CAPTION")
    return {
        "execution_score": 1.0 if trace.outcome_verified else 0.0,
        "story_score": 1.0 if not failures else 0.0,
        "hard_failures": failures,
        "event_count": len(trace.events),
        "narration_line_count": len(script) if script is not None else None,
    }
