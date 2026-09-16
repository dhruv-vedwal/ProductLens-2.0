import pytest

from app.contracts.models import (
    ActionCapability,
    FormField,
    FormSchema,
    ObjectiveSpec,
    ObservedElement,
    PageKnowledge,
    ProductContext,
    Target,
)
from app.planning.capabilities import (
    CapabilityCompilationError,
    _field_target,
    compile_read_only_form_inspection,
    compile_record_creation,
    compile_rehearsal_operations,
)
from app.planning.rehearsal import (
    CapabilitySelectionError,
    derive_outcome_witness,
    select_rehearsal_capability,
)
from app.planning.side_effects import SideEffectPolicyError, authorize_operation
from app.services.generation import _rehearsal_detail_navigation_witness


def _capability() -> ActionCapability:
    return ActionCapability(
        kind="form",
        purpose="appointment",
        source_url="https://example.test/appointments",
        entry_target=Target(name="New appointment", selector="#new"),
        form_schema=FormSchema(
            source_url="https://example.test/appointments",
            fields=[
                FormField(
                    name="Customer", selector="#customer", control_type="text", required=True
                ),
            ],
        ),
        submit_target=Target(name="Save appointment", selector="#save"),
    )


def test_rehearsal_requires_a_new_visible_outcome_not_the_submit_button():
    capability = _capability()
    witnessed = derive_outcome_witness(
        capability,
        before_text="Appointments New appointment Save appointment",
        observed=[ObservedElement(tag="button", name="Save appointment", selector="#save")],
    )
    assert witnessed is None


def test_rehearsal_promotes_only_a_new_visible_success_witness():
    capability = _capability()
    witnessed = derive_outcome_witness(
        capability,
        before_text="Appointments New appointment Save appointment",
        observed=[
            ObservedElement(
                tag="div",
                role="status",
                name="Appointment created successfully",
                selector='[role="status"]',
                text="Appointment created successfully",
                source_url=capability.source_url,
            )
        ],
    )
    assert witnessed is not None
    assert witnessed.verified
    assert witnessed.outcome_target.name == "Appointment created successfully"
    assert "rehearsal-visible-outcome" in witnessed.outcome_evidence[0]


def test_rehearsal_does_not_treat_the_open_form_as_a_created_record():
    capability = _capability()
    opened_form = "Create appointment Customer Save appointment Required field"
    witnessed = derive_outcome_witness(
        capability,
        before_text=f"Appointments {opened_form}",
        observed=[
            ObservedElement(
                tag="div",
                role="dialog",
                name="Create appointment",
                text=opened_form,
                selector='[role="dialog"]',
                source_url=capability.source_url,
            )
        ],
    )
    assert witnessed is None


def test_rehearsal_rejects_a_dialog_even_when_new_validation_copy_appears_after_submit():
    capability = _capability()
    witnessed = derive_outcome_witness(
        capability,
        before_text="Appointments Create appointment Customer Save appointment",
        observed=[
            ObservedElement(
                tag="div",
                role="dialog",
                name="Create appointment",
                text="Create appointment Customer Required field Save appointment",
                selector='[role="dialog"]',
                source_url=capability.source_url,
            )
        ],
    )
    assert witnessed is None


def test_rehearsal_accepts_an_explicit_success_confirmation_dialog():
    capability = _capability()
    witnessed = derive_outcome_witness(
        capability,
        before_text="Appointments Create appointment Customer Save appointment",
        observed=[
            ObservedElement(
                tag="div",
                role="dialog",
                name="Appointment created Pending confirmation Saved successfully Close",
                text="Appointment created Pending confirmation Saved successfully Close",
                selector='[role="dialog"]',
                source_url=capability.source_url,
            )
        ],
    )
    assert witnessed is not None
    assert witnessed.outcome_target.role == "dialog"
    assert witnessed.outcome_target.name == "Appointment created"


def test_rehearsal_accepts_a_new_structural_row_with_the_generated_record_value():
    capability = _capability()
    witnessed = derive_outcome_witness(
        capability,
        before_text="Appointments New appointment Save appointment",
        submitted_values=["Maya Shah", "9852239496"],
        observed=[
            ObservedElement(
                tag="tr",
                role="row",
                name="Maya Shah 985 223 9496 Pending",
                text="Maya Shah 985 223 9496 Pending",
                selector="tr",
                source_url=capability.source_url,
            )
        ],
    )

    assert witnessed is not None
    assert witnessed.verified
    assert witnessed.outcome_target.role == "row"
    assert witnessed.outcome_target.name == "verified created record"
    assert witnessed.outcome_target.text == "Maya Shah"
    assert witnessed.outcome_evidence[-1] == "rehearsal-visible-outcome:verified-created-record"


