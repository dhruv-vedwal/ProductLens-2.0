"""Quality gate for the planning certificate and verified execution trace."""

from __future__ import annotations

from app.contracts.models import DemoPlan, DemoTrace, DiagramState


def inspect_diagram_semantics(
    diagram: DiagramState | None,
    *,
    requested_labels: list[str] | None = None,
) -> list[str]:
    """Reject pixel-only canvas success without readable labeled topology."""

    if (
        diagram is None
        or len(diagram.nodes) < 2
        or not diagram.labels_verified
        or not diagram.topology_verified
        or not diagram.connectors
    ):
        return ["DIAGRAM_SEMANTIC_VERIFICATION_FAILED"]
    failures: list[str] = []
    present = {node.label.casefold() for node in diagram.nodes if node.label}
    for label in requested_labels or []:
        if label.casefold() not in present:
            failures.append("DIAGRAM_REQUESTED_LABELS_MISSING")
            break
    if any(not node.label_evidence_ref for node in diagram.nodes):
        failures.append("DIAGRAM_LABELS_UNCOMMITTED")
    return failures


def inspect_certified_outcomes(plan: DemoPlan, trace: DemoTrace) -> dict[str, object]:
    """Ensure every required planned outcome has a verified event witness."""
    certificate = plan.certified_script
    if certificate is None:
        return {
            "status": "not_applicable",
            "hard_failures": [],
            "required_outcomes": 0,
            "verified_outcomes": 0,
            "missing_outcomes": [],
        }
    successful = {event.operation_id: event for event in trace.events if event.success}
    missing: list[str] = []
    verified = 0
    for outcome in certificate.outcomes:
        operation_id = outcome.id.removeprefix("outcome-")
        witness = successful.get(operation_id)
        # The production executor intentionally removes an equivalent opening
        # Navigate when the browser is already on that canonical URL. The
        # stable page evidence still proves the opening outcome without a
        # duplicate refresh.
        if witness is None and outcome.success_predicate == "url":
            page_refs = [ref.removeprefix("page:") for ref in outcome.evidence_refs if ref.startswith("page:")]
            witness = next(
                (
                    event
                    for event in successful.values()
                    if event.page_url and event.page_url in page_refs
                ),
                None,
            )
        if witness is None:
            if outcome.required:
                missing.append(outcome.id)
            continue
        verified += 1
    return {
        "status": "pass" if not missing else "failed",
        "hard_failures": ["CERTIFIED_OUTCOME_MISSING"] if missing else [],
        "required_outcomes": sum(item.required for item in certificate.outcomes),
        "verified_outcomes": verified,
        "missing_outcomes": missing,
        "certificate_schema_version": certificate.schema_version,
    }
