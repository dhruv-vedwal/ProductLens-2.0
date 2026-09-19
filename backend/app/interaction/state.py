"""Normalize and reconcile behavior-aware browser state."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Literal
from urllib.parse import urlsplit

from app.contracts.models import (
    ControlDependency,
    ControlDescriptor,
    PageState,
    Rect,
    StateTransition,
)


def _clean(value: object, *, max_length: int | None = None) -> str:
    cleaned = " ".join(str(value or "").split()).strip()
    if max_length is not None and len(cleaned) > max_length:
        return cleaned[: max_length - 1].rstrip() + "…"
    return cleaned


def _route_identity(url: str) -> str:
    parsed = urlsplit(url)
    segments = [
        "{id}" if re.fullmatch(r"[0-9a-f-]{12,}|\d{5,}", segment, flags=re.IGNORECASE) else segment
        for segment in parsed.path.rstrip("/").split("/")
    ]
    return f"{parsed.scheme}://{parsed.netloc}{'/'.join(segments)}"


def classify_control(raw: Mapping[str, object]) -> tuple[str, float]:
    """Classify from observable semantics; names are not behavior authority."""

    role = _clean(raw.get("role")).casefold()
    tag = _clean(raw.get("tag")).casefold()
    input_type = _clean(raw.get("type") or raw.get("input_type")).casefold()
    haspopup = _clean(raw.get("haspopup") or raw.get("ariaHaspopup")).casefold()
    autocomplete = _clean(raw.get("autocomplete") or raw.get("ariaAutocomplete")).casefold()
    options = raw.get("options")
    option_count = len(options) if isinstance(options, list) else 0
    contenteditable = bool(raw.get("contenteditable"))

    if tag == "canvas" or role in {"application", "graphics-document"}:
        return "canvas_surface", 0.98
    if input_type == "file":
        return "file_picker", 0.99
    if contenteditable:
        return "rich_text", 0.9
    if input_type == "date":
        return "native_date", 0.99
    if input_type == "email":
        return "email_input", 0.99
    if input_type in {"tel", "phone"}:
        return "phone_input", 0.99
    if input_type == "checkbox" or role == "checkbox":
        return "checkbox", 0.99
    if input_type == "radio" or role == "radio":
        return "radio", 0.99
    if tag == "select":
        return "native_select", 0.99
    if role == "combobox" or haspopup == "listbox":
        if raw.get("depends_on") and bool(raw.get("disabled") or raw.get("loading")):
            return "dependent_async", 0.9
        return ("autocomplete" if autocomplete in {"list", "both"} else "combobox", 0.96)
    if role == "option" and raw.get("time_value"):
        return "time_slot", 0.9
    if role in {"gridcell", "option"} and raw.get("date_value"):
        return "date_picker", 0.9
    if tag == "textarea":
        return "multiline_input", 0.99
    if tag == "input" or role in {"textbox", "searchbox", "spinbutton"}:
        return "text_input", 0.88
    if role == "tab":
        return "tab", 0.99
    if role in {"button", "link"} or tag in {"button", "a"}:
        if input_type == "submit" or raw.get("submits_form"):
            return "submit", 0.95
        if raw.get("canvas_tool"):
            return "canvas_tool", 0.9
        return "button", 0.9
    if raw.get("draggable"):
        return "drag_target", 0.9
    return ("unknown", 0.25 if option_count == 0 else 0.45)


def stable_control_id(raw: Mapping[str, object], *, surface_id: str) -> str:
    """Build an identity from semantic and structural evidence, never ordinal alone."""

    geometry = raw.get("geometry") if isinstance(raw.get("geometry"), Mapping) else {}
    identity = {
        "surface": surface_id,
        "role": _clean(raw.get("role")).casefold(),
        "label": _clean(raw.get("label") or raw.get("name")).casefold(),
        "tag": _clean(raw.get("tag")).casefold(),
        "type": _clean(raw.get("type") or raw.get("input_type")).casefold(),
        "owner": _clean(raw.get("form") or raw.get("section") or raw.get("owner")).casefold(),
        "ancestry": _clean(raw.get("ancestry")).casefold(),
        "stable_attributes": raw.get("stable_attributes") or {},
        "geometry_bucket": {
            "x": round(float(geometry.get("x", 0)) / 24),
            "y": round(float(geometry.get("y", 0)) / 24),
        },
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]
    return f"control:{digest}"


def descriptor_from_observation(
    raw: Mapping[str, object],
    *,
    surface_id: str,
    evidence_refs: Iterable[str] = (),
) -> ControlDescriptor:
    behavior, confidence = classify_control(raw)
    geometry_raw = raw.get("geometry")
    geometry = (
        Rect(
            x=float(geometry_raw.get("x", 0)),
            y=float(geometry_raw.get("y", 0)),
            width=max(0.0, float(geometry_raw.get("width", 0))),
            height=max(0.0, float(geometry_raw.get("height", 0))),
        )
        if isinstance(geometry_raw, Mapping)
        else None
    )
    options = raw.get("options")
    return ControlDescriptor(
        stable_id=stable_control_id(raw, surface_id=surface_id),
        surface_id=surface_id,
        role=_clean(raw.get("role"), max_length=80),
        label=_clean(raw.get("label") or raw.get("name"), max_length=240),
        tag=_clean(raw.get("tag"), max_length=40),
        input_type=_clean(raw.get("type") or raw.get("input_type"), max_length=80),
        behavior_class=behavior,
        value=(_clean(raw.get("value"), max_length=500) or None),
        options=[_clean(item, max_length=240) for item in options if _clean(item)]
        if isinstance(options, list)
        else [],
        geometry=geometry,
        visible=bool(raw.get("visible", True)),
        enabled=not bool(raw.get("disabled", False)),
        read_only=bool(raw.get("read_only") or raw.get("readonly")),
        expanded=bool(raw.get("expanded")) if raw.get("expanded") is not None else None,
        classification_confidence=confidence,
        selector_hints=[
            _clean(raw.get(key), max_length=320)
            for key in ("selector", "testid")
            if _clean(raw.get(key))
        ][:8],
        evidence_refs=list(dict.fromkeys(str(item) for item in evidence_refs))[:32],
    )


def page_state_fingerprint(state: PageState) -> str:
    payload = {
        "route": state.route_identity,
        "surfaces": [(item.id, item.kind, item.visible) for item in state.surfaces],
        "controls": [
            (
                item.stable_id,
                item.behavior_class,
                item.value,
                item.options,
                item.visible,
                item.enabled,
                item.expanded,
            )
            for item in state.controls
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def reconcile_page_states(
    before: PageState,
    after: PageState,
    *,
    action_intent_id: str | None = None,
) -> StateTransition:
    prior = {item.stable_id: item for item in before.controls}
    current = {item.stable_id: item for item in after.controls}
    changed = [
        control_id
        for control_id in prior.keys() & current.keys()
        if prior[control_id].model_dump(
            exclude={"evidence_refs", "selector_hints", "geometry"}
        )
        != current[control_id].model_dump(
            exclude={"evidence_refs", "selector_hints", "geometry"}
        )
    ]
    prior_surfaces = {item.id for item in before.surfaces if item.visible}
    current_surfaces = {item.id for item in after.surfaces if item.visible}
    return StateTransition(
        before_state_id=before.id,
        after_state_id=after.id,
        action_intent_id=action_intent_id,
        changed_control_ids=sorted(changed),
        added_control_ids=sorted(current.keys() - prior.keys()),
        removed_control_ids=sorted(prior.keys() - current.keys()),
        opened_surface_ids=sorted(current_surfaces - prior_surfaces),
        closed_surface_ids=sorted(prior_surfaces - current_surfaces),
        url_changed=before.url != after.url,
        confidence=1.0,
        evidence_refs=list(dict.fromkeys([*before.evidence_refs, *after.evidence_refs])),
    )


def infer_control_dependencies(
    before: PageState,
    after: PageState,
    *,
    parent_control_id: str,
    evidence_refs: Iterable[str] = (),
) -> list[ControlDependency]:
    """Infer only dependencies demonstrated by an observed parent interaction."""

    prior = {item.stable_id: item for item in before.controls}
    current = {item.stable_id: item for item in after.controls}
    dependencies: list[ControlDependency] = []
    for control_id in prior.keys() & current.keys():
        if control_id == parent_control_id:
            continue
        old, new = prior[control_id], current[control_id]
        effect: Literal["enabled", "visible", "populated", "filtered"] | None = None
        condition = ""
        if not old.enabled and new.enabled:
            effect = "enabled"
            condition = "parent selection enabled child"
        elif not old.visible and new.visible:
            effect = "visible"
            condition = "parent selection revealed child"
        elif not old.options and new.options:
            effect = "populated"
            condition = "parent selection populated child options"
        elif old.options != new.options:
            effect = "filtered"
            condition = "parent selection changed child options"
        if effect is not None:
            dependencies.append(
                ControlDependency(
                    parent_control_id=parent_control_id,
                    child_control_id=control_id,
                    condition=condition,
                    effect=effect,
                    confidence=0.95,
                    evidence_refs=list(dict.fromkeys(str(item) for item in evidence_refs)),
                )
            )
    return dependencies


def route_identity(url: str) -> str:
    """Public wrapper used by browser state capture."""

    return _route_identity(url)


__all__ = [
    "classify_control",
    "descriptor_from_observation",
    "infer_control_dependencies",
    "page_state_fingerprint",
    "reconcile_page_states",
    "route_identity",
    "stable_control_id",
]