def test_rehearsal_does_not_treat_a_non_result_container_with_a_generated_value_as_proof():
    capability = _capability()
    witnessed = derive_outcome_witness(
        capability,
        before_text="Appointments New appointment Save appointment",
        submitted_values=["Maya Shah"],
        observed=[
            ObservedElement(
                tag="div",
                name="Maya Shah",
                text="Maya Shah",
                selector="div",
                source_url=capability.source_url,
            )
        ],
    )

    assert witnessed is None


def test_rehearsal_accepts_a_stable_card_containing_the_unique_generated_value():
    capability = _capability()
    witnessed = derive_outcome_witness(
        capability,
        before_text="Appointments New appointment Save appointment",
        submitted_values=["Maya Shah"],
        observed=[
            ObservedElement(
                tag="div",
                name="Maya Shah Pending",
                text="Maya Shah Pending",
                selector='[data-testid="appointment-card"]',
                source_url=capability.source_url,
            )
        ],
    )

    assert witnessed is not None
    assert witnessed.verified
    assert witnessed.outcome_target.name == "verified created record"


def test_rehearsal_accepts_same_origin_post_submit_detail_navigation():
    capability = _capability()

    witnessed = _rehearsal_detail_navigation_witness(
        capability,
        before_url="https://example.test/appointments?status=open",
        after_url="https://example.test/appointments/record-123?created=true#overview",
    )

    assert witnessed is not None
    assert witnessed.verified
    assert witnessed.outcome_target.name == "verified created record"
    assert witnessed.outcome_target.source_url == "https://example.test/appointments/record-123"
    assert (
        witnessed.outcome_evidence[-1] == "rehearsal-visible-outcome:post-submit-detail-navigation"
    )


def test_rehearsal_rejects_cross_origin_post_submit_navigation_as_a_creation_witness():
    assert (
        _rehearsal_detail_navigation_witness(
            _capability(),
            before_url="https://example.test/appointments",
            after_url="https://other.example/appointments/record-123",
        )
        is None
    )


def test_verified_detail_navigation_becomes_a_url_postcondition_for_production():
    capability = _capability().model_copy(
        update={
            "verified": True,
            "outcome_target": Target(
                name="verified created record",
                source_url="https://example.test/appointments/record-123",
            ),
        }
    )

    submit = compile_record_creation(capability)[-1]
    assert submit.postconditions[0].kind == "url"
    assert submit.postconditions[0].expected == "https://example.test/appointments/record-123"


def test_rehearsal_compiled_submit_is_still_blocked_when_it_sends_externally():
    capability = _capability().model_copy(
        update={
            "submit_target": Target(name="Send appointment confirmation", selector="#send"),
        }
    )
    submit = compile_rehearsal_operations(capability)[-1]
    try:
        authorize_operation(submit, True)
    except SideEffectPolicyError:
        return
    raise AssertionError("external rehearsal submission must be blocked")


def test_read_only_form_inspection_skips_unresolved_choice_groups_and_submit():
    capability = _capability().model_copy(
        update={
            "form_schema": FormSchema(
                source_url="https://example.test/appointments",
                fields=[
                    FormField(name="Customer", selector="#customer", control_type="text"),
                    FormField(name="Existing", selector="#existing", control_type="radio"),
                    FormField(name="Branch", selector="#branch", control_type="combobox"),
                ],
            ),
        }
    )
    operations = compile_read_only_form_inspection(capability)
    assert [operation.kind.value for operation in operations] == ["OpenModal", "FillText"]
    assert all(operation.kind.value != "Submit" for operation in operations)


def test_rehearsal_selects_the_feature_page_not_an_unrelated_form_by_probe_order():
    lead_url = "https://example.test/leads"
    template_url = "https://example.test/lead-templates"
    lead = _capability().model_copy(
        update={
            "purpose": "New lead",
            "source_url": lead_url,
            "entry_target": Target(name="New lead", selector="#new-lead", source_url=lead_url),
            "submit_target": Target(name="Save lead", selector="#save-lead", source_url=lead_url),
        }
    )
    template = _capability().model_copy(
        update={
            "purpose": "New template",
            "source_url": template_url,
            "entry_target": Target(
                name="New template", selector="#new-template", source_url=template_url
            ),
            "submit_target": Target(
                name="Save template", selector="#save-template", source_url=template_url
            ),
        }
    )
    context = ProductContext(
        url="https://example.test",
        title="Example",
        application_type="web_application",
        objective=ObjectiveSpec(
            raw="Create an isolated lead-management demo record", primary_entity="lead management"
        ),
        page_knowledge=[
            PageKnowledge(
                url=lead_url, title="Leads", purpose="Lead management", fingerprint="leads"
            ),
            PageKnowledge(
                url=template_url,
                title="Lead templates",
                purpose="Reusable lead templates",
                fingerprint="templates",
            ),
        ],
    )
    assert select_rehearsal_capability(context, [template, lead]).id == lead.id


