"""Compile verified, product-neutral form capabilities into semantic steps."""

from __future__ import annotations

import re

from app.contracts.models import (
    ActionCapability,
    FormField,
    OperationKind,
    Postcondition,
    SemanticOperation,
    Target,
)
from app.planning.form_dependencies import FormDependencyError, order_form_fields


class CapabilityCompilationError(ValueError):
    pass


_TRANSIENT_REACT_ID = re.compile(r"^#:r[0-9a-z]+:$", re.IGNORECASE)


def _is_transient_selector(selector: str) -> bool:
    """Reject React/MUI generated ids whether CSS escaping is present or not."""
    return bool(_TRANSIENT_REACT_ID.fullmatch(selector.replace("\\", "")))


def _is_well_formed_selector(selector: str) -> bool:
    """Reject truncated attribute selectors from dynamic accessibility probes."""
    return selector.count("[") == selector.count("]") and selector.count('"') % 2 == 0


def _field_target(field: FormField, source_url: str) -> Target:
    """Prefer a label/name over a broad tag selector for production grounding."""
    # A selector emitted by an SPA mount (for example ``#\\:r30\\:``) cannot
    # be trusted in a fresh production context. Labels remain the primary
    # identity; retain only stable attribute/id selectors as bounded hints.
    selector = (
        field.selector
        if (
            field.selector.startswith(("#", "["))
            and _is_well_formed_selector(field.selector)
            and not _is_transient_selector(field.selector)
        )
        else None
    )
    control = field.control_type.casefold()
    behavior = (field.behavior_class or "").casefold()
    role = None
    if control in {"select", "combobox"} or behavior in {
        "native_select",
        "combobox",
        "autocomplete",
        "time_slot",
    }:
        role = "combobox"
    elif control == "radio":
        role = "radio"
    return Target(
        name=field.name,
        label=field.name,
        selector=selector,
        role=role,
        source_url=source_url,
    )


