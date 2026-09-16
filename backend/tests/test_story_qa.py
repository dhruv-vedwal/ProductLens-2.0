from datetime import UTC, datetime

from app.contracts.models import DemoTrace, InteractionEvent, OperationKind
from app.quality.story import inspect_story


def test_story_qa_rejects_unverified_outcome():
    trace = DemoTrace(
        run_id="run",
        objective="Create lead",
        started_at=datetime.now(UTC),
        events=[
            InteractionEvent(
                operation_id="op",
                kind=OperationKind.CLICK,
                intent="Create lead",
                before={},
                after={},
                success=True,
                duration_ms=1,
            )
        ],
    )
    assert (
        "OBJECTIVE_OUTCOME_UNVERIFIED"
        in inspect_story(trace, objective="Create lead")["hard_failures"]
    )


def test_story_qa_rejects_caption_lines_that_are_not_trace_backed():
    trace = DemoTrace(
        run_id="run",
        objective="Create lead",
        started_at=datetime.now(UTC),
        outcome_verified=True,
        events=[
            InteractionEvent(
                id="event-1",
                operation_id="op",
                kind=OperationKind.CLICK,
                intent="Open leads",
                before={},
                after={},
                success=True,
                duration_ms=1,
            )
        ],
    )
    report = inspect_story(
        trace,
        objective="Create lead",
        script=[{"event_id": "different-event", "text": "Open leads"}],
    )
    assert "NARRATION_TRACE_MISMATCH" in report["hard_failures"]
    assert "RAW_ACTION_LABEL_CAPTION" in report["hard_failures"]


def test_story_qa_accepts_a_failed_pre_dispatch_target_recovered_by_replan():
    trace = DemoTrace(
        run_id="run",
        objective="Show the documentation",
        started_at=datetime.now(UTC),
        outcome_verified=True,
        events=[
            InteractionEvent(
                operation_id="stale-op",
                kind=OperationKind.CLICK,
                intent="Open stale link",
                success=False,
                duration_ms=1,
            ),
            InteractionEvent(
                operation_id="replacement-op",
                kind=OperationKind.NAVIGATE,
                intent="Open the observed replacement page",
                success=True,
                duration_ms=1,
            ),
        ],
        replan_decisions=[
            {
                "failed_operation_id": "stale-op",
                "dispatched": False,
                "replacement_steps": [{"operation": {"id": "replacement-op"}}],
            }
        ],
    )

    assert (
        "FAILED_TRACE_EVENT" not in inspect_story(trace, objective=trace.objective)["hard_failures"]
    )
