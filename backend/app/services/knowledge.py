"""Canonical materialization of reusable product knowledge.

Discovery produces a rich `ProductContext`; workers and generation stages need
the same versioned `ProductKnowledge` representation. Keeping that conversion
in one module prevents the two pipelines from drifting apart.
"""

from __future__ import annotations

import hashlib
import json

from app.contracts.models import ActionCapability, ProductContext, ProductKnowledge


def product_knowledge_from_context(
    context: ProductContext,
    *,
    project_id: str | None = None,
) -> ProductKnowledge:
    identity = {
        "url": context.url,
        "title": getattr(context, "title", ""),
        "routes": sorted(getattr(context, "relevant_routes", [])),
        "pages": sorted(
            getattr(page, "url", "") for page in getattr(context, "page_knowledge", [])
        ),
        "sections": sorted(
            section
            for page in getattr(context, "page_knowledge", [])
            for section in getattr(page, "visible_sections", [])
        ),
        "relationships": sorted(
            (item.source, item.target, item.relation)
            for item in getattr(context, "relationships", [])
        ),
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    form_schemas = []
    capabilities = getattr(context, "capabilities", [])
    for raw_capability in capabilities:
        try:
            capability = ActionCapability.model_validate(raw_capability)
        except (TypeError, ValueError):
            continue
        if capability.form_schema is not None:
            form_schemas.append(capability.form_schema)
    return ProductKnowledge(
        project_id=project_id,
        product_fingerprint=fingerprint,
        version=fingerprint[:16],
        application_type=getattr(context, "application_type", "web_application"),
        navigation=getattr(context, "navigation", []),
        routes=getattr(context, "relevant_routes", []),
        feature_map=getattr(context, "feature_knowledge", []),
        relationships=getattr(context, "relationships", []),
        page_knowledge=getattr(context, "page_knowledge", []),
        workflow_knowledge=getattr(context, "candidate_demo_flows", []),
        form_schemas=form_schemas,
        capabilities=capabilities,
        capability_resolutions=getattr(context, "capability_resolutions", []),
        known_blockers=getattr(context, "blockers", []),
        successful_actions=getattr(context, "successful_action_hints", []),
    )


def product_knowledge_payload(
    context: ProductContext,
    *,
    project_id: str | None = None,
) -> dict[str, object]:
    """Serialize canonical knowledge while retaining legacy artifact aliases."""
    knowledge = product_knowledge_from_context(context, project_id=project_id)
    return {
        **knowledge.model_dump(mode="json"),
        "relevant_routes": getattr(context, "relevant_routes", []),
        "successful_actions": getattr(context, "successful_action_hints", []),
    }
