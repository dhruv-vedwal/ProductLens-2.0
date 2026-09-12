from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from productlens.contracts.models import (
    AudienceProfile,
    DemoPlan,
    DemoTrace,
    InteractionEvent,
    NarrationScript,
    OperationKind,
    Target,
    WorkflowState,
)


def test_audience_profile_is_bounded_and_serializable():
    profile = AudienceProfile(
        type="recruiter", vocabulary="executive", depth="overview",
        priorities=["career progression"], narration_style="concise",
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
        DemoPlan(**{**plan.model_dump(), "minimum_duration_seconds": 45, "maximum_duration_seconds": 90})


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
    script = NarrationScript(segments=[{
        "scene_id": "scene-1", "event_id": "event-1",
        "text": "This opening view establishes the product context for the viewer.",
        "evidence": ["page:https://example.test/"],
        "start_seconds": 0, "end_seconds": 3,
    }])
    assert script.schema_version == 1
    assert script.timing_owner == "scene"
    with pytest.raises(ValidationError):
        NarrationScript(segments=[{
            "scene_id": "scene-1", "event_id": "event-1",
            "text": "This opening view establishes the product context for the viewer.",
            "start_seconds": 4, "end_seconds": 2,
        }])
