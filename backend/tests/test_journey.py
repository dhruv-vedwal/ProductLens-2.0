from datetime import UTC, datetime

from productlens.contracts.models import DemoTrace, InteractionEvent, OperationKind
from productlens.presentation.journey import build_journey, inspect_journey


def _event(operation_id: str, kind: OperationKind, page_url: str | None = None) -> InteractionEvent:
    return InteractionEvent(operation_id=operation_id, kind=kind, intent=operation_id, success=True, duration_ms=500, occurred_at=datetime.now(UTC), page_url=page_url)


def test_journey_requires_context_and_exploration_after_navigation():
    trace = DemoTrace(run_id="golden", objective="portfolio", started_at=datetime.now(UTC), events=[
        _event("opening", OperationKind.VERIFY_STATE),
        _event("timeline", OperationKind.OPEN_NAVIGATION_ITEM),
        _event("timeline-content", OperationKind.SCROLL_TO),
    ], outcome_verified=True)
    scenes = [{"event_id": event.id, "show_cursor": True, "caption_safe_zone": "bottom"} for event in trace.events]
    journey = build_journey(trace, scenes)
    assert [scene["story_phase"] for scene in journey] == ["context", "enter", "explore"]
    assert inspect_journey(journey)["hard_failures"] == []


def test_journey_counts_directed_enter_phase_when_source_stage_is_explain():
    """Planner metadata must not hide the directed page-establishment beat."""
    trace = DemoTrace(run_id="stage-precedence", objective="portfolio", started_at=datetime.now(UTC), events=[
        _event("opening", OperationKind.VERIFY_STATE, "https://example.test/"),
        _event("timeline", OperationKind.OPEN_NAVIGATION_ITEM, "https://example.test/timeline"),
        _event("timeline-content", OperationKind.SCROLL_TO, "https://example.test/timeline"),
    ], outcome_verified=True)
    scenes = [
        {"event_id": event.id, "page_stage": "explain", "dwell_seconds": 2}
        for event in trace.events
    ]
    journey = build_journey(trace, scenes)
    completion = journey[-1]["page_completion"]
    assert completion["established"] is True
    assert inspect_journey(journey)["hard_failures"] == []


def test_journey_rejects_route_sweep_without_page_exploration():
    trace = DemoTrace(run_id="bad", objective="portfolio", started_at=datetime.now(UTC), events=[
        _event("opening", OperationKind.VERIFY_STATE),
        _event("timeline", OperationKind.OPEN_NAVIGATION_ITEM),
        _event("notes", OperationKind.OPEN_NAVIGATION_ITEM),
    ], outcome_verified=True)
    scenes = [{"event_id": event.id, "show_cursor": True, "caption_safe_zone": "bottom"} for event in trace.events]
    assert "JOURNEY_HAS_NAVIGATION_WITHOUT_EXPLORATION" in inspect_journey(build_journey(trace, scenes))["hard_failures"]


def test_full_walkthrough_rejects_an_opening_page_that_is_only_glimpsed_before_navigation():
    trace = DemoTrace(run_id="opening-gap", objective="Complete walkthrough of the product", started_at=datetime.now(UTC), events=[
        _event("opening", OperationKind.VERIFY_STATE, "https://example.test/"),
        _event("features", OperationKind.OPEN_NAVIGATION_ITEM, "https://example.test/features"),
        _event("features-content", OperationKind.SCROLL_TO, "https://example.test/features"),
    ], outcome_verified=True)
    scenes = [{"event_id": event.id, "dwell_seconds": 2} for event in trace.events]

    report = inspect_journey(build_journey(trace, scenes))

    assert "JOURNEY_OPENING_PAGE_INCOMPLETE" in report["hard_failures"]


