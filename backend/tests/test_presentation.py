from datetime import UTC, datetime

from productlens.contracts.models import DemoTrace, InteractionEvent, OperationKind, Rect, Target
from productlens.presentation.director import build_presentation_plan
from productlens.presentation.scenes import build_scene_plan


def test_small_target_keeps_native_scale_by_default():
    trace = DemoTrace(
        run_id="run",
        objective="Open profile",
        started_at=datetime.now(UTC),
        events=[
            InteractionEvent(
                operation_id="op",
                kind=OperationKind.CLICK,
                intent="Open profile",
                target=Target(name="Profile"),
                target_rect=Rect(x=1330, y=12, width=24, height=24),
                before={},
                after={},
                success=True,
                duration_ms=10,
            )
        ],
    )
    plan = build_presentation_plan(trace)
    assert plan.camera[0].zoom == 1.0
    assert "native browser scale" in plan.camera[0].reason
    assert plan.cursor_event_ids
    assert plan.cursor_paths[0]["destination"] == {"x": 1342.0, "y": 24.0}
    assert plan.cursor_paths[0]["source"] == {"x": 720.0, "y": 450.0}
    assert 0.22 <= plan.cursor_paths[0]["travel_seconds"] <= 0.8


def test_form_interaction_gets_bounded_focus_zoom_when_enabled():
    trace = DemoTrace(
        run_id="form", objective="Fill form", started_at=datetime.now(UTC),
        events=[InteractionEvent(
            operation_id="fill", kind=OperationKind.FILL_TEXT, intent="Enter name",
            target=Target(name="Name"), target_rect=Rect(x=400, y=350, width=360, height=48),
            before={}, after={}, success=True, duration_ms=10,
        )],
    )
    decision = build_presentation_plan(trace, allow_camera_zoom=True).camera[0]
    assert decision.zoom == 1.14


def test_scene_directed_form_focus_reaches_the_renderer_camera():
    trace = DemoTrace(
        run_id="form-directed", objective="Fill form", started_at=datetime.now(UTC),
        events=[InteractionEvent(
            operation_id="fill", kind=OperationKind.FILL_TEXT, intent="Enter name",
            target=Target(name="Name"), target_rect=Rect(x=400, y=350, width=360, height=48),
            before={}, after={}, success=True, duration_ms=10,
        )],
    )
    decision = build_presentation_plan(
        trace, allow_camera_zoom=True, scene_plan=build_scene_plan(trace)
    ).camera[0]
    assert decision.zoom == 1.14


def test_small_non_form_target_is_not_zoomed_without_explicit_scene_direction():
    trace = DemoTrace(
        run_id="small", objective="Open help", started_at=datetime.now(UTC),
        events=[InteractionEvent(
            operation_id="help", kind=OperationKind.CLICK, intent="Open help",
            target=Target(name="Help"), target_rect=Rect(x=1300, y=80, width=30, height=28),
            before={}, after={}, success=True, duration_ms=10,
        )],
    )
    assert build_presentation_plan(trace, allow_camera_zoom=True).camera[0].zoom == 1.0


def test_scene_requested_zoom_is_honoured_but_capped_to_safe_envelope():
    trace = DemoTrace(
        run_id="scene-zoom", objective="Focus control", started_at=datetime.now(UTC),
        events=[InteractionEvent(
            operation_id="click", kind=OperationKind.CLICK, intent="Open details",
            target=Target(name="Details"), target_rect=Rect(x=500, y=300, width=120, height=40),
            before={}, after={}, success=True, duration_ms=10,
        )],
    )
    scene = [{"event_id": trace.events[0].id, "camera": {"mode": "target-focus", "zoom": 1.35}}]
    decision = build_presentation_plan(trace, allow_camera_zoom=True, scene_plan=scene).camera[0]
    assert decision.zoom == 1.18
    assert "safety bounds" in decision.reason


def test_edge_form_control_keeps_a_smaller_safe_reframe():
    trace = DemoTrace(
        run_id="edge-form", objective="Fill form", started_at=datetime.now(UTC),
        events=[InteractionEvent(
            operation_id="fill", kind=OperationKind.FILL_TEXT, intent="Enter email",
            target=Target(name="Email"), target_rect=Rect(x=8, y=80, width=320, height=44),
            before={}, after={}, success=True, duration_ms=10,
        )],
    )
    decision = build_presentation_plan(trace, allow_camera_zoom=True).camera[0]
    assert decision.zoom == 1.14


def test_cursor_direction_has_a_natural_bounded_waypoint_but_keeps_exact_target():
    trace = DemoTrace(
        run_id="cursor", objective="Open settings", started_at=datetime.now(UTC),
        events=[InteractionEvent(
            operation_id="settings", kind=OperationKind.CLICK, intent="Open settings",
            target=Target(name="Settings"), target_rect=Rect(x=1000, y=200, width=80, height=40),
            before={}, after={}, success=True, duration_ms=10,
        )],
    )
    path = build_presentation_plan(trace).cursor_paths[0]
    assert path["destination"] == {"x": 1040.0, "y": 220.0}
    assert path["waypoints"]
    assert path["cursor_style"] == "productlens-pointer-v1"
    assert 0 <= path["waypoints"][0]["x"] <= 1440
    assert 0 <= path["waypoints"][0]["y"] <= 900
