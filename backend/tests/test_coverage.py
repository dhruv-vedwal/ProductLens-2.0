from datetime import UTC, datetime

from productlens.contracts.models import (
    DemoPlan,
    DemoTrace,
    InteractionEvent,
    OperationKind,
    SemanticOperation,
    WorkflowStep,
)
from productlens.quality.coverage import inspect_coverage


def test_coverage_requires_all_promised_outcomes():
    plan = DemoPlan.model_construct(
        objective="Demo", narrative_goal="Demo", audience="tester", target_duration_seconds=30,
        selected_workflow="test", workflow_steps=[], expected_outcomes=["Today", "Week 1"],
        viewport_strategy="native", stop_conditions=["done"],
    )
    trace = DemoTrace(
        run_id="coverage",
        objective="Demo",
        started_at=datetime.now(UTC),
        events=[InteractionEvent(operation_id="one", kind=OperationKind.CLICK, intent="Open Today", before={}, after={}, success=True, duration_ms=1)],
    )
    report = inspect_coverage(plan, trace)
    assert report["covered_outcomes"] == ["Today"]
    assert report["hard_failures"] == ["OBJECTIVE_COVERAGE_INCOMPLETE"]


def test_coverage_proves_page_complete_outcome_from_verified_phase_operations():
    phases = ["establish", "explore", "explain", "demonstrate", "verify"]
    steps = [
        WorkflowStep(
            id=f"step-{phase}", intent=f"{phase} Home", page_phase=phase,
            operation=SemanticOperation(
                id=f"operation-{phase}", kind=OperationKind.SCROLL_TO,
                intent=f"{phase} Home", story_phase=phase, page_url="https://demo.test/",
            ),
        )
        for phase in phases
    ]
    plan = DemoPlan.model_construct(
        objective="Demo", narrative_goal="Demo", audience="tester", target_duration_seconds=30,
        selected_workflow="page complete", workflow_steps=steps,
        expected_outcomes=["Home: visible content established, explored, explained, demonstrated, and verified"],
        viewport_strategy="native", stop_conditions=["done"],
    )
    trace = DemoTrace(
        run_id="coverage-contract", objective="Demo", started_at=datetime.now(UTC),
        events=[
            InteractionEvent(operation_id=step.operation.id, kind=OperationKind.SCROLL_TO, intent=step.intent,
                             before={}, after={}, page_url="https://demo.test/", success=True, duration_ms=1)
            for step in steps
        ],
    )
    report = inspect_coverage(plan, trace)
    assert report["coverage_score"] == 1.0
    assert report["hard_failures"] == []
