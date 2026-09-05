from datetime import UTC, datetime

from productlens.contracts.models import DemoTrace, InteractionEvent, OperationKind
from productlens.quality.story import inspect_story


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
