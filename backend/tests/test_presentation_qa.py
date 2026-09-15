from datetime import UTC, datetime

from productlens.contracts.models import DemoTrace, InteractionEvent, OperationKind, Rect, Target
from productlens.quality.presentation import (
    attach_presentation_qa,
    inspect_presentation,
    inspect_visual_state,
)


def _trace() -> DemoTrace:
    return DemoTrace(
        run_id="presentation",
        objective="Open dashboard",
        started_at=datetime.now(UTC),
        outcome_verified=True,
        events=[
            InteractionEvent(
                operation_id="one",
                kind=OperationKind.CLICK,
                intent="Open dashboard",
                before={},
                after={},
                success=True,
                duration_ms=1,
            )
        ],
    )


def test_presentation_qa_accepts_bounded_cursor_and_caption_timeline():
    report = inspect_presentation(
        _trace(),
        {
            "screenFrames": 120,
            "beats": [{"start": 0, "end": 120, "clickFrame": 18, "zoom": 1.08}],
            "captions": [{"start": 0, "end": 4, "text": "Open the dashboard."}],
        },
    )
    assert report["hard_failures"] == []
    assert (
        attach_presentation_qa({"hard_failures": [], "visual_score": 1}, report)["visual_score"]
        == 1
    )


def test_presentation_qa_rejects_excessive_zoom_and_bad_click_timing():
    report = inspect_presentation(
        _trace(),
        {
            "screenFrames": 120,
            "beats": [{"start": 10, "end": 30, "clickFrame": 35, "zoom": 1.25}],
            "captions": [],
        },
    )
    assert "CURSOR_CLICK_OUTSIDE_PRESENTATION_BEAT" in report["hard_failures"]


def test_presentation_qa_rejects_slideshow_slowdown():
    report = inspect_presentation(
        _trace(),
        {
            "screenFrames": 120,
            "playbackRate": 0.2,
            "beats": [{"start": 0, "end": 120, "clickFrame": 18, "zoom": 1}],
            "captions": [],
        },
    )
    assert "EXCESSIVE_EVIDENCE_SLOWDOWN" in report["hard_failures"]


def test_presentation_qa_uses_the_render_frame_rate_for_caption_alignment():
    trace = _trace()
    report = inspect_presentation(
        trace,
        {
            "screenFrames": 120,
            "frameRate": 30,
            "beats": [
                {"eventId": trace.events[0].id, "start": 0, "end": 120, "clickFrame": 18, "zoom": 1}
            ],
            "captions": [
                {
                    "scene_id": trace.events[0].id,
                    "start": 0,
                    "end": 4,
                    "text": "A complete four-second scene.",
                }
            ],
        },
    )
    assert report["hard_failures"] == []


def test_presentation_qa_rejects_caption_led_tours_that_go_silent_before_the_end():
    trace = DemoTrace(
        run_id="caption-coverage",
        objective="Walkthrough",
        started_at=datetime.now(UTC),
        outcome_verified=True,
        events=[
            InteractionEvent(
                operation_id=str(index),
                kind=OperationKind.CLICK,
                intent=f"Scene {index}",
                before={},
                after={},
                success=True,
                duration_ms=1,
            )
            for index in range(3)
        ],
    )
    report = inspect_presentation(
        trace,
        {
            "screenFrames": 900,
            "frameRate": 30,
            "beats": [
                {
                    "eventId": event.id,
                    "start": index * 300,
                    "end": (index + 1) * 300,
                    "clickFrame": index * 300 + 18,
                    "zoom": 1,
                }
                for index, event in enumerate(trace.events)
            ],
            "captions": [
                {
                    "scene_id": trace.events[0].id,
                    "start": 0,
                    "end": 5,
                    "text": "The first visible state is explained in full.",
                }
            ],
        },
    )

    assert "CAPTION_COVERAGE_TOO_SHORT" in report["hard_failures"]


