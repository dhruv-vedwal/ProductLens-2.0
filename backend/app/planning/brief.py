"""Evidence-only DemoBrief construction.

This module is intentionally provider-free: discovery has already collected
the evidence, and a brief must remain inspectable even when an editorial model
is unavailable.  It is a planning boundary, not another narration generator.
"""

from __future__ import annotations

from app.contracts.models import DemoBrief, ObjectiveSpec, ProductContext
from app.planning.candidates import select_candidate_flow


def build_demo_brief(
    context: ProductContext,
    *,
    objective: str,
    audience: str,
    duration_seconds: int,
) -> DemoBrief:
    """Describe the selected story using only fresh page knowledge."""
    specification = context.objective or ObjectiveSpec(raw=objective)
    candidate = select_candidate_flow(context, objective)
    if candidate is None:
        return DemoBrief(
            objective=specification,
            audience=audience,
            duration_seconds=duration_seconds,
            selected_flow="No evidence-backed candidate flow",
            exclusions=[*specification.exclusions, "production recording"],
            risks=["no_evidence_backed_candidate_flow"],
        )

    by_url = {page.url: page for page in context.page_knowledge}
    roles: dict[str, str] = {}
    evidence: list[str] = []
    for page_url in candidate.page_urls:
        page = by_url.get(page_url)
        roles[page_url] = "operational story page"
        if page:
            evidence.extend([f"page:{page.url}", *page.evidence_refs[:8]])
    for page_url in candidate.supporting_page_urls:
        page = by_url.get(page_url)
        roles[page_url] = "supporting context; include only when it improves comprehension"
        if page:
            evidence.extend([f"page:{page.url}", *page.evidence_refs[:8]])
    return DemoBrief(
        objective=specification,
        audience=audience,
        duration_seconds=duration_seconds,
        selected_flow=candidate.name,
        included_pages=candidate.page_urls,
        supporting_pages=candidate.supporting_page_urls,
        page_story_roles=roles,
        required_outcomes=candidate.expected_outcomes,
        exclusions=list(dict.fromkeys([*specification.exclusions, *context.rejected_routes])),
        evidence_refs=list(dict.fromkeys(evidence))[:100],
        risks=candidate.risks,
    )
