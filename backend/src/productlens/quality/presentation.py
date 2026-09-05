"""Deterministic checks for the renderer contract before delivery."""

from __future__ import annotations

from typing import Any

from productlens.contracts.models import DemoTrace


def inspect_visual_state(state: dict[str, Any]) -> dict[str, Any]:
    """Reject a video whose explicit requested theme was not applied."""
    failures: list[str] = []
    if state.get("requested") == "light" and state.get("applied") is not True:
        failures.append("REQUESTED_VISUAL_STATE_NOT_APPLIED")
    return {"visual_state_score": 1.0 if not failures else 0.0, "hard_failures": failures}


def inspect_presentation(trace: DemoTrace, props: dict[str, Any]) -> dict[str, Any]:
    """Reject malformed editorial timelines without a paid visual model.

    This complements encoded-video checks: it validates the camera/cursor/caption
    data that was actually sent to Remotion, including the native-scale zoom cap.
    """
    failures: list[str] = []
    warnings: list[str] = []
    screen_frames = int(props.get("screenFrames") or 0)
    frame_rate = int(props.get("frameRate") or 30)
    beats = props.get("beats") if isinstance(props.get("beats"), list) else []
    captions = props.get("captions") if isinstance(props.get("captions"), list) else []
    cursor_paths = props.get("cursorPaths") if isinstance(props.get("cursorPaths"), list) else []
    event_viewports = props.get("eventViewports") if isinstance(props.get("eventViewports"), dict) else {}
    source_width = float(props.get("sourceWidth") or 1920)
    source_height = float(props.get("sourceHeight") or 1080)
    successful_count = sum(event.success for event in trace.events)
    if screen_frames < 72:
        failures.append("INVALID_PRESENTATION_DURATION")
    if successful_count and len(beats) < successful_count:
        failures.append("MISSING_PRESENTATION_BEATS")
    successful_ids = [event.id for event in trace.events if event.success]
    if any("eventId" in beat for beat in beats) and [str(beat.get("eventId", "")) for beat in beats] != successful_ids:
        failures.append("PRESENTATION_BEAT_TRACE_MISMATCH")
    playback_rate = float(props.get("playbackRate") or 1.0)
    if playback_rate < 0.9:
        failures.append("EXCESSIVE_EVIDENCE_SLOWDOWN")
    for beat in beats:
        start, end = int(beat.get("start", -1)), int(beat.get("end", -1))
        click_frame = int(beat.get("clickFrame", start))
        zoom = float(beat.get("zoom", 0))
        if start < 0 or end <= start or end > screen_frames:
            failures.append("BROKEN_PRESENTATION_BEAT_TIMING")
            break
        if click_frame < start or click_frame >= end:
            failures.append("CURSOR_CLICK_OUTSIDE_PRESENTATION_BEAT")
            break
        if zoom < 1.0 or zoom > 1.2:
            failures.append("EXCESSIVE_PRESENTATION_ZOOM")
            break
    beats_by_id = {str(beat.get("eventId", "")): beat for beat in beats}
    path_by_id = {str(path.get("event_id", "")): path for path in cursor_paths}
    for event in trace.events:
        if not event.success or event.target_rect is None:
            continue
        # Scrolling is represented by the page motion itself, not a pointer
        # click.  Likewise, an off-viewport navigation target cannot have a
        # truthful on-canvas cursor geometry; the director intentionally omits
        # those paths rather than drawing a jump outside the frame.
        viewport = event_viewports.get(event.id) if isinstance(event_viewports.get(event.id), dict) else {}
        viewport_width = float(viewport.get("width") or (event.viewport.width if event.viewport else source_width))
        viewport_height = float(viewport.get("height") or (event.viewport.height if event.viewport else source_height))
        target_in_view = (
            event.target_rect.x >= 0 and event.target_rect.y >= 0
            and event.target_rect.x + event.target_rect.width <= viewport_width
            and event.target_rect.y + event.target_rect.height <= viewport_height
        )
        if event.kind.value == "ScrollTo" or not target_in_view:
            continue
        path = path_by_id.get(event.id)
        if path is None:
            failures.append("MISSING_CURSOR_PATH")
            break
        destination = path.get("destination") if isinstance(path.get("destination"), dict) else {}
        expected_x = (event.target_rect.x + event.target_rect.width / 2) * source_width / max(1, viewport_width)
        expected_y = (event.target_rect.y + event.target_rect.height / 2) * source_height / max(1, viewport_height)
        if abs(float(destination.get("x", -1)) - expected_x) > 1 or abs(float(destination.get("y", -1)) - expected_y) > 1:
            failures.append("CURSOR_PATH_TARGET_MISMATCH")
            break
        source = path.get("source") if isinstance(path.get("source"), dict) else {}
        if min(float(source.get("x", -1)), float(source.get("y", -1)), float(destination.get("x", -1)), float(destination.get("y", -1))) < 0:
            failures.append("CURSOR_PATH_OUTSIDE_VIEWPORT")
            break
    previous_end = 0.0
    if frame_rate < 24:
        failures.append("INVALID_PRESENTATION_FRAME_RATE")
        frame_rate = 30
    screen_seconds = screen_frames / frame_rate if screen_frames else 0
    for caption in captions:
        start, end = float(caption.get("start", -1)), float(caption.get("end", -1))
        if start < previous_end or end <= start or end > screen_seconds + 0.01:
            failures.append("CAPTION_OUTSIDE_PRESENTATION_TIMELINE")
            break
        scene_id = str(caption.get("scene_id", ""))
        beat = beats_by_id.get(scene_id)
        if scene_id and beat is None:
            failures.append("CAPTION_WITHOUT_PRESENTATION_BEAT")
            break
        # A beat is the cursor/action interval.  A caption is bound to the
        # same event id but begins when that action's verified state becomes
        # readable, which can be after the cursor interval for a navigation or
        # asynchronous transition.  Requiring time-range containment here
        # reintroduces caption-ahead-of-video failures; the shared scene id and
        # monotonic rendered timeline are the applicable synchronization
        # contract.
        previous_end = end
    # Caption-led delivery is the narration product until TTS is configured.
    # A long walkthrough whose final caption ends near the opening is not a
    # coherent silent demo, even if each individual line is well formed.
    # Keep this scoped to multi-scene videos: a compact one-scene interaction
    # can legitimately finish its explanation before a short closing hold.
    if (
        not props.get("narration")
        and successful_count >= 3
        and captions
        and previous_end < screen_seconds * 0.8
    ):
        failures.append("CAPTION_COVERAGE_TOO_SHORT")
    if beats and all(float(beat.get("zoom", 1)) == 1 for beat in beats):
        warnings.append("PRESENTATION_USES_NATIVE_SCALE_ONLY")
    return {
        "presentation_score": 1.0 if not failures else 0.0,
        "hard_failures": failures,
        "warnings": warnings,
        "screen_frames": screen_frames,
        "beat_count": len(beats),
        "caption_count": len(captions),
        "caption_coverage_seconds": round(previous_end, 3),
    }


def attach_presentation_qa(video_report: dict[str, Any], presentation_report: dict[str, Any]) -> dict[str, Any]:
    """Make presentation-contract failures delivery-blocking visual failures."""
    failures = [*video_report.get("hard_failures", []), *presentation_report["hard_failures"]]
    return {
        **video_report,
        "visual_score": 1.0 if not failures else 0.0,
        "overall_score": 1.0 if not failures else 0.0,
        "hard_failures": failures,
        "warnings": [*video_report.get("warnings", []), *presentation_report["warnings"]],
        "presentation": presentation_report,
    }
