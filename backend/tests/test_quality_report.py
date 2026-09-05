from datetime import UTC, datetime

from productlens.contracts.models import DemoTrace, InteractionEvent, OperationKind
from productlens.quality.delivery import delivery_report
from productlens.quality.repair import classify_repair
from productlens.quality.synchronization import inspect_synchronization


def _trace() -> DemoTrace:
    event = InteractionEvent(
        operation_id="operation", kind=OperationKind.CLICK, intent="Open dashboard",
        before={}, after={}, success=True, duration_ms=1,
    )
    return DemoTrace(
        run_id="run", objective="Open dashboard", started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC), events=[event], outcome_verified=True,
    )


def test_synchronization_requires_trace_backed_caption_timing():
    trace = _trace()
    report = inspect_synchronization(
        trace, [{"event_id": trace.events[0].id, "text": "We open the dashboard."}],
        [{"scene_id": trace.events[0].id, "start": 0, "end": 3, "text": "We open the dashboard."}],
        narration_requested=False, narration_created=False,
    )
    assert report["hard_failures"] == []


def test_delivery_quality_report_contains_all_required_scores_and_repair_classification():
    report = delivery_report(
        artifacts={"trace": True, "final_video": True}, execution={"execution_score": 1},
        story={"story_score": 1}, video={"visual_score": 0, "hard_failures": ["MISSING_OR_EMPTY_RENDER"]},
    )
    assert report["overall_score"] == 0
    assert report["synchronization_score"] == 1
    decision = classify_repair(report["hard_failures"])
    assert decision.category == "presentation"
    assert decision.action == "re_render"
