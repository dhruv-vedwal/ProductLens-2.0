import pytest

from app.contracts.models import OperationKind, PageState, SemanticOperation, Target
from app.interaction.adapters import BehaviorAdapterRegistry
from app.interaction.state import (
    classify_control,
    descriptor_from_observation,
    infer_control_dependencies,
    page_state_fingerprint,
    reconcile_page_states,
    stable_control_id,
)


def test_behavior_classification_uses_observed_semantics_not_field_name():
    native = {"name": "bookingTime", "tag": "input", "type": "text", "role": "textbox"}
    slot = {
        "name": "Choose",
        "tag": "button",
        "role": "option",
        "time_value": "10:30",
    }

    assert classify_control(native)[0] == "text_input"
    assert classify_control(slot)[0] == "time_slot"


def test_stable_control_identity_survives_value_and_option_changes():
    base = {
        "name": "Doctor",
        "tag": "div",
        "role": "combobox",
        "ancestry": "form:booking>section:care",
        "geometry": {"x": 100, "y": 220, "width": 300, "height": 40},
    }

    before = stable_control_id({**base, "value": "", "options": []}, surface_id="page:booking")
    after = stable_control_id(
        {**base, "value": "Dr Rao", "options": ["Dr Rao"]}, surface_id="page:booking"
    )

    assert before == after


def test_reconciliation_reports_behavioral_control_changes():
    raw = {
        "name": "Branch",
        "tag": "div",
        "role": "combobox",
        "geometry": {"x": 20, "y": 40, "width": 200, "height": 40},
    }
    first = descriptor_from_observation(
        {**raw, "value": "", "disabled": False}, surface_id="page:booking"
    )
    second = descriptor_from_observation(
        {**raw, "value": "Main Branch", "disabled": False}, surface_id="page:booking"
    )
    before = PageState(
        id="before",
        url="https://example.test/bookings",
        route_identity="https://example.test/bookings",
        controls=[first],
    )
    after = PageState(
        id="after",
        url="https://example.test/bookings",
        route_identity="https://example.test/bookings",
        controls=[second],
    )
    before.fingerprint = page_state_fingerprint(before)
    after.fingerprint = page_state_fingerprint(after)

    transition = reconcile_page_states(before, after, action_intent_id="choose-branch")

    assert transition.changed_control_ids == [first.stable_id]
    assert transition.action_intent_id == "choose-branch"


def test_dependency_inference_requires_observed_child_state_change():
    parent = descriptor_from_observation(
        {"name": "Branch", "role": "combobox", "tag": "div"}, surface_id="page:booking"
    )
    child_before = descriptor_from_observation(
        {"name": "Doctor", "role": "combobox", "tag": "div", "disabled": True},
        surface_id="page:booking",
    )
    child_after = descriptor_from_observation(
        {
            "name": "Doctor",
            "role": "combobox",
            "tag": "div",
            "disabled": False,
            "options": ["Dr Rao"],
        },
        surface_id="page:booking",
    )
    before = PageState(
        id="before",
        url="https://example.test/bookings",
        controls=[parent, child_before],
    )
    after = PageState(
        id="after",
        url="https://example.test/bookings",
        controls=[parent, child_after],
    )

    dependencies = infer_control_dependencies(
        before, after, parent_control_id=parent.stable_id, evidence_refs=["probe:branch"]
    )

    assert dependencies[0].child_control_id == child_after.stable_id
    assert dependencies[0].effect == "enabled"


@pytest.mark.asyncio
async def test_dependent_field_adapter_refuses_disabled_child():
    descriptor = descriptor_from_observation(
        {
            "name": "Doctor",
            "tag": "div",
            "role": "combobox",
            "disabled": True,
            "depends_on": ["Branch"],
        },
        surface_id="page:booking",
    )
    operation = SemanticOperation(
        kind=OperationKind.SELECT_OPTION,
        intent="Choose the observed doctor",
        target=Target(name="Doctor"),
        value="Dr Rao",
    )

    with pytest.raises(ValueError, match="DEPENDENT_CONTROL_NOT_READY"):
        await BehaviorAdapterRegistry().execute(operation, descriptor, lambda _: None)


def test_stable_identity_differs_across_owning_surfaces():
    raw = {"name": "Save", "tag": "button", "role": "button"}
    assert stable_control_id(raw, surface_id="page:leads") != stable_control_id(
        raw, surface_id="modal:create-lead"
    )


def test_canvas_tool_and_surface_are_classified_separately():
    surface = {"tag": "canvas", "role": "application"}
    tool = {"name": "Arrow", "tag": "button", "role": "button", "canvas_tool": True}
    assert classify_control(surface)[0] == "canvas_surface"
    assert classify_control(tool)[0] == "canvas_tool"


@pytest.mark.asyncio
async def test_behavior_registry_records_visible_adapter_and_classification():
    descriptor = descriptor_from_observation(
        {"name": "Contact", "tag": "input", "type": "email"},
        surface_id="page:form",
    )
    operation = SemanticOperation(
        kind=OperationKind.FILL_EMAIL,
        intent="Enter the contact",
        target=Target(name="Contact"),
        value="demo@example.test",
    )

    async def dispatch(_operation):
        return {"typing_started": True}

    result = await BehaviorAdapterRegistry().execute(operation, descriptor, dispatch)

    assert result["behavior_adapter"] == "TextInputAdapter"
    assert result["behavior_class"] == "email_input"
    assert result["classification_confidence"] >= 0.6
