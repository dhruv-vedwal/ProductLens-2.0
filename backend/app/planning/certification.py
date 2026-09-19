"""Build an auditable production workflow certificate from rehearsal evidence."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.contracts.models import (
    ActionCapability,
    BehavioralProductModel,
    CertifiedWorkflowEdge,
    CertifiedWorkflowGraph,
    OperationKind,
    OutcomeSpec,
)
from app.planning.capabilities import compile_record_creation


def certify_rehearsed_capability(
    capability: ActionCapability,
    *,
    rehearsal_run_id: str,
    behavioral_model: BehavioralProductModel | None = None,
) -> CertifiedWorkflowGraph:
    if not capability.verified or capability.outcome_target is None:
        raise ValueError("capability must have an independently verified rehearsal outcome")
    operations = compile_record_creation(capability)
    fingerprint = (
        behavioral_model.product_fingerprint
        if behavioral_model is not None
        else hashlib.sha256(capability.source_url.encode("utf-8")).hexdigest()
    )
    start_state = (
        next(
            (
                state.id
                for state in (behavioral_model.states if behavioral_model else [])
                if state.url == capability.source_url
            ),
            "rehearsal:start",
        )
    )
    outcomes: list[OutcomeSpec] = []
    edges: list[CertifiedWorkflowEdge] = []
    prior_state = start_state
    for index, operation in enumerate(operations):
        outcome_id = f"outcome:{operation.id}"
        next_state = (
            "rehearsal:terminal"
            if index == len(operations) - 1
            else f"rehearsal:state:{index + 1}"
        )
        condition = operation.postconditions[0] if operation.postconditions else None
        outcomes.append(
            OutcomeSpec(
                id=outcome_id,
                intent=operation.intent,
                success_predicate=(
                    condition.kind
                    if condition and condition.kind in {"visible", "text", "value", "url"}
                    else "state"
                ),
                target=condition.target if condition and condition.target else operation.target,
                expected=condition.expected if condition else operation.value,
                start_state_id=prior_state,
                terminal_state_id=next_state,
                required_control_ids=[],
                identity_fields=[
                    field.name
                    for field in (capability.form_schema.fields if capability.form_schema else [])
                    if field.required or "rehearsal:include" in field.validation_messages
                ],
                verification_witnesses=list(operation.postconditions),
                evidence_refs=list(
                    dict.fromkeys(
                        [
                            *operation.evidence_refs,
                            *capability.outcome_evidence,
                            f"rehearsal:{rehearsal_run_id}",
                        ]
                    )
                ),
                mutation_class=(
                    "authorized_mutation"
                    if operation.kind in {OperationKind.SUBMIT, OperationKind.CREATE_RECORD}
                    else "read_only"
                ),
            )
        )
        edges.append(
            CertifiedWorkflowEdge(
                id=f"edge:{index + 1}",
                from_state_id=prior_state,
                to_state_id=next_state,
                outcome_id=outcome_id,
                operation=operation,
                verified=True,
                reversible=operation.kind
                not in {OperationKind.SUBMIT, OperationKind.CREATE_RECORD},
                mutation_committed=False,
                evidence_refs=list(
                    dict.fromkeys(
                        [
                            *operation.evidence_refs,
                            *capability.outcome_evidence,
                            f"rehearsal:{rehearsal_run_id}",
                        ]
                    )
                ),
            )
        )
        prior_state = next_state
    entity_fields = list(
        dict.fromkeys(field for outcome in outcomes for field in outcome.identity_fields)
    )
    if len(entity_fields) < 2:
        raise ValueError("certified creation workflow lacks two entity-specific witness fields")
    return CertifiedWorkflowGraph(
        product_fingerprint=fingerprint,
        objective=capability.purpose,
        start_state_id=start_state,
        terminal_state_ids=[prior_state],
        outcomes=outcomes,
        edges=edges,
        entity_witness_fields=entity_fields,
        rehearsal_run_id=rehearsal_run_id,
        certified_at=datetime.now(UTC).isoformat(),
    )


__all__ = ["certify_rehearsed_capability"]
