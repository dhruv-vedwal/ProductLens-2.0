"""Compile discovery evidence into the behavior-first product graph."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from app.contracts.models import (
    BehavioralProductModel,
    ControlDependency,
    PageState,
    ProductContext,
    RuntimeCapability,
    StateTransition,
    SurfaceState,
)
from app.interaction.state import (
    descriptor_from_observation,
    page_state_fingerprint,
    route_identity,
)


def build_behavioral_product_model(context: ProductContext) -> BehavioralProductModel:
    controls_by_url: dict[str, list[dict[str, object]]] = defaultdict(list)
    for element in context.elements:
        url = element.source_url or context.url
        controls_by_url[url].append(
            {
                "name": element.name,
                "role": element.role,
                "tag": element.tag,
                "type": element.element_type,
                "selector": element.selector,
                "options": element.options,
                "autocomplete": element.autocomplete,
                "draggable": element.draggable,
            }
        )
    for capability in context.capabilities:
        schema = capability.get("form_schema") if isinstance(capability, dict) else None
        if not isinstance(schema, dict):
            continue
        url = str(schema.get("source_url") or capability.get("source_url") or context.url)
        for field in schema.get("fields", []):
            if isinstance(field, dict):
                controls_by_url[url].append(
                    {
                        "name": field.get("name"),
                        "role": field.get("control_type"),
                        "tag": "select"
                        if field.get("control_type") == "select"
                        else "input",
                        "type": field.get("control_type"),
                        "selector": field.get("selector"),
                        "options": field.get("options") or [],
                        "disabled": not bool(field.get("enabled", True)),
                        "value": field.get("observed_value"),
                        "depends_on": field.get("depends_on") or [],
                    }
                )

    states: list[PageState] = []
    surfaces: list[SurfaceState] = []
    all_controls = []
    dependencies: list[ControlDependency] = []
    unresolved: list[str] = []
    evidence_refs: list[str] = []
    pages = context.page_knowledge or []
    if not pages:
        unresolved.append("no_page_knowledge")
    for index, page in enumerate(pages):
        page_evidence = list(dict.fromkeys(str(item) for item in page.evidence_refs))[:32]
        surface = SurfaceState(
            id=f"page:{route_identity(page.url)}",
            kind="page",
            label=page.title,
            evidence_refs=page_evidence,
        )
        descriptors = [
            descriptor_from_observation(
                item,
                surface_id=surface.id,
                evidence_refs=[*page_evidence, f"page-knowledge:{index}"][:32],
            )
            for item in controls_by_url.get(page.url, [])
            if str(item.get("name") or "").strip()
        ]
        deduped = {item.stable_id: item for item in descriptors}
        state = PageState(
            id=f"discovery-state:{index}",
            url=page.url,
            route_identity=route_identity(page.url),
            title=page.title,
            ready=True,
            visual_surface=(
                "canvas"
                if any(item.behavior_class == "canvas_surface" for item in deduped.values())
                else "dom"
            ),
            surfaces=[surface],
            controls=list(deduped.values()),
            fingerprint=page.fingerprint,
            screenshot_ref=page.screenshot_evidence,
            evidence_refs=page_evidence,
        )
        state.fingerprint = state.fingerprint or page_state_fingerprint(state)
        states.append(state)
        surfaces.append(surface)
        all_controls.extend(state.controls)
        evidence_refs.extend(page_evidence)

        by_name = {item.label.casefold(): item for item in state.controls if item.label}
        for raw in controls_by_url.get(page.url, []):
            child_name = str(raw.get("name") or "").casefold()
            child = by_name.get(child_name)
            if child is None:
                continue
            for parent_name in raw.get("depends_on", []) or []:
                parent = by_name.get(str(parent_name).casefold())
                if parent is None:
                    unresolved.append(f"dependency_parent_unresolved:{parent_name}:{child.label}")
                    continue
                dependencies.append(
                    ControlDependency(
                        parent_control_id=parent.stable_id,
                        child_control_id=child.stable_id,
                        condition="discovery form evidence declares dependency",
                        effect="enabled",
                        confidence=0.8,
                        evidence_refs=page_evidence,
                    )
                )

    safe_transitions = [
        StateTransition(
            id=f"discovery-transition:{index}",
            before_state_id=states[index - 1].id,
            after_state_id=states[index].id,
            url_changed=states[index - 1].url != states[index].url,
            confidence=0.8,
            evidence_refs=["discovery:safe-navigation"],
        )
        for index in range(1, len(states))
    ]
    runtime_capabilities = []
    for raw in context.capabilities:
        try:
            runtime_capabilities.append(RuntimeCapability.model_validate(raw))
        except (TypeError, ValueError):
            # Legacy ActionCapability evidence remains available in the
            # source context but is not silently promoted to a verified
            # runtime capability.
            continue
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "pages": [state.fingerprint for state in states],
                "controls": [item.stable_id for item in all_controls],
                "dependencies": [
                    (item.parent_control_id, item.child_control_id, item.effect)
                    for item in dependencies
                ],
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return BehavioralProductModel(
        product_fingerprint=fingerprint,
        objective=context.objective,
        states=states,
        surfaces=surfaces,
        controls=all_controls,
        dependencies=dependencies,
        safe_transitions=safe_transitions,
        capabilities=runtime_capabilities,
        unresolved_uncertainties=list(dict.fromkeys(unresolved)),
        evidence_refs=list(dict.fromkeys(evidence_refs)),
    )


__all__ = ["build_behavioral_product_model"]