def _operation_for(field: FormField, source_url: str) -> SemanticOperation:
    target = _field_target(field, source_url)
    control = field.control_type.casefold()
    behavior = field.behavior_class.casefold()
    # Checkboxes/switches require an explicit policy (for example consent) and
    # hidden controls are never user actions.  A radio is different: the live
    # rehearsal gives us a unique option selector, so choosing that observed
    # option is a deterministic, replayable interaction.
    if control in {"checkbox", "switch", "hidden"}:
        raise CapabilityCompilationError(
            f"Form control has no deterministic editable value: {field.name}"
        )
    if control == "radio":
        return SemanticOperation(
            kind=OperationKind.CHOOSE_RADIO,
            intent=f"Choose the observed {field.name} option",
            target=target,
            value=True,
            postconditions=[Postcondition(kind="changed", expected=True, target=target)],
            story_phase="demonstrate",
            page_url=source_url,
            evidence_refs=[f"form-field:{source_url}:{field.name}"],
        )
    if control in {"select", "combobox"} or behavior in {
        "native_select",
        "combobox",
        "autocomplete",
        "time_slot",
    }:
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
        observed = (field.observed_value or "").strip()
        matching_observed = next(
            (item for item in options if item.casefold() == observed.casefold()), None
        )
        if matching_observed is not None:
            value = matching_observed
        else:
            # Discovery may observe a choice list without a pre-selected value
            # (empty required comboboxes). Pick the same deterministic option
            # used by dependency probing so rehearsal stays evidence-backed
            # and product-neutral rather than omitting the control.
            value = min(options, key=str.casefold)
        kind = OperationKind.SELECT_OPTION
    elif control == "email" or behavior == "email_input":
        value, kind = None, OperationKind.FILL_EMAIL
    elif control in {"phone", "tel"} or behavior == "phone_input":
        value, kind = None, OperationKind.FILL_PHONE
    elif control == "date" or behavior in {"native_date", "date_picker"}:
        value, kind = None, OperationKind.SELECT_DATE
    else:
        value, kind = None, OperationKind.FILL_TEXT
    return SemanticOperation(
        kind=kind,
        intent=f"Enter the observed {field.name} value",
        target=target,
        value=value,
        # Custom comboboxes frequently do not expose the selected value through
        # the native input value (the visible label is rendered in a portal).
        # Require a DOM state change for those controls and let the live
        # grounding/verifier inspect the resulting widget state.  Text/date
        # controls retain exact value verification because their value is the
        # evidence we need to prove a filled field.
        postconditions=(
            [Postcondition(kind="value", expected=value, target=target)]
            if field.required
            else [Postcondition(kind="changed", expected=True, target=target)]
        ),
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
    # Recovery may mark a combobox for inclusion before its option list is
    # persisted. Prefer the rehearsed observed value as a one-item choice list
    # rather than failing closed after a successful live selection.
    healed_fields: list[FormField] = []
    for field in capability.form_schema.fields:
        if (
            field.control_type.casefold() in {"select", "combobox"}
            and not field.options
            and (field.observed_value or "").strip()
        ):
            healed_fields.append(
                field.model_copy(update={"options": [str(field.observed_value).strip()]})
            )
        else:
            healed_fields.append(field)
    if healed_fields != list(capability.form_schema.fields):
        capability = capability.model_copy(
            update={"form_schema": capability.form_schema.model_copy(update={"fields": healed_fields})}
        )
    has_required_field = any(field.required for field in capability.form_schema.fields)
    has_non_choice_field = any(
        field.control_type.casefold() not in {"select", "combobox"}
        for field in capability.form_schema.fields
    )
    unresolved_choices = [
        field.name
        for field in capability.form_schema.fields
        if field.control_type.casefold() in {"select", "combobox"}
        and not field.options
        and not _explicitly_optional(field)
        # A custom combobox may be optional at the initial state and only
        # become relevant after a dependency is chosen (or after the first
        # validation submit). Do not reject an otherwise usable capability
        # merely because discovery could not enumerate a closed widget. The
        # rehearsal recovery pass re-observes the live listbox and promotes a
        # field only when the application proves it is required. Natively
        # required or explicitly promoted controls still fail closed here.
        and (
            field.required
            or "rehearsal:include" in field.validation_messages
            # If a form consists only of unobserved selectors, there is no
            # safe minimal flow to compile. Keep the fail-closed boundary for
            # that case while allowing mixed forms to demonstrate their
            # observed text fields without inventing an optional choice.
            or (not has_required_field and not has_non_choice_field)
        )
    ]
    if unresolved_choices:
        raise CapabilityCompilationError(
            "Creation capability has no safe observed option for non-optional selectable fields: "
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
        field
        for field in capability.form_schema.fields
        if not _explicitly_optional(field)
        and field.control_type.casefold() not in {"radio", "checkbox", "switch", "hidden"}
        and field.name.casefold() not in {"type", "id"}
    ]
    # A successful rehearsal can reveal dependent controls that are not
    # natively marked required (custom widgets commonly omit that metadata).
    # Include only fields explicitly promoted by the rehearsal boundary; this
    # avoids broadening every form with unrelated optional inputs.
    promoted = [
        field
        for field in capability.form_schema.fields
        if "rehearsal:include" in field.validation_messages
        and not _explicitly_optional(field)
        and field.control_type.casefold() not in {"checkbox", "switch", "hidden"}
        and field.name.casefold() not in {"type", "id"}
    ]
    seen_fields: set[tuple[str, str | None]] = set()
    seen_choice_groups: set[str] = set()
    unique_fields: list[FormField] = []
    for field in [*candidate_fields, *promoted]:
        if field.control_type.casefold() == "radio":
            # A group is represented by one observed option during rehearsal;
            # compiling every label would select mutually exclusive values in
            # sequence. The selector contains the option value, so it is the
            # stable group identity even when labels are duplicated.
            group_selector = re.sub(r"\[value=(?:\\?['\"]).*", "", field.selector or "")
            group_selector = group_selector or (field.selector or field.name)
            if group_selector in seen_choice_groups:
                continue
            seen_choice_groups.add(group_selector)
        key = (field.name.casefold(), field.selector)
        if key in seen_fields:
            continue
        seen_fields.add(key)
        unique_fields.append(field)
    candidate_fields = unique_fields
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
    # The rehearsal boundary already scoped these controls to the selected
    # capability. Do not silently drop fields at an arbitrary eight-control
    # cutoff (which can omit required identity/date fields after dependency
    # choices are ordered). Keep the browser-side discovery bound as the only
    # generic safety cap.
    for field in candidate_fields[:32]:
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
        # Read-only inspection must not mutate option groups.  A radio choice
        # is compiled only after the authorised rehearsal explicitly promotes
        # the observed option into the production capability.
        if field.control_type.casefold() in {"radio", "checkbox", "switch", "hidden"}:
            continue
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