def test_visual_state_qa_rejects_a_missed_requested_theme():
    assert inspect_visual_state({"requested": "light", "applied": False})["hard_failures"] == [
        "REQUESTED_VISUAL_STATE_NOT_APPLIED"
    ]


def test_presentation_qa_rejects_a_cursor_path_that_misses_recorded_target():
    trace = DemoTrace(
        run_id="presentation",
        objective="Open dashboard",
        started_at=datetime.now(UTC),
        outcome_verified=True,
        events=[
            InteractionEvent(
                operation_id="one",
                kind=OperationKind.CLICK,
                intent="Open dashboard",
                target=Target(name="Dashboard"),
                target_rect=Rect(x=100, y=50, width=40, height=20),
                before={},
                after={},
                success=True,
                duration_ms=1,
            )
        ],
    )
    report = inspect_presentation(
        trace,
        {
            "screenFrames": 120,
            "beats": [
                {"eventId": trace.events[0].id, "start": 0, "end": 120, "clickFrame": 18, "zoom": 1}
            ],
            "cursorPaths": [
                {
                    "event_id": trace.events[0].id,
                    "source": {"x": 0, "y": 0},
                    "destination": {"x": 1, "y": 1},
                }
            ],
            "captions": [],
        },
    )

    assert "CURSOR_PATH_TARGET_MISMATCH" in report["hard_failures"]


def test_presentation_qa_validates_drag_source_and_destination_geometry():
    trace = DemoTrace(
        run_id="drag-qa",
        objective="Demonstrate a canvas gesture",
        started_at=datetime.now(UTC),
        outcome_verified=True,
        events=[
            InteractionEvent(
                operation_id="drag",
                kind=OperationKind.DRAG,
                intent="Move the shape",
                target=Target(name="shape tool"),
                target_rect=Rect(x=80, y=40, width=40, height=40),
                before={},
                after={
                    "gesture": {"source": {"x": 100, "y": 60}, "destination": {"x": 620, "y": 360}}
                },
                success=True,
                duration_ms=700,
            )
        ],
    )
    event = trace.events[0]
    report = inspect_presentation(
        trace,
        {
            "screenFrames": 120,
            "frameRate": 30,
            "beats": [{"eventId": event.id, "start": 0, "end": 120, "clickFrame": 18, "zoom": 1}],
            "cursorPaths": [
                {
                    "event_id": event.id,
                    "source": {"x": 0, "y": 0},
                    "waypoints": [{"x": 100, "y": 60}],
                    "destination": {"x": 620, "y": 360},
                }
            ],
            "captions": [],
        },
    )
    assert report["hard_failures"] == []


def test_presentation_qa_validates_pointer_sequence_endpoint_not_surface_center():
    trace = DemoTrace(
        run_id="pointer-qa",
        objective="Draw on a canvas",
        started_at=datetime.now(UTC),
        outcome_verified=True,
        events=[
            InteractionEvent(
                operation_id="stroke",
                kind=OperationKind.POINTER_SEQUENCE,
                intent="Draw a stroke",
                target=Target(name="canvas", selector="svg"),
                target_rect=Rect(x=0, y=0, width=800, height=500),
                before={},
                after={"gesture": {"points": [{"x": 120, "y": 200}, {"x": 680, "y": 260}]}},
                success=True,
                duration_ms=700,
            )
        ],
    )
    event = trace.events[0]
    report = inspect_presentation(
        trace,
        {
            "screenFrames": 120,
            "frameRate": 30,
            "beats": [{"eventId": event.id, "start": 0, "end": 120, "clickFrame": 18, "zoom": 1}],
            "cursorPaths": [
                {
                    "event_id": event.id,
                    "source": {"x": 100, "y": 180},
                    "waypoints": [{"x": 400, "y": 230}],
                    "destination": {"x": 680, "y": 260},
                }
            ],
            "captions": [],
        },
    )
    assert report["hard_failures"] == []
