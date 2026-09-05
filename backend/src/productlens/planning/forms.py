"""DOM-backed initial form intelligence; browser validation remains authoritative."""

from __future__ import annotations

from productlens.contracts.models import FormField, FormSchema, ObservedElement


def infer_form_schema(elements: list[ObservedElement], source_url: str) -> FormSchema:
    fields: list[FormField] = []
    for item in elements:
        if item.tag not in {"input", "select", "textarea"}:
            continue
        control_type = (item.element_type or item.tag).lower()
        if control_type in {"hidden", "submit", "button", "reset"}:
            continue
        name = item.name.strip() or item.selector
        required_hint = item.required or "required" in name.lower() or "*" in name
        fields.append(
            FormField(
                name=name[:200], selector=item.selector, control_type=control_type,
                required=required_hint, options=item.options[:40], confidence=0.8 if item.name else 0.45,
            )
        )
    return FormSchema(
        source_url=source_url,
        fields=fields,
        evidence=["visible DOM controls", "semantic names and input types"],
    )
