import pytest

from app.contracts.models import OperationKind, Postcondition, SemanticOperation, Target
from app.planning.side_effects import (
    SideEffectPolicyError,
    authorize_operation,
    side_effect_decision,
)


def _submit(intent: str) -> SemanticOperation:
    return SemanticOperation(kind=OperationKind.SUBMIT, intent=intent, target=Target(name=intent))


def test_external_side_effects_are_blocked_even_when_mutations_are_allowed():
    operation = _submit("Send invitation email")
    assert side_effect_decision(operation) == "blocked_external"
    with pytest.raises(SideEffectPolicyError):
        authorize_operation(operation, True)


def test_testable_creation_requires_explicit_demo_authorization():
    operation = _submit("Create test lead")
    with pytest.raises(SideEffectPolicyError):
        authorize_operation(operation, False)
    assert authorize_operation(operation, True) == "allowed_testable"


def test_test_state_postcondition_marks_an_invitation_as_deterministic_fixture_work():
    operation = _submit("Send teammate invitation")
    operation.postconditions = [
        Postcondition(kind="test_state", expected="window.__testState.invited")
    ]
    assert side_effect_decision(operation) == "allowed_testable"


def test_record_creation_is_mutating_even_when_named_generically():
    operation = SemanticOperation(
        kind=OperationKind.CREATE_RECORD,
        intent="Add item",
        target=Target(name="Add item"),
    )
    with pytest.raises(SideEffectPolicyError):
        authorize_operation(operation, False)