def test_rehearsal_rejects_ambiguous_feature_forms_instead_of_guessing():
    first = _capability().model_copy(update={"source_url": "https://example.test/a"})
    second = _capability().model_copy(update={"source_url": "https://example.test/b"})
    context = ProductContext(
        url="https://example.test",
        title="Example",
        application_type="web_application",
        objective=ObjectiveSpec(
            raw="Create an isolated appointment demo record", primary_entity="appointment"
        ),
        page_knowledge=[
            PageKnowledge(
                url="https://example.test/a",
                title="Appointment",
                purpose="Appointment",
                fingerprint="a",
            ),
            PageKnowledge(
                url="https://example.test/b",
                title="Appointment",
                purpose="Appointment",
                fingerprint="b",
            ),
        ],
    )
    with pytest.raises(CapabilitySelectionError, match="ambiguous"):
        select_rehearsal_capability(context, [first, second])


def test_transient_component_id_is_not_used_as_a_fresh_context_selector():
    target = _field_target(
        FormField(name="Name", selector=r"#\:r30\:", control_type="text"),
        "https://example.test",
    )
    assert target.selector is None
    stable = _field_target(
        FormField(name="Name", selector='[name="name"]', control_type="text"),
        "https://example.test",
    )
    assert stable.selector == '[name="name"]'


def test_rehearsal_omits_optional_selectors_without_observed_choices():
    capability = _capability().model_copy(
        update={
            "form_schema": FormSchema(
                source_url="https://example.test/appointments",
                fields=[
                    FormField(
                        name="Branch (Optional)", selector="#branch", control_type="combobox"
                    ),
                    FormField(name="Customer", selector="#customer", control_type="text"),
                ],
            ),
        }
    )

    operations = compile_rehearsal_operations(capability)

    assert [operation.target.name for operation in operations] == [
        "New appointment",
        "Customer",
        "Save appointment",
    ]


def test_rehearsal_omits_explicitly_optional_choices_even_when_their_options_are_observed():
    capability = _capability().model_copy(
        update={
            "form_schema": FormSchema(
                source_url="https://example.test/appointments",
                fields=[
                    FormField(name="Customer", selector="#customer", control_type="text"),
                    FormField(
                        name="Branch (Optional)",
                        selector="#branch",
                        control_type="combobox",
                        options=["North"],
                    ),
                ],
            ),
        }
    )

    operations = compile_rehearsal_operations(capability)

    assert [operation.target.name for operation in operations] == [
        "New appointment",
        "Customer",
        "Save appointment",
    ]


def test_rehearsal_still_rejects_required_selector_without_observed_choices():
    capability = _capability().model_copy(
        update={
            "form_schema": FormSchema(
                source_url="https://example.test/appointments",
                fields=[
                    FormField(
                        name="Branch", selector="#branch", control_type="combobox", required=True
                    )
                ],
            ),
        }
    )

    with pytest.raises(CapabilityCompilationError, match="no safe observed option"):
        compile_rehearsal_operations(capability)


def test_rehearsal_rejects_an_unmarked_combobox_without_an_observed_choice():
    capability = _capability().model_copy(
        update={
            "form_schema": FormSchema(
                source_url="https://example.test/appointments",
                fields=[
                    FormField(name="Source", selector="#source", control_type="combobox"),
                    FormField(
                        name="Branch (Optional)", selector="#branch", control_type="combobox"
                    ),
                ],
            ),
        }
    )

    with pytest.raises(CapabilityCompilationError, match="non-optional selectable"):
        compile_rehearsal_operations(capability)


def test_rehearsal_rejects_a_schema_with_visible_but_unresolved_required_controls():
    capability = _capability().model_copy(
        update={
            "form_schema": FormSchema(
                source_url="https://example.test/appointments",
                fields=[
                    FormField(name="Owner (Optional)", selector="#owner", control_type="combobox")
                ],
                unresolved_required_fields=True,
            ),
        }
    )

    with pytest.raises(CapabilityCompilationError, match="visible required controls"):
        compile_rehearsal_operations(capability)


def test_rehearsal_can_prove_an_authorised_optional_only_creation_state():
    capability = _capability().model_copy(
        update={
            "form_schema": FormSchema(
                source_url="https://example.test/appointments",
                fields=[
                    FormField(name="Owner (Optional)", selector="#owner", control_type="combobox")
                ],
            ),
        }
    )

    operations = compile_rehearsal_operations(capability)

    assert [operation.kind.value for operation in operations] == ["OpenModal", "Submit"]
    assert operations[0].postconditions[0].target.name == "Save appointment"
