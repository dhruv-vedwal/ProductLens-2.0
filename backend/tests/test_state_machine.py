import pytest

from app.contracts.models import OperationKind, Postcondition, SemanticOperation, Target
from app.planning.state_machine import InvalidTransition, WorkflowStateMachine


def test_operation_graph_requires_verified_postconditions_before_advance():
    operation = SemanticOperation(
        kind=OperationKind.CLICK,
        intent="Open details",
        target=Target(name="Details", selector="button.details"),
        postconditions=[Postcondition(kind="visible", expected=True)],
    )
    machine = WorkflowStateMachine.from_operations([operation])
    with pytest.raises(InvalidTransition):
        machine.transition(operation.id)
    assert machine.transition(operation.id, verified=True) == "STEP_1"
    assert machine.history[-1]["verified"] is True


def test_unknown_event_cannot_skip_compiled_steps():
    machine = WorkflowStateMachine.from_operations([])
    with pytest.raises(InvalidTransition):
        machine.transition("invented-event", verified=True)


def test_state_graph_artifact_records_the_postcondition_gate():
    operation = SemanticOperation(
        kind=OperationKind.CLICK,
        intent="Open details",
        target=Target(name="Details", selector="button.details"),
        postconditions=[Postcondition(kind="visible", expected=True)],
    )

    artifact = WorkflowStateMachine.from_operations([operation]).artifact()

    assert artifact["initial_state"] == "NEW"
    assert artifact["transitions"][0]["event"] == operation.id
    assert artifact["transitions"][0]["required_postconditions"] == ["visible"]
