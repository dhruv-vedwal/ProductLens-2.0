from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from productlens.contracts.models import (
    ActionIntent,
    AudienceProfile,
    DemoPlan,
    DemoTrace,
    InteractionEvent,
    NarrationScript,
    OperationKind,
    Postcondition,
    Target,
    WorkflowState,
)


def test_audience_profile_is_bounded_and_serializable():
    profile = AudienceProfile(
        type="recruiter",
        vocabulary="executive",
        depth="overview",
        priorities=["career progression"],
        narration_style="concise",
    )
    assert profile.model_dump()["type"] == "recruiter"
    with pytest.raises(ValidationError):
        AudienceProfile(type="unknown")


def test_demo_plan_rejects_unstructured_fields():
    step = {
        "id": "one",
        "intent": "Open",
        "operation": {"kind": "Click", "intent": "Open", "target": {"name": "Open"}},
    }
    plan = DemoPlan(
        objective="Create a lead",
        narrative_goal="Show lead capture",
        audience="Sales",
        target_duration_seconds=30,
        selected_workflow="CreateLead",
        workflow_steps=[step],
        expected_outcomes=["Lead exists"],
        viewport_strategy="focus actions",
        stop_conditions=["lead exists"],
    )
    assert plan.workflow_steps[0].operation.kind is OperationKind.CLICK
    with pytest.raises(ValidationError):
        DemoPlan(**{**plan.model_dump(), "raw_browser_script": "click(1,2)"})
    with pytest.raises(ValidationError, match="duration envelope"):
        DemoPlan(
            **{**plan.model_dump(), "minimum_duration_seconds": 45, "maximum_duration_seconds": 90}
        )


def test_trace_requires_semantic_before_after_and_success():
    trace = DemoTrace(run_id="run-1", objective="Create a lead", started_at=datetime.now(UTC))
    trace.events.append(
        InteractionEvent(
            operation_id="op-1",
            kind=OperationKind.CLICK,
            intent="Open lead form",
            target=Target(name="New lead", test_id="new-lead-btn"),
            before={"url": "x"},
            after={"modal": True},
            success=True,
            duration_ms=12,
        )
    )
    trace.final_state = WorkflowState.ACTION_IN_PROGRESS
    assert trace.events[0].before != trace.events[0].after
    assert trace.events[0].target.test_id == "new-lead-btn"


def test_narration_script_is_versioned_and_rejects_invalid_timing():
    script = NarrationScript(
        segments=[
            {
                "scene_id": "scene-1",
                "event_id": "event-1",
                "text": "This opening view establishes the product context for the viewer.",
                "evidence": ["page:https://example.test/"],
                "start_seconds": 0,
                "end_seconds": 3,
            }
        ]
    )
    assert script.schema_version == 1
    assert script.timing_owner == "scene"
    with pytest.raises(ValidationError):
        NarrationScript(
            segments=[
                {
                    "scene_id": "scene-1",
                    "event_id": "event-1",
                    "text": "This opening view establishes the product context for the viewer.",
                    "start_seconds": 4,
                    "end_seconds": 2,
                }
            ]
        )


def test_action_intent_compiles_runtime_gesture_without_domain_vocabulary():
    intent = ActionIntent(
        goal="Connect the observed source to the destination",
        gesture="drag",
        target=Target(name="source connector", role="button"),
        destination=Target(name="destination connector", role="button"),
        parameters={"duration_ms": 650},
        evidence_refs=["dom:connector-source", "dom:connector-destination"],
    )
    operation = intent.to_operation()
    assert operation.kind is OperationKind.DRAG
    assert operation.side_effect_policy == "read_only"
    assert operation.value["destination"]["name"] == "destination connector"
    assert operation.evidence_refs == ["dom:connector-source", "dom:connector-destination"]


def test_action_intent_rejects_ungrounded_drag_or_blocked_dispatch():
    with pytest.raises(ValidationError, match="destination"):
        ActionIntent(
            goal="Move the observed item",
            gesture="drag",
            target=Target(name="item"),
        )
    with pytest.raises(ValidationError, match="blocked side-effect"):
        ActionIntent(
            goal="Submit the observed form",
            gesture="click",
            target=Target(name="submit"),
            side_effect_policy="blocked",
        )


def test_action_intent_supports_targetless_state_verification():
    intent = ActionIntent(
        goal="Confirm the route changed",
        gesture="verify",
        expected_state=[Postcondition(kind="url", expected="https://example.test/done")],
    )
    operation = intent.to_operation()
    assert operation.kind is OperationKind.VERIFY_STATE
    assert operation.target is None


def test_action_intent_compiles_generic_upload_gesture():
    intent = ActionIntent(
        goal="Upload the observed document",
        gesture="upload",
        target=Target(name="Document upload", role="button"),
        value="/tmp/document.pdf",
    )
    assert intent.to_operation().kind is OperationKind.UPLOAD


def test_action_intent_allows_page_scoped_keyboard_gesture():
    intent = ActionIntent(
        goal="Dismiss the open overlay", gesture="key", value="Escape", parameters={"scope": "page"}
    )
    operation = intent.to_operation()
    assert operation.kind is OperationKind.KEY_PRESS
    assert operation.target is None


def test_action_intent_compiles_generic_select_gesture():
    intent = ActionIntent(
        goal="Choose the observed option",
        gesture="select",
        target=Target(name="Category", selector="#category"),
        value="Example",
    )
    assert intent.to_operation().kind is OperationKind.SELECT_OPTION


def test_action_intent_compiles_submit_without_domain_specific_operation():
    intent = ActionIntent(
        goal="Submit the completed form",
        gesture="submit",
        target=Target(name="Continue", role="button"),
        expected_state=[Postcondition(kind="visible", expected=True, target=Target(name="Result"))],
        side_effect_policy="authorized_mutation",
    )
    operation = intent.to_operation()
    assert operation.kind is OperationKind.SUBMIT
    assert operation.side_effect_policy == "authorized_mutation"
