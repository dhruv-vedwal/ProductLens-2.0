"""Conservative, explainable side-effect authorization for semantic operations."""

from __future__ import annotations

from app.contracts.models import OperationKind, SemanticOperation


class SideEffectPolicyError(ValueError):
    pass


# Semantic mutation categories are product-neutral.  A generic planner may
# call the action "add item" or "create record", but both still require the
# explicit side-effect policy.
MUTATING_KINDS = {
    OperationKind.SUBMIT,
    OperationKind.CHECK,
    OperationKind.UNCHECK,
    OperationKind.CREATE_RECORD,
}
# Local, reversible gestures (canvas strokes, drag/reposition, and similar
# UI edits) do not leave the product or notify third parties.  They are safe
# to demonstrate under a read-only objective, but must still carry an
# observable postcondition so a dispatched gesture cannot be mistaken for a
# successful interaction.
REVERSIBLE_GESTURE_KINDS = {OperationKind.POINTER_SEQUENCE, OperationKind.DRAG}
_BLOCKED_TERMS = {
    "payment",
    "pay",
    "checkout",
    "invoice",
    "charge",
    "send",
    "email",
    "sms",
    "whatsapp",
    "invite",
    "invitation",
    "webhook",
    "publish",
    "production",
    "integration",
    "connect app",
}
# Product nouns are not a safety policy. A CRM's Lead/Booking labels happened
# to be present in an early benchmark, but privileging them made production
# authorization application-specific. Only explicit isolated/demo language or
# fixture state can classify a mutation as testable.
_TESTABLE_TERMS = {"test", "demo", "sample", "sandbox", "isolated", "fixture"}


def side_effect_decision(operation: SemanticOperation) -> str:
    if operation.side_effect_policy == "blocked":
        raise SideEffectPolicyError("Operation explicitly blocks side effects")
    if operation.side_effect_policy == "read_only" and operation.kind in MUTATING_KINDS:
        return "blocked_external"
    if operation.kind in REVERSIBLE_GESTURE_KINDS:
        if not any(
            condition.kind in {"changed", "test_state", "text", "visible"}
            for condition in operation.postconditions
        ):
            raise SideEffectPolicyError("Reversible gesture requires an observable postcondition")
        return "allowed_reversible"
    if operation.kind not in MUTATING_KINDS:
        return "read_only"
    evidence = " ".join(
        part
        for part in [
            operation.intent,
            operation.target.name if operation.target else "",
            str(operation.value or ""),
        ]
    ).lower()
    # A ProductLens test-state postcondition is deterministic fixture evidence;
    # it is stronger than a natural-language label such as "invite" and avoids
    # treating a local benchmark button as a real email dispatch.
    if any(condition.kind == "test_state" for condition in operation.postconditions):
        return "allowed_testable"
    if any(term in evidence for term in _BLOCKED_TERMS):
        return "blocked_external"
    if any(term in evidence for term in _TESTABLE_TERMS):
        return "allowed_testable"
    return "requires_explicit_authorization"


def authorize_operation(operation: SemanticOperation, allow_external_side_effects: bool) -> str:
    decision = side_effect_decision(operation)
    if decision == "blocked_external":
        raise SideEffectPolicyError(
            "Side-effecting operation is not authorized: blocked external or production effect"
        )
    if decision == "requires_explicit_authorization" and not allow_external_side_effects:
        raise SideEffectPolicyError(
            "Side-effecting operation is not authorized: explicit authorization required"
        )
    if decision == "allowed_testable" and not allow_external_side_effects:
        raise SideEffectPolicyError(
            "Side-effecting operation is not authorized: isolated-demo authorization required"
        )
    if decision == "allowed_reversible":
        return decision
    return decision
