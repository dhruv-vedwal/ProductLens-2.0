"""DOM-backed initial form intelligence; browser validation remains authoritative."""

from __future__ import annotations

from app.contracts.models import FormField, FormSchema, ObservedElement


def _is_stable_field_evidence(item: ObservedElement) -> bool:
    """A fresh context must never target a generic control such as ``input``.

    Scoped browser discovery normally supplies an accessible name and an
    attribute/id selector. This fallback keeps old/resumable discovery records
    from compiling anonymous design-system internals into a mutation plan.
    """
    name = item.name.strip().casefold()
    if not name or name in {"on", "off", "x", "true", "false"} or name.isdecimal():
        return False
    if name.startswith("element-") and name[8:].isdecimal():
        return False
    return item.selector.startswith(("#", "["))


def infer_form_schema(elements: list[ObservedElement], source_url: str) -> FormSchema:
    fields: list[FormField] = []
    for item in elements:
        role = (item.role or "").casefold()
        if item.tag not in {"input", "select", "textarea"} and role not in {
            "combobox",
            "textbox",
            "checkbox",
            "radio",
        }:
            continue
        control_type = (
            role
            if role in {"combobox", "textbox", "checkbox", "radio"}
            else (item.element_type or item.tag).lower()
        )
        if control_type in {"hidden", "submit", "button", "reset"}:
            continue
        if not _is_stable_field_evidence(item):
            continue
        name = item.name.strip() or item.selector
        required_hint = item.required or "required" in name.lower() or "*" in name
        fields.append(
            FormField(
                name=name[:200],
                selector=item.selector,
                control_type=control_type,
                required=required_hint,
                options=item.options[:40],
                confidence=0.8 if item.name else 0.45,
            )
        )
    return FormSchema(
        source_url=source_url,
        fields=fields,
        evidence=["visible DOM controls", "semantic names and input types"],
    )
