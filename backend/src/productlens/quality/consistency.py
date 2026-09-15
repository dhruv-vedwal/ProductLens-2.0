"""Cross-layer consistency checks for persisted production plans.

The selected candidate flow is discovery metadata, while ``workflow_steps``
are executable truth. A stale candidate must never silently survive into a
deliverable run. These checks are product-agnostic and JSON-compatible so
offline artifact tooling can use the same rule as completion QA.
"""

from __future__ import annotations

from typing import Any

from productlens.urls import canonical_product_url


def _canonical_url(value: str) -> str:
    return canonical_product_url(str(value or ""))


def _operation_page(operation: dict[str, Any]) -> str | None:
    if operation.get("page_url"):
        return str(operation["page_url"])
    target = operation.get("target")
    if isinstance(target, dict) and target.get("source_url"):
        return str(target["source_url"])
    return None


def validate_selected_candidate_consistency(plan: dict[str, Any]) -> list[str]:
    """Return hard failures when candidate metadata diverges from execution."""
    synthetic = plan.get("synthetic_data_plan")
    if not isinstance(synthetic, dict):
        return []
    candidate = synthetic.get("selected_candidate_flow")
    if candidate is None:
        return []
    if not isinstance(candidate, dict):
        return ["SELECTED_CANDIDATE_ARTIFACT_MISMATCH"]

    failures: list[str] = []
    steps = plan.get("workflow_steps")
    operations = [
        item.get("operation")
        for item in (steps if isinstance(steps, list) else [])
        if isinstance(item, dict) and isinstance(item.get("operation"), dict)
    ]
    actual_pages = {
        _canonical_url(page) for operation in operations if (page := _operation_page(operation))
    }
    candidate_pages = {_canonical_url(page) for page in candidate.get("page_urls", []) if page}
    if actual_pages and candidate_pages != actual_pages:
        failures.append("SELECTED_CANDIDATE_ARTIFACT_MISMATCH")

    expected = [str(value) for value in plan.get("expected_outcomes", []) if value]
    candidate_expected = [str(value) for value in candidate.get("expected_outcomes", []) if value]
    if expected != candidate_expected:
        failures.append("SELECTED_CANDIDATE_ARTIFACT_MISMATCH")

    selected_workflow = str(plan.get("selected_workflow") or "").strip()
    if selected_workflow and str(candidate.get("name") or "").strip() != selected_workflow:
        failures.append("SELECTED_CANDIDATE_ARTIFACT_MISMATCH")

    candidate_steps = candidate.get("semantic_steps")
    if not isinstance(candidate_steps, list) or len(candidate_steps) != len(operations):
        failures.append("SELECTED_CANDIDATE_ARTIFACT_MISMATCH")

    evidence = {
        str(reference)
        for operation in operations
        for reference in operation.get("evidence_refs", [])
        if reference
    }
    candidate_evidence = {
        str(reference) for reference in candidate.get("evidence_coverage", []) if reference
    }
    if evidence - candidate_evidence:
        failures.append("SELECTED_CANDIDATE_ARTIFACT_MISMATCH")
    return list(dict.fromkeys(failures))