def test_journey_rejects_a_return_to_an_already_completed_page():
    trace = DemoTrace(run_id="bad-return", objective="walkthrough", started_at=datetime.now(UTC), events=[
        _event("opening", OperationKind.VERIFY_STATE, "https://example.test/"),
        _event("first-page", OperationKind.OPEN_NAVIGATION_ITEM, "https://example.test/first"),
        _event("first-content", OperationKind.SCROLL_TO, "https://example.test/first"),
        _event("second-page", OperationKind.OPEN_NAVIGATION_ITEM, "https://example.test/second"),
        _event("second-content", OperationKind.SCROLL_TO, "https://example.test/second"),
        _event("return", OperationKind.OPEN_NAVIGATION_ITEM, "https://example.test/first/"),
        _event("return-content", OperationKind.SCROLL_TO, "https://example.test/first/"),
    ], outcome_verified=True)
    scenes = [{"event_id": event.id} for event in trace.events]
    assert "JOURNEY_REVISITS_COMPLETED_PAGE" in inspect_journey(build_journey(trace, scenes))["hard_failures"]


def test_journey_rejects_a_return_to_the_completed_opening_page():
    trace = DemoTrace(run_id="bad-home-return", objective="walkthrough", started_at=datetime.now(UTC), events=[
        _event("opening", OperationKind.VERIFY_STATE, "https://example.test/"),
        _event("opening-content", OperationKind.SCROLL_TO, "https://example.test/"),
        _event("other", OperationKind.OPEN_NAVIGATION_ITEM, "https://example.test/other"),
        _event("other-content", OperationKind.SCROLL_TO, "https://example.test/other"),
        _event("return-home", OperationKind.OPEN_NAVIGATION_ITEM, "https://example.test/"),
        _event("home-content", OperationKind.SCROLL_TO, "https://example.test/"),
    ], outcome_verified=True)
    scenes = [{"event_id": event.id} for event in trace.events]
    assert "JOURNEY_REVISITS_COMPLETED_PAGE" in inspect_journey(build_journey(trace, scenes))["hard_failures"]


def test_journey_rejects_a_page_that_skips_a_required_content_group():
    trace = DemoTrace(run_id="missing-group", objective="portfolio", started_at=datetime.now(UTC), events=[
        _event("opening", OperationKind.VERIFY_STATE, "https://example.test/"),
        _event("timeline", OperationKind.OPEN_NAVIGATION_ITEM, "https://example.test/timeline"),
        _event("current-role", OperationKind.SCROLL_TO, "https://example.test/timeline"),
        _event("notes", OperationKind.OPEN_NAVIGATION_ITEM, "https://example.test/notes"),
        _event("notes-content", OperationKind.SCROLL_TO, "https://example.test/notes"),
    ], outcome_verified=True)
    scenes = [
        {"event_id": event.id, "dwell_seconds": 2, "required_content_groups": ["Current role", "Internship"],
         "covered_content_groups": (["Current role"] if event.operation_id == "current-role" else [])}
        for event in trace.events
    ]

    report = inspect_journey(build_journey(trace, scenes))

    assert "JOURNEY_REQUIRED_CONTENT_GROUP_NOT_SHOWN" in report["hard_failures"]


def test_journey_does_not_invent_scroll_motion_for_an_already_visible_landmark():
    event = _event("already-visible", OperationKind.SCROLL_TO, "https://example.test/")
    event.action_at = datetime.now(UTC)
    event.scroll_path = [{"x": 0, "y": 320}, {"x": 0, "y": 320}]
    trace = DemoTrace(run_id="visible", objective="portfolio", started_at=datetime.now(UTC), events=[event])

    scene = build_journey(trace, [{"event_id": event.id}])[0]

    assert scene["scroll"]["already_visible"] is True
    assert scene["scroll_continuity_required"] is False
    assert inspect_journey([scene])["hard_failures"] == []


def test_journey_requires_continuity_for_a_real_browser_scroll():
    event = _event("moved", OperationKind.SCROLL_TO, "https://example.test/")
    event.action_at = datetime.now(UTC)
    event.scroll_path = [{"x": 0, "y": 0}, {"x": 0, "y": 600}]
    trace = DemoTrace(run_id="moved", objective="portfolio", started_at=datetime.now(UTC), events=[event])

    scene = build_journey(trace, [{"event_id": event.id}])[0]

    assert scene["scroll"]["mode"] == "gradual"
    assert scene["scroll_continuity_required"] is True
