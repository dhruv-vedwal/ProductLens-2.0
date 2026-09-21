"""Quality gate for the planning certificate and verified execution trace."""

from __future__ import annotations

import re

from app.contracts.models import (
    DemoPlan,
    DemoTrace,
    DiagramConnector,
    DiagramNode,
    DiagramState,
    OperationKind,
)


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


def infer_diagram_state_from_trace(trace: DemoTrace, plan: DemoPlan) -> DiagramState | None:
    """Recover a semantic diagram witness from verified visual editor events.

    The executor records a committed ``surface_changed`` gesture for every
    drawing, label, and connector operation. Older plans may not have carried
    optional ``diagram_*`` payloads, so QA can still build a grounded witness
    from the operation's observed intent and the committed event geometry.
    This is deliberately vocabulary-neutral and never names a product, route,
    or fixed canvas control.
    """
    operations = {step.operation.id: step.operation for step in plan.workflow_steps}
    nodes: list[DiagramNode] = []
    connectors: list[DiagramConnector] = []
    latest: DiagramState | None = None
    for event in trace.events:
        if not event.success or not isinstance(event.after, dict):
            continue
        gesture = event.after.get("gesture")
        if not isinstance(gesture, dict) or gesture.get("surface_changed") is not True:
            continue
        operation = operations.get(event.operation_id)
        if operation is None or (
            not isinstance(operation.value, dict) and operation.kind is not OperationKind.KEY_PRESS
        ):
            continue
        evidence = event.screenshot_path or f"trace:event:{event.id}"
        payload = (
            operation.value.get("diagram_node")
            if isinstance(operation.value, dict)
            else None
        )
        if not isinstance(payload, dict) and operation.kind is OperationKind.POINTER_SEQUENCE:
            match = re.search(
                r"(?:draw|place)\s+(?:the\s+)?['\"]?(.+?)['\"]?\s+(?:rectangle|component|shape)\b",
                operation.intent,
                flags=re.IGNORECASE,
            )
            if match:
                label = " ".join(match.group(1).split()).strip(" '\"")
                points = operation.value.get("relative_points")
                first = points[0] if isinstance(points, list) and points else {}
                last = points[-1] if isinstance(points, list) and points else first
                x = (float(first.get("x", 0.5)) + float(last.get("x", 0.5))) / 2
                y = (float(first.get("y", 0.5)) + float(last.get("y", 0.5))) / 2
                payload = {
                    "id": f"diagram-node:{re.sub(r'[^a-z0-9]+', '-', label.casefold()).strip('-')}",
                    "label": label,
                    "kind": "component",
                    "x": max(0.0, min(1.0, x)),
                    "y": max(0.0, min(1.0, y)),
                }
        if isinstance(payload, dict):
            node = DiagramNode(**payload, visual_evidence_ref=evidence)
            nodes = [item for item in nodes if item.id != node.id]
            nodes.append(node)
        typed = (
            str(operation.value.get("value") or "").strip()
            if isinstance(operation.value, dict)
            else str(operation.value or "").strip()
        )
        if operation.kind is OperationKind.KEY_PRESS and typed:
            nodes = [
                item.model_copy(update={"label_evidence_ref": evidence})
                if item.label.casefold() == typed.casefold()
                else item
                for item in nodes
            ]
        connector = (
            operation.value.get("diagram_connector")
            if isinstance(operation.value, dict)
            else None
        )
        if not isinstance(connector, dict) and operation.kind is OperationKind.POINTER_SEQUENCE:
            match = re.search(
                r"(?:arrow|connector)\s+from\s+['\"]?(.+?)['\"]?\s+to\s+['\"]?(.+?)['\"]?\.?$",
                operation.intent,
                flags=re.IGNORECASE,
            )
            if match:
                source = next(
                    (item for item in nodes if item.label.casefold() == match.group(1).strip(" '\"").casefold()),
                    None,
                )
                target = next(
                    (item for item in nodes if item.label.casefold() == match.group(2).strip(" '\"").casefold()),
                    None,
                )
                if source is not None and target is not None:
                    connector = {
                        "id": f"diagram-connector:{source.id}->{target.id}",
                        "source_node_id": source.id,
                        "target_node_id": target.id,
                        "kind": "directed",
                    }
        if isinstance(connector, dict):
            item = DiagramConnector(
                **connector,
                visual_evidence_ref=evidence,
            )
            connectors = [value for value in connectors if value.id != item.id]
            connectors.append(item)
        labels_verified = bool(nodes) and all(item.label_evidence_ref for item in nodes)
        node_ids = {item.id for item in nodes}
        topology_verified = bool(connectors) and all(
            item.source_node_id in node_ids and item.target_node_id in node_ids
            for item in connectors
        )
        latest = DiagramState(
            surface_control_id=operation.target.selector if operation.target else None,
            nodes=list(nodes),
            connectors=list(connectors),
            labels_verified=labels_verified,
            topology_verified=topology_verified,
            screenshot_ref=event.screenshot_path,
            evidence_refs=list(dict.fromkeys([*getattr(latest, "evidence_refs", []), evidence])),
        )
    return latest


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
