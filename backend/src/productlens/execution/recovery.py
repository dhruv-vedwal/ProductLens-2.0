"""Targeted, non-mutating recovery for a dispatched terminal submit."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from productlens.contracts.models import DemoTrace, OperationKind, WorkflowState


class OutcomeRepairError(RuntimeError):
    pass


def repair_dispatched_terminal_submit(
    trace: DemoTrace,
    verification: dict[str, Any],
) -> DemoTrace:
    """Promote one dispatched terminal submit using fresh read-only evidence.

    This deliberately cannot repair an arbitrary failure: the trace must end
    with one unsuccessful, dispatched submit and the verifier must report a
    visible deterministic generated-value match.  The external action is not
    re-executed; the repair simply records independent proof that it completed.
    """
    if not verification.get("any_visible_match"):
        raise OutcomeRepairError("read-only verification did not find a visible generated value")
    if not trace.events:
        raise OutcomeRepairError("trace has no dispatched event to repair")
    event = trace.events[-1]
    if event.kind is not OperationKind.SUBMIT or event.success or event.action_at is None:
        raise OutcomeRepairError("only the final dispatched unsuccessful submit may be repaired")
    checked = [
        str(item.get("field"))
        for item in verification.get("checked_fields", [])
        if isinstance(item, dict) and item.get("visible_match")
    ]
    if not checked:
        raise OutcomeRepairError("verification has no matched field evidence")
    event.success = True
    event.after = {
        **event.after,
        "target_available": True,
        "verified_outcome": {
            "mode": "fresh_read_only_generated_value_match",
            "matched_fields": checked,
            "source_url": verification.get("source_url"),
        },
    }
    event.recovery.append({
        "strategy": "read_only_outcome_verification",
        "reason": "post-submit target changed after successful navigation",
    })
    trace.errors = [
        error for error in trace.errors
        if error.get("code") != "PRODUCTION_CAPTURE_INTERRUPTED"
    ]
    trace.outcome_verified = True
    trace.completed_at = datetime.now(UTC)
    trace.final_state = WorkflowState.COMPLETE
    return trace
