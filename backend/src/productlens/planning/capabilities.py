"""Compile verified, product-neutral form capabilities into semantic steps."""

from __future__ import annotations

import re

from productlens.contracts.models import (
    ActionCapability,
    FormField,
    OperationKind,
    Postcondition,
    SemanticOperation,
    Target,
)
from productlens.planning.form_dependencies import FormDependencyError, order_form_fields


class CapabilityCompilationError(ValueError):
    pass


_TRANSIENT_REACT_ID = re.compile(r"^#:r[0-9a-z]+:$", re.IGNORECASE)


def _is_transient_selector(selector: str) -> bool:
    """Reject React/MUI generated ids whether CSS escaping is present or not."""
    return bool(_TRANSIENT_REACT_ID.fullmatch(selector.replace("\\", "")))


def _field_target(field: FormField, source_url: str) -> Target:
    """Prefer a label/name over a broad tag selector for production grounding."""
    # A selector emitted by an SPA mount (for example ``#\\:r30\\:``) cannot
    # be trusted in a fresh production context. Labels remain the primary
    # identity; retain only stable attribute/id selectors as bounded hints.
    selector = (
        field.selector
        if field.selector.startswith(("#", "[")) and not _is_transient_selector(field.selector)
        else None
    )
    return Target(
        name=field.name,
        label=field.name,
        selector=selector,
        source_url=source_url,
    )


def _operation_for(field: FormField, source_url: str) -> SemanticOperation:
    target = _field_target(field, source_url)
    control = field.control_type.casefold()
    name = field.name.casefold()
    # Radio/checkbox inputs are option groups, not text fields.  Without an
    # observed option value, treating their labels as fill targets produces
    # invalid operations such as typing into ``Old`` or ``Yes``.  Leave these
    # controls as visible form evidence; a later capability probe can compile
    # an explicit choice when the group semantics are known.
    if control in {"radio", "checkbox", "switch", "hidden"}:
        raise CapabilityCompilationError(
            f"Form control has no deterministic editable value: {field.name}"
        )
    if name in {"type", "id"} and control in {"input", "string", "text"}:
        raise CapabilityCompilationError(
            f"Form control is structural metadata, not an editable field: {field.name}"
        )
    if control in {"select", "combobox"}:
        options = [
            item
            for item in field.options
            if item.strip()
            and item.strip().casefold()
            not in {
                "select",
                "select an option",
                "choose",
                "choose an option",
            }
        ]
        if not options:
            raise CapabilityCompilationError(
                f"Selectable field has no safe observed option: {field.name}"
            )
        value = options[0]
        kind = OperationKind.SELECT_OPTION
    elif "email" in control or "email" in name:
        value, kind = None, OperationKind.FILL_EMAIL
    elif any(token in f"{control} {name}" for token in ("phone", "mobile", "telephone", "tel")):
        value, kind = None, OperationKind.FILL_PHONE
    elif control == "date" or "date" in name:
        value, kind = None, OperationKind.SELECT_DATE
    else:
        value, kind = None, OperationKind.FILL_TEXT
    return SemanticOperation(
        kind=kind,
        intent=f"Enter the observed {field.name} value",
        target=target,
        value=value,
        postconditions=[Postcondition(kind="value", expected=value, target=target)],
        story_phase="demonstrate",
        page_url=source_url,
        evidence_refs=[f"form-field:{source_url}:{field.name}"],
    )


def _explicitly_optional(field: FormField) -> bool:
    """Return true only for UI text that expressly marks a field optional.

    A missing native ``required`` attribute is not proof that a custom
    combobox is optional.  ProductLens may omit an explicitly optional choice
    from a minimal safe record, but it must not submit past an unnamed,
    optionless selector and hope the server accepts it.
    """
    name = field.name.casefold()
    return "optional" in name or "not required" in name


