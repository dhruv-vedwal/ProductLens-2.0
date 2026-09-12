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


def test_synchronization_allows_an_ordered_editorial_subset_but_rejects_unreadable_caption_dwell():
    trace = _trace()
    extra = InteractionEvent(
        id="later-event", operation_id="later", kind=OperationKind.CLICK, intent="Open details",
        before={}, after={}, success=True, duration_ms=1,
    )
    trace.events.append(extra)
    report = inspect_synchronization(
        trace,
        [{"event_id": trace.events[0].id, "text": "This dashboard gives the team a clear starting point for the workflow."}],
        [{"scene_id": trace.events[0].id, "start": 0, "end": 1, "text": "This dashboard gives the team a clear starting point for the workflow."}],
        narration_requested=False, narration_created=False,
    )
    assert "SCRIPT_TRACE_MISMATCH" not in report["hard_failures"]
    assert "CAPTION_READING_DWELL_TOO_SHORT" in report["hard_failures"]


def test_caption_only_delivery_rejects_a_long_unexplained_silence_gap():
    trace = _trace()
    report = inspect_synchronization(
        trace,
        [{"event_id": trace.events[0].id, "text": "This dashboard gives the team a clear starting point for the workflow."}],
        [{"scene_id": trace.events[0].id, "start": 7.0, "end": 11.0, "text": "This dashboard gives the team a clear starting point for the workflow."}],
        narration_requested=False, narration_created=False,
    )
    assert "CAPTION_SILENCE_GAP_TOO_LONG" in report["hard_failures"]


def test_caption_only_delivery_allows_only_an_explicit_secure_transition_gap():
    trace = _trace()
    report = inspect_synchronization(
        trace,
        [{"event_id": trace.events[0].id, "text": "The workspace is now ready for the walkthrough."}],
        [{"scene_id": trace.events[0].id, "start": 9.0, "end": 12.0, "text": "The workspace is now ready for the walkthrough."}],
        narration_requested=False, narration_created=False,
        explained_intervals=[(0.0, 7.0)],
    )
    assert "CAPTION_SILENCE_GAP_TOO_LONG" not in report["hard_failures"]


def test_delivery_quality_report_contains_all_required_scores_and_repair_classification():
    report = delivery_report(
        artifacts={"trace": True, "final_video": True}, execution={"execution_score": 1},
        story={"story_score": 1}, video={"visual_score": 0, "hard_failures": ["MISSING_OR_EMPTY_RENDER"]},
    )
    assert report["overall_score"] == 0
    assert report["synchronization_score"] == 1
    assert report["owner_by_failure"]["MISSING_OR_EMPTY_RENDER"] == "video"
    assert report["layer_evidence"]["video"] == ["MISSING_OR_EMPTY_RENDER"]
    decision = classify_repair(report["hard_failures"])
    assert decision.category == "presentation"
    assert decision.action == "re_render"
