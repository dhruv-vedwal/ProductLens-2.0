from datetime import UTC, datetime

import pytest

from productlens.contracts.models import DemoTrace, InteractionEvent, OperationKind
from productlens.execution.recovery import OutcomeRepairError, repair_dispatched_terminal_submit


def _trace() -> DemoTrace:
    trace = DemoTrace(run_id="run", objective="Create a safe record", started_at=datetime.now(UTC))
    trace.events.append(
        InteractionEvent(
            operation_id="submit",
            kind=OperationKind.SUBMIT,
            intent="Create record",
            occurred_at=datetime.now(UTC),
            action_at=datetime.now(UTC),
            success=False,
            after={"target_available": False},
            duration_ms=100,
        )
    )
    trace.errors.append({"code": "PRODUCTION_CAPTURE_INTERRUPTED"})
    return trace


def test_read_only_evidence_repairs_only_the_dispatched_terminal_submit():
    repaired = repair_dispatched_terminal_submit(
        _trace(),
        {
            "source_url": "https://example.test/records",
            "any_visible_match": True,
            "checked_fields": [{"field": "Phone", "visible_match": True}],
        },
    )

    assert repaired.outcome_verified
    assert repaired.events[-1].success
    assert repaired.events[-1].after["verified_outcome"]["matched_fields"] == ["Phone"]
    assert repaired.errors == []


def test_read_only_repair_rejects_missing_visible_evidence():
    with pytest.raises(OutcomeRepairError, match="did not find"):
        repair_dispatched_terminal_submit(_trace(), {"any_visible_match": False})