def _compile(capability: ActionCapability, *, require_outcome: bool) -> list[SemanticOperation]:
    """Return an executable create flow only when its independent outcome is known.

    Discovery can safely learn a modal's fields without submitting it. A later
    authorised rehearsal may set ``verified`` and ``outcome_target``. This
    boundary prevents a form-opening observation from becoming a fake product
    outcome in a final demo.
    """
    if capability.kind != "form" or capability.form_schema is None:
        raise CapabilityCompilationError("A form capability is required for record creation")
    if capability.form_schema.unresolved_required_fields:
        raise CapabilityCompilationError(
            "Creation capability has visible required controls without a safely grounded field schema"
        )
    unresolved_choices = [
        field.name
        for field in capability.form_schema.fields
        if field.control_type.casefold() in {"select", "combobox"}
        and not field.options
        and not field.required
        and not _explicitly_optional(field)
    ]
    if unresolved_choices:
        raise CapabilityCompilationError(
            "Creation capability has non-optional selectable fields without safe observed choices: "
            + ", ".join(unresolved_choices)
        )
    if capability.submit_target is None or (
        require_outcome and (not capability.verified or capability.outcome_target is None)
    ):
        raise CapabilityCompilationError(
            "Creation capability lacks independently verified outcome evidence"
        )
    required_fields = [field for field in capability.form_schema.fields if field.required]
    # When the UI does not expose native requiredness, use the minimal form
    # surface that is not explicitly optional. This includes visible business
    # inputs and a required-by-behaviour selector such as a source/category,
    # while avoiding unnecessary branch/assignee/remark changes in a demo.
    candidate_fields = required_fields or [
        field for field in capability.form_schema.fields if not _explicitly_optional(field)
    ]
    if not candidate_fields and required_fields:
        raise CapabilityCompilationError("Creation capability has no observed editable fields")
    # A form can expose optional dependent controls (for example a Branch
    # combobox whose values are only available after a required field).  They
    # are evidence, not permission to invent a choice. Required controls must
    # compile; optional controls with no safe observed value are simply not
    # part of the minimal, safe rehearsal.
    field_operations: list[SemanticOperation] = []
    try:
        candidate_fields = order_form_fields(candidate_fields)
    except FormDependencyError as error:
        raise CapabilityCompilationError(str(error)) from error
    for field in candidate_fields[:8]:
        try:
            field_operations.append(_operation_for(field, capability.source_url))
        except CapabilityCompilationError:
            if field.required:
                raise
    if not field_operations and required_fields:
        raise CapabilityCompilationError(
            "Creation capability has no safely compilable editable fields"
        )
    opening = SemanticOperation(
        kind=OperationKind.OPEN_MODAL,
        intent=f"Open the {capability.purpose} form",
        target=capability.entry_target,
        # Some products deliberately start a record with optional metadata
        # only, then open its detail state after submit. Do not invent values
        # for those controls. The authorised rehearsal may prove that native
        # empty-state transition once; required unresolved controls still fail
        # above, and production remains forbidden until an outcome witness is
        # persisted.
        postconditions=[
            Postcondition(
                kind="visible",
                expected=True,
                target=field_operations[0].target if field_operations else capability.submit_target,
            )
        ],
        story_phase="demonstrate",
        page_url=capability.source_url,
        evidence_refs=capability.evidence_refs,
    )
    operations = [opening]
    operations.extend(field_operations)
    outcome = capability.outcome_target
    outcome_postconditions: list[Postcondition] = []
    if outcome is not None:
        # A verified form may naturally land on a newly-created entity's
        # detail route. Preserve that proof as a route postcondition rather
        # than pretending the target is still visible on the source page.
        if outcome.source_url and outcome.source_url != capability.source_url:
            outcome_postconditions.append(
                Postcondition(
                    kind="url",
                    expected=outcome.source_url,
                    target=outcome,
                )
            )
        else:
            outcome_postconditions.append(
                Postcondition(
                    kind="visible",
                    expected=True,
                    target=outcome,
                )
            )
    operations.append(
        SemanticOperation(
            kind=OperationKind.SUBMIT,
            intent=f"Create and verify the {capability.purpose} record",
            target=capability.submit_target,
            postconditions=outcome_postconditions,
            story_phase="verify",
            page_url=capability.source_url,
            evidence_refs=[*capability.evidence_refs, *capability.outcome_evidence],
        )
    )
    return operations


def compile_rehearsal_operations(capability: ActionCapability) -> list[SemanticOperation]:
    """Compile a one-time authorised rehearsal without claiming an outcome."""
    return _compile(capability, require_outcome=False)


def compile_read_only_form_inspection(capability: ActionCapability) -> list[SemanticOperation]:
    """Compile a safe form walkthrough without submitting or inventing choices.

    Discovery may find a useful form whose dependent comboboxes do not yet
    expose options (for example, options loaded after a branch is chosen).
    That should not prevent a prospect demo from opening the form and showing
    the grounded fields that are actually visible.  This compiler deliberately
    emits only the reversible open plus fields with deterministic safe values;
    it never emits a submit and never treats an unobserved option as evidence.
    """
    if capability.kind != "form" or capability.form_schema is None:
        raise CapabilityCompilationError("A form capability is required for inspection")
    if capability.entry_target is None:
        raise CapabilityCompilationError("Form capability has no visible entry target")
    field_operations: list[SemanticOperation] = []
    try:
        fields = order_form_fields(capability.form_schema.fields)
    except FormDependencyError as error:
        raise CapabilityCompilationError(str(error)) from error
    for field in fields[:8]:
        try:
            field_operations.append(_operation_for(field, capability.source_url))
        except CapabilityCompilationError:
            # An unresolved choice is still useful evidence on screen, but it
            # is not a deterministic operation. Leave it to the page-local
            # screenshot/DOM evidence rather than guessing a value.
            continue
    if not field_operations:
        raise CapabilityCompilationError("Form capability has no safely inspectable fields")
    opening = SemanticOperation(
        kind=OperationKind.OPEN_MODAL,
        intent=f"Open the observed {capability.purpose} form to explain its setup fields",
        target=capability.entry_target,
        postconditions=[
            Postcondition(
                kind="visible",
                expected=True,
                target=field_operations[0].target,
            )
        ],
        story_phase="demonstrate",
        page_url=capability.source_url,
        evidence_refs=capability.evidence_refs,
    )
    return [opening, *field_operations]


def compile_record_creation(capability: ActionCapability) -> list[SemanticOperation]:
    """Compile a production creation flow only after rehearsal proves outcome."""
    return _compile(capability, require_outcome=True)
