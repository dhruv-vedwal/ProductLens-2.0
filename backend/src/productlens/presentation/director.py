"""Deterministic camera and cursor direction based on verified trace geometry.

This module deliberately directs *recorded* interaction rather than inventing a
new interaction in the renderer.  The extra state on cursor paths is consumed
by presentation clients that support it and remains backwards compatible with
the original ``source``/``destination`` cursor contract.
"""

from __future__ import annotations

from productlens.contracts.models import CameraDecision, DemoTrace, OperationKind, PresentationPlan


def build_presentation_plan(
    trace: DemoTrace,
    *,
    viewport_width: int = 1440,
    viewport_height: int = 900,
    allow_camera_zoom: bool = False,
    scene_plan: list[dict] | None = None,
) -> PresentationPlan:
    camera: list[CameraDecision] = []
    cursor_events: list[str] = []
    cursor_paths: list[dict] = []
    previous_point = {"x": viewport_width / 2, "y": viewport_height / 2}
    previous_viewport = (viewport_width, viewport_height)
    scene_by_event = {
        str(scene.get("event_id")): scene
        for scene in (scene_plan or [])
        if isinstance(scene, dict) and scene.get("event_id")
    }
    for event in trace.events:
        if not event.success:
            continue
        gesture = event.after.get("gesture") if isinstance(event.after, dict) else None
        gesture_points = gesture.get("points") if isinstance(gesture, dict) else None
        if (
            event.kind is OperationKind.POINTER_SEQUENCE
            and isinstance(gesture_points, list)
            and len(gesture_points) >= 2
        ):
            event_width = event.viewport.width if event.viewport else viewport_width
            event_height = event.viewport.height if event.viewport else viewport_height
            points = [
                {"x": float(point.get("x", 0)), "y": float(point.get("y", 0))}
                for point in gesture_points
                if isinstance(point, dict)
            ]
            if len(points) >= 2:
                destination = points[-1]
                cursor_events.append(event.id)
                cursor_paths.append(
                    {
                        "event_id": event.id,
                        "source": previous_point,
                        "destination": destination,
                        "waypoints": points[1:-1],
                        "travel_seconds": round(
                            min(1.4, max(0.28, float(gesture.get("duration_ms", 450)) / 1000)), 2
                        ),
                        "hover_seconds": 0.12,
                        "settle_seconds": 0.08,
                        "easing": "linear-observed-path",
                        "cursor_style": "productlens-pointer-v1",
                        "click": bool(gesture.get("press") or gesture.get("release")),
                    }
                )
                previous_point = destination
            continue
        if event.target_rect is None:
            continue
        # Geometry is authoritative per event. Cloud and local captures may
        # use different viewport sizes, and comparing a 1920px trace against
        # the director's historical 1440px default incorrectly classified
        # valid targets as off-canvas and dropped their cursor paths.
        event_width = event.viewport.width if event.viewport else viewport_width
        event_height = event.viewport.height if event.viewport else viewport_height
        if (event_width, event_height) != previous_viewport:
            previous_point = {"x": event_width / 2, "y": event_height / 2}
            previous_viewport = (event_width, event_height)
        rect = event.target_rect
        small_target = rect.width < 180 or rect.height < 48
        off_center = abs((rect.x + rect.width / 2) - event_width / 2) > event_width * 0.28
        # The source product remains the presentation.  Camera crop is opt-in
        # because an arbitrary target zoom can cut product chrome, sidebars,
        # and visual effects from a real walkthrough.
        form_interaction = event.kind in {
            OperationKind.FILL_TEXT,
            OperationKind.FILL_EMAIL,
            OperationKind.FILL_PHONE,
            OperationKind.SELECT_OPTION,
            OperationKind.SELECT_DATE,
            OperationKind.SELECT_DATE_RANGE,
            OperationKind.CHOOSE_RADIO,
            OperationKind.CHECK,
            OperationKind.UNCHECK,
        }
        scene = scene_by_event.get(event.id)
        scene_camera = (
            scene.get("camera")
            if isinstance(scene, dict) and isinstance(scene.get("camera"), dict)
            else {}
        )
        requested_zoom = scene_camera.get("zoom")
        try:
            requested_zoom = float(requested_zoom) if requested_zoom is not None else None
        except (TypeError, ValueError):
            requested_zoom = None
        # Geometry captured from a responsive/other viewport must never drive
        # a camera move outside the recording's readable canvas.
        target_in_view = (
            rect.x >= 0
            and rect.y >= 0
            and rect.x + rect.width <= event_width
            and rect.y + rect.height <= event_height
        )
        # Non-form clicks receive focus only when the validated scene plan
        # explicitly requested it. A missing scene used to make every small
        # button eligible for a cinematic zoom, including empty sidebars.
        scene_allows_focus = scene_camera.get("mode") == "target-focus" or form_interaction
        # A focused form control needs more than the old 1.12 reframe to make
        # real typing readable at 1080p. The allowable zoom is nevertheless
        # derived from target geometry: controls near an edge retain more page
        # context, while a centrally visible field can receive a closer but
        # still reversible editorial view.
        center_x = (rect.x + rect.width / 2) / max(event_width, 1)
        center_y = (rect.y + rect.height / 2) / max(event_height, 1)
        edge_proximity = min(center_x, 1 - center_x, center_y, 1 - center_y)
        # Derive focus from the observed target instead of using one global
        # zoom number. Small inputs need a readable height at 1080p, while a
        # large content region should remain native. Edge targets receive a
        # lower bound because a close reframe would hide their surrounding
        # context. The renderer and QA layer share the 1.36 ceiling.
        readable_width = 220.0 if form_interaction else 180.0
        readable_height = 62.0 if form_interaction else 54.0
        geometry_zoom = max(
            readable_width / max(rect.width, 1.0),
            readable_height / max(rect.height, 1.0),
            1.0,
        )
        edge_cap = 1.18 if edge_proximity < 0.12 else 1.28 if edge_proximity < 0.2 else 1.36
        max_safe_zoom = min(1.36, edge_cap, geometry_zoom)
        if (
            allow_camera_zoom
            and scene_allows_focus
            and target_in_view
            and requested_zoom is not None
        ):
            zoom = min(max_safe_zoom, max(1.0, requested_zoom))
            reason = "scene-requested target emphasis constrained by target-to-frame safety bounds"
        elif allow_camera_zoom and form_interaction and scene_allows_focus and target_in_view:
            zoom, reason = (
                max_safe_zoom,
                "form control receives geometry-derived target-local focus while surrounding context remains visible",
            )
        elif (
            allow_camera_zoom
            and scene_camera.get("mode") == "target-focus"
            and target_in_view
            and (rect.width < 96 or rect.height < 30)
        ):
            zoom, reason = (
                max_safe_zoom,
                "small target receives geometry-derived cinematic emphasis",
            )
        elif (
            allow_camera_zoom
            and scene_camera.get("mode") == "target-focus"
            and target_in_view
            and small_target
        ):
            zoom, reason = (
                max_safe_zoom,
                "compact target receives geometry-derived cinematic emphasis",
            )
        else:
            zoom, reason = 1.0, "native browser scale; no scene-local focus justification"
        if off_center:
            reason += "; target is outside default focal area"
        camera.append(CameraDecision(event_id=event.id, focus=rect, zoom=zoom, reason=reason))
        # Scroll targets and stale navigation geometry can legitimately be
        # outside the current viewport.  A cursor drawn there would visibly
        # jump off-canvas and cannot represent the dispatched interaction.
        # Keep the verified camera/evidence event, but omit an impossible
        # cursor path; the browser trace remains the source of truth.
        if event.kind is OperationKind.SCROLL_TO or not target_in_view:
            continue
        cursor_events.append(event.id)
        destination = {"x": rect.x + rect.width / 2, "y": rect.y + rect.height / 2}
        if event.kind is OperationKind.DRAG and isinstance(gesture, dict):
            source = gesture.get("source")
            drag_destination = gesture.get("destination")
            if isinstance(source, dict) and isinstance(drag_destination, dict):
                source_point = {
                    "x": float(source.get("x", destination["x"])),
                    "y": float(source.get("y", destination["y"])),
                }
                destination = {
                    "x": float(drag_destination.get("x", destination["x"])),
                    "y": float(drag_destination.get("y", destination["y"])),
                }
                cursor_paths.append(
                    {
                        "event_id": event.id,
                        "source": previous_point,
                        "destination": destination,
                        "waypoints": [source_point],
                        "travel_seconds": round(
                            min(1.5, max(0.3, float(gesture.get("duration_ms", 700)) / 1000)), 2
                        ),
                        "hover_seconds": 0.2,
                        "settle_seconds": 0.12,
                        "easing": "out-cubic-observed-drag",
                        "cursor_style": "productlens-pointer-v1",
                        "click": False,
                        "drag": True,
                    }
                )
                previous_point = destination
                continue
        distance = (
            (destination["x"] - previous_point["x"]) ** 2
            + (destination["y"] - previous_point["y"]) ** 2
        ) ** 0.5
        # A slight perpendicular waypoint prevents cursor motion from looking
        # like a robotic straight-line teleport.  It is bounded to the browser
        # viewport and is only decorative: the final point remains the exact
        # geometry captured at action dispatch.
        midpoint = {
            "x": (previous_point["x"] + destination["x"]) / 2,
            "y": (previous_point["y"] + destination["y"]) / 2,
        }
        dx, dy = destination["x"] - previous_point["x"], destination["y"] - previous_point["y"]
        length = max(distance, 1.0)
        bend = min(42.0, max(10.0, distance * 0.08))
        waypoint = {
            "x": round(min(event_width, max(0.0, midpoint["x"] - dy / length * bend)), 2),
            "y": round(min(event_height, max(0.0, midpoint["y"] + dx / length * bend)), 2),
        }
        cursor_paths.append(
            {
                "event_id": event.id,
                "source": previous_point,
                "destination": destination,
                "travel_seconds": round(min(0.8, max(0.22, distance / 1300)), 2),
                "hover_seconds": 0.25,
                "settle_seconds": 0.18,
                "waypoints": [waypoint],
                "easing": "out-cubic",
                "cursor_style": "productlens-pointer-v1",
                "click": event.kind.value
                in {
                    "Click",
                    "OpenNavigationItem",
                    "OpenModal",
                    "CloseModal",
                    "Submit",
                    "ApplyFilter",
                    "Check",
                    "Uncheck",
                    "ChooseRadio",
                    "SelectOption",
                },
            }
        )
        previous_point = destination
    return PresentationPlan(
        trace_run_id=trace.run_id,
        camera=camera,
        cursor_event_ids=cursor_events,
        cursor_paths=cursor_paths,
    )
