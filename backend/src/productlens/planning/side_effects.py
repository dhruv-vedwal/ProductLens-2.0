"""Conservative, explainable side-effect authorization for semantic operations."""

from __future__ import annotations

from productlens.contracts.models import OperationKind, SemanticOperation


class SideEffectPolicyError(ValueError):
    pass


MUTATING_KINDS = {OperationKind.SUBMIT, OperationKind.CHECK, OperationKind.UNCHECK}
_BLOCKED_TERMS = {
    "payment", "pay", "checkout", "invoice", "charge", "email", "sms", "whatsapp",
    "invite", "invitation", "webhook", "publish", "production", "integration", "connect app",
}
_TESTABLE_TERMS = {"test", "demo", "lead", "booking", "project", "sample", "sandbox"}


def side_effect_decision(operation: SemanticOperation) -> str:
    if operation.kind not in MUTATING_KINDS:
        return "read_only"
    evidence = " ".join(
        part for part in [operation.intent, operation.target.name if operation.target else "", str(operation.value or "")]
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
        raise SideEffectPolicyError("Side-effecting operation is not authorized: blocked external or production effect")
    if decision == "requires_explicit_authorization" and not allow_external_side_effects:
        raise SideEffectPolicyError("Side-effecting operation is not authorized: explicit authorization required")
    if decision == "allowed_testable" and not allow_external_side_effects:
        raise SideEffectPolicyError("Side-effecting operation is not authorized: isolated-demo authorization required")
    return decision
