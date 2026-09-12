from datetime import UTC, datetime

from productlens.contracts.models import (
    DemoPlan,
    DemoTrace,
    InteractionEvent,
    OperationKind,
    Postcondition,
    SemanticOperation,
    Target,
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


def test_coverage_accepts_declared_sparse_page_contract_phases_without_exact_copy():
    operation = SemanticOperation(
        id="opening", kind=OperationKind.VERIFY_STATE, intent="Hold the workspace",
        story_phase="establish", page_url="https://demo.test/bookings",
        page_contract_phases=["establish", "explore", "explain", "verify"],
    )
    plan = DemoPlan.model_construct(
        objective="Demo", narrative_goal="Demo", audience="tester", target_duration_seconds=30,
        selected_workflow="sparse page", workflow_steps=[WorkflowStep(id="opening", intent="Hold", operation=operation)],
        expected_outcomes=["The booking workspace is held, explored, explained, and verified"],
        viewport_strategy="native", stop_conditions=["done"],
    )
    trace = DemoTrace(
        run_id="sparse-contract", objective="Demo", started_at=datetime.now(UTC),
        events=[InteractionEvent(operation_id="opening", kind=OperationKind.VERIFY_STATE,
                                 intent="Hold the workspace", page_url="https://demo.test/bookings",
                                 before={}, after={}, success=True, duration_ms=1)],
    )
    report = inspect_coverage(plan, trace)
    assert report["coverage_score"] == 1.0
    assert report["hard_failures"] == []


def test_coverage_proves_read_only_setup_fields_from_modal_and_reversible_actions():
    modal = SemanticOperation(
        id="modal", kind=OperationKind.OPEN_MODAL, intent="Open the appointment form",
        page_url="https://demo.test/bookings",
    )
    phone = SemanticOperation(
        id="phone", kind=OperationKind.FILL_PHONE, intent="Enter the phone number",
        target=Target(name="Phone", selector='[name="phone"]'), value="9814680905",
        page_url="https://demo.test/bookings",
    )
    name = SemanticOperation(
        id="name", kind=OperationKind.FILL_TEXT, intent="Enter the patient name",
        target=Target(name="Patient Name", selector='[name="name"]'), value="Maya Shah",
        page_url="https://demo.test/bookings",
    )
    steps = [WorkflowStep(id=o.id, intent=o.intent, operation=o) for o in (modal, phone, name)]
    plan = DemoPlan.model_construct(
        objective="Demo", narrative_goal="Demo", audience="tester", target_duration_seconds=30,
        selected_workflow="read-only setup", workflow_steps=steps,
        expected_outcomes=["Visible appointment setup fields are explained"],
        viewport_strategy="native", stop_conditions=["done"],
    )
    trace = DemoTrace(
        run_id="form-coverage", objective="Demo", started_at=datetime.now(UTC),
        events=[InteractionEvent(operation_id=o.id, kind=o.kind, intent=o.intent,
                                 before={}, after={}, success=True, duration_ms=1)
                for o in (modal, phone, name)],
    )
    report = inspect_coverage(plan, trace)
    assert report["coverage_score"] == 1.0
    assert report["hard_failures"] == []


def test_coverage_requires_a_visual_witness_for_a_visible_mutation_outcome():
    operation = SemanticOperation(
        id="submit", kind=OperationKind.SUBMIT, intent="Create record",
        postconditions=[Postcondition(kind="visible", expected=True, target=Target(name="Created record"))],
    )
    plan = DemoPlan.model_construct(
        objective="Demo", narrative_goal="Demo", audience="tester", target_duration_seconds=30,
        selected_workflow="create", workflow_steps=[WorkflowStep(id="submit", intent="Create", operation=operation)],
        expected_outcomes=[], viewport_strategy="native", stop_conditions=["done"],
    )
    trace = DemoTrace(
        run_id="missing-image", objective="Demo", started_at=datetime.now(UTC),
        events=[InteractionEvent(operation_id="submit", kind=OperationKind.SUBMIT, intent="Create record", before={}, after={}, success=True, duration_ms=1)],
    )
    assert "VISIBLE_MUTATION_OUTCOME_NOT_CAPTURED" in inspect_coverage(plan, trace)["hard_failures"]
    trace.events[0].screenshot_path = "execution/result.png"
    assert inspect_coverage(plan, trace)["hard_failures"] == []
