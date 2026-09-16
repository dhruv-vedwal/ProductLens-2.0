import pytest

from app.benchmark.runner import run
from app.contracts.models import WorkflowState


@pytest.mark.integration
@pytest.mark.parametrize("gate", [1, 2, 3, 4, 5, 6])
async def test_completed_gate_produces_verified_trace(gate: int):
    trace = await run(gate)
    assert trace.outcome_verified
    assert trace.final_state is WorkflowState.COMPLETE
    assert trace.events


@pytest.mark.integration
async def test_gate_three_trace_is_semantic_not_coordinate_only():
    trace = await run(3)
    assert {event.intent for event in trace.events} >= {
        "Enable notifications",
        "Increase quantity",
        "Advance item status",
        "Complete first task",
        "Complete upload",
    }
    assert all(event.before != event.after for event in trace.events)
