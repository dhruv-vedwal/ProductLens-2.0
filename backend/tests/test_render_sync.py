import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from productlens.artifacts.store import RunArtifacts
from productlens.contracts.models import (
    DemoTrace,
    EditorialBrief,
    EditorialScene,
    EditorialStoryboard,
    InteractionEvent,
    OperationKind,
    PresentationPlan,
    Rect,
    Target,
    Viewport,
)
from productlens.video.render import (
    CaptureDurationError,
    NarrationTimingError,
    RecordingProvenanceError,
    _build_editorial_source,
    _completed_segment_after_timeout,
    _editorial_cut_windows,
    _evidence_timed_captions,
    _frame_rate,
    _outro_copy,
    _prepare_remotion_source,
    _presentation_secret_redactions,
    _validate_recording_provenance,
    _promote_render,
    _recording_space_cursor_paths,
    _remap_trace_for_cuts,
    _render_concurrency,
    _render_timeout_seconds,
    render_remotion,
)


def test_frame_rate_parser_preserves_measured_source_cadence():
    assert _frame_rate("60000/1001") > 59
    assert _frame_rate("30000/1001") < 30
    assert _frame_rate("broken") == 0


def test_prepared_h264_editorial_source_is_copied_without_a_second_encode(
    monkeypatch, tmp_path: Path
):
    raw = tmp_path / "editorial-source.mp4"
    raw.write_bytes(b"already-encoded-evidence")
    public = tmp_path / "public"

    class Probe:
        stdout = json.dumps({"streams": [{"codec_name": "h264", "pix_fmt": "yuv420p"}]})

    monkeypatch.setattr("productlens.video.render.subprocess.run", lambda *args, **kwargs: Probe())
    asset = _prepare_remotion_source(raw, public, "run")
    assert asset == "run.mp4"
    assert (public / asset).read_bytes() == raw.read_bytes()


def test_render_promotion_falls_back_when_windows_rename_is_locked(monkeypatch, tmp_path: Path):
    candidate = tmp_path / "candidate.mp4"
    output = tmp_path / "final" / "demo.mp4"
    candidate.write_bytes(b"complete-render")
    output.parent.mkdir()
    output.write_bytes(b"previous-render")

    def locked_replace(self, target):
        raise PermissionError("simulated media-player lock")

    monkeypatch.setattr(Path, "replace", locked_replace)
    _promote_render(candidate, output)

    assert output.read_bytes() == b"complete-render"
    assert not candidate.exists()


def test_existing_content_addressed_editorial_source_is_reused(monkeypatch, tmp_path: Path):
    raw = tmp_path / "raw.webm"
    raw.write_bytes(b"immutable-recording")
    windows = [(0.0, 4.0)]

    def initial_encode(args, **kwargs):
        Path(args[-1]).write_bytes(b"x" * 10_001)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("productlens.video.render.subprocess.run", initial_encode)
    first = _build_editorial_source(raw, render_dir=tmp_path / "render", windows=windows)
    monkeypatch.setattr(
        "productlens.video.render.subprocess.run",
        lambda *args, **kwargs: pytest.fail("cached source must not invoke ffmpeg"),
    )
    assert _build_editorial_source(raw, render_dir=tmp_path / "render", windows=windows) == first


def test_outro_copy_uses_the_approved_product_story_not_a_static_label():
    board = EditorialStoryboard(
        brief=EditorialBrief(
            title="Service Console walkthrough",
            product_purpose="Service health and incident coordination",
            opening_message="Welcome to the Service Console.",
        ),
        scenes=[
            EditorialScene(
                id="result",
                operation_id="result",
                title="Resolved incident",
                purpose="Verify the outcome",
                narration="The resolved incident leaves a clear handoff for the next responder.",
                evidence=["page:https://example.test/incidents"],
                interaction="observe",
                required_dwell_seconds=3,
                completion_criteria=["visible"],
            )
        ],
        minimum_duration_seconds=60,
    )
    title, subtitle = _outro_copy(
        DemoTrace(
            run_id="close", objective="Show incident coordination", started_at=datetime.now(UTC)
        ),
        board,
    )
    assert title == "That concludes the Service Console walkthrough."
    assert "clear handoff" in subtitle


def test_render_concurrency_is_conservative_and_configurable(monkeypatch):
    monkeypatch.delenv("PRODUCTLENS_REMOTION_CONCURRENCY", raising=False)
    assert _render_concurrency() == 2
    monkeypatch.setenv("PRODUCTLENS_REMOTION_CONCURRENCY", "12")
    assert _render_concurrency() == 4
    monkeypatch.setenv("PRODUCTLENS_REMOTION_CONCURRENCY", "invalid")
    assert _render_concurrency() == 2


def test_presentation_masks_generic_credential_fields_until_navigation():
    recorded_at = datetime.now(UTC)
    email = InteractionEvent(
        operation_id="email",
        kind=OperationKind.FILL_EMAIL,
        intent="Enter account email",
        target=Target(name="Email address"),
        target_rect=Rect(x=40, y=60, width=360, height=48),
        page_url="https://example.test/sign-in",
        occurred_at=recorded_at + timedelta(seconds=2),
        before={},
        after={},
        success=True,
        duration_ms=1,
    )
    password = InteractionEvent(
        operation_id="password",
        kind=OperationKind.FILL_TEXT,
        intent="Enter password",
        target=Target(name="Password"),
        target_rect=Rect(x=40, y=120, width=360, height=48),
        page_url="https://example.test/sign-in",
        occurred_at=recorded_at + timedelta(seconds=3),
        before={},
        after={},
        success=True,
        duration_ms=1,
    )
    submit = InteractionEvent(
        operation_id="submit",
        kind=OperationKind.SUBMIT,
        intent="Sign in",
        page_url="https://example.test/home",
        occurred_at=recorded_at + timedelta(seconds=5),
        before={},
        after={},
        success=True,
        duration_ms=1,
    )
    trace = DemoTrace(
        run_id="redact", objective="Demo", started_at=recorded_at, events=[email, password, submit]
    )
    beats = [
        {"eventId": email.id, "start": 10, "end": 30, "x": 60, "y": 90, "width": 540, "height": 72},
        {"eventId": password.id, "start": 30, "end": 60},
        {"eventId": submit.id, "start": 60, "end": 90},
    ]
    masks = _presentation_secret_redactions(trace, beats, frame_rate=30)
    assert [(item["x"], item["end"]) for item in masks] == [(60, 60), (40, 60)]
    assert masks[0]["start"] == 0
    assert all(item["label"] == "Sensitive value redacted" for item in masks)


def test_presentation_uses_a_full_frame_secure_transition_when_legacy_geometry_is_missing():
    event = InteractionEvent(
        operation_id="token",
        kind=OperationKind.FILL_TEXT,
        intent="Enter access token",
        target=Target(name="Access token"),
        before={},
        after={},
        success=True,
        duration_ms=1,
    )
    trace = DemoTrace(
        run_id="unsafe", objective="Demo", started_at=datetime.now(UTC), events=[event]
    )
    masks = _presentation_secret_redactions(
        trace, [{"eventId": event.id, "start": 0, "end": 10}], frame_rate=30
    )
    assert masks == [
        {
            "eventId": event.id,
            "start": 0,
            "end": 10,
            "mode": "secure-full-frame",
            "label": "Signing in securely",
        }
    ]


def test_missing_credential_geometry_protects_the_pre_action_authentication_prelude():
    recorded_at = datetime.now(UTC)
    event = InteractionEvent(
        operation_id="email",
        kind=OperationKind.FILL_EMAIL,
        intent="Enter account email",
        target=Target(name="Email address"),
        before={},
        after={},
        success=True,
        duration_ms=1,
        occurred_at=recorded_at + timedelta(seconds=3),
    )
    trace = DemoTrace(
        run_id="unsafe-prelude", objective="Demo", started_at=recorded_at, events=[event]
    )
    masks = _presentation_secret_redactions(
        trace, [{"eventId": event.id, "start": 90, "end": 120}], frame_rate=30
    )
    assert masks[0]["start"] == 0
    assert masks[0]["mode"] == "secure-full-frame"


def test_editorial_cuts_preserve_native_action_and_reveal_edges():
    recorded_at = datetime.now(UTC)
    event = InteractionEvent(
        operation_id="slow-navigation",
        kind=OperationKind.OPEN_NAVIGATION_ITEM,
        intent="Open the timeline",
        action_at=recorded_at + timedelta(seconds=12),
        occurred_at=recorded_at + timedelta(seconds=32),
        before={},
        after={},
        success=True,
        duration_ms=20_000,
    )
    trace = DemoTrace(
        run_id="native-cuts",
        objective="Demo",
        started_at=recorded_at,
        recording_started_at=recorded_at,
        events=[event],
    )

    windows = _editorial_cut_windows(trace, source_seconds=40)

    # This is a purposeful cut, not a speed adjustment: both causal moments
    # and their reading holds remain while the remote idle gap is omitted.
    assert windows[0] == pytest.approx((6.55, 13.9))
    assert windows[1] == pytest.approx((30.1, 34.9))
    assert sum(end - start for start, end in windows) < 40

    remapped = _remap_trace_for_cuts(trace, windows)
    remapped_event = remapped.events[0]
    assert remapped_event.action_at is not None
    assert remapped_event.action_at < remapped_event.occurred_at
    assert (remapped_event.action_at - recorded_at).total_seconds() == pytest.approx(5.45)
    assert (remapped_event.occurred_at - recorded_at).total_seconds() == pytest.approx(9.25)


def test_editorial_cuts_keep_long_typing_gesture_contiguous():
    """Typing must remain visible; a delayed DOM witness cannot micro-cut it."""
    recorded_at = datetime.now(UTC)
    event = InteractionEvent(
        operation_id="fill",
        kind=OperationKind.FILL_TEXT,
        intent="Type the workflow name",
        target=Target(name="Workflow name"),
        action_at=recorded_at + timedelta(seconds=8),
        occurred_at=recorded_at + timedelta(seconds=18),
        before={},
        after={"value": "Create chat"},
        success=True,
        duration_ms=10_000,
    )
    trace = DemoTrace(
        run_id="typing-continuity",
        objective="Demo",
        started_at=recorded_at,
        recording_started_at=recorded_at,
        events=[event],
    )
    windows = _editorial_cut_windows(trace, source_seconds=30)
    # One contiguous retained interval contains the complete 10s type action
    # and its readable result (rather than action/reveal micro-clips).
    assert len(windows) == 1
    assert windows[0][0] <= 7.4
    assert windows[0][1] >= 20.0


def test_recording_provenance_rejects_metadata_from_another_run(tmp_path):
    artifacts = RunArtifacts(tmp_path, "current-run")
    raw = artifacts.execution / "browser-recording.mp4"
    raw.write_bytes(b"evidence")
    artifacts.write_json(
        "execution/browserbase-recording.json",
        {
            "provider": "browserbase",
            "native_recording": "completed",
            "run_id": "other-run",
            "artifact": str(raw),
        },
    )
    trace = DemoTrace(
        run_id="current-run", objective="Demo", started_at=datetime.now(UTC), events=[]
    )
    with pytest.raises(RecordingProvenanceError, match="belongs to run"):
        _validate_recording_provenance(trace, artifacts, raw)


def test_recording_provenance_allows_local_capture_without_provider_metadata(tmp_path):
    artifacts = RunArtifacts(tmp_path, "local-run")
    raw = artifacts.execution / "browser-recording.mp4"
    raw.write_bytes(b"evidence")
    trace = DemoTrace(
        run_id="local-run", objective="Demo", started_at=datetime.now(UTC), events=[]
    )
    _validate_recording_provenance(trace, artifacts, raw)


def test_editorial_cuts_never_remove_the_middle_of_a_directed_scroll():
    recorded_at = datetime.now(UTC)
    event = InteractionEvent(
        operation_id="projects",
        kind=OperationKind.SCROLL_TO,
        intent="Read featured projects",
        action_at=recorded_at + timedelta(seconds=10),
        occurred_at=recorded_at + timedelta(seconds=30),
        before={},
        after={},
        success=True,
        duration_ms=20_000,
    )
    trace = DemoTrace(
        run_id="scroll-continuity",
        objective="Demo",
        started_at=recorded_at,
        recording_started_at=recorded_at,
        events=[event],
    )

    windows = _editorial_cut_windows(trace, source_seconds=40)

    # Remote latency is cut around navigation/idle states only. A scroll is
    # visible evidence in its own right, so its motion is retained at speed.
    assert len(windows) == 1
    assert windows[0] == pytest.approx((4.55, 32.9))


def test_editorial_cuts_preserve_native_reading_dwell_only_for_selected_caption_scene():
    recorded_at = datetime.now(UTC)
    first = InteractionEvent(
        operation_id="selected",
        kind=OperationKind.SCROLL_TO,
        intent="Explain selected evidence",
        action_at=recorded_at + timedelta(seconds=4),
        occurred_at=recorded_at + timedelta(seconds=5),
        before={},
        after={"scroll_motion": {"duration_ms": 900}},
        success=True,
        duration_ms=1,
    )
    second = InteractionEvent(
        operation_id="transition",
        kind=OperationKind.SCROLL_TO,
        intent="Move through supporting context",
        action_at=recorded_at + timedelta(seconds=15),
        occurred_at=recorded_at + timedelta(seconds=16),
        before={},
        after={"scroll_motion": {"duration_ms": 900}},
        success=True,
        duration_ms=1,
    )
    trace = DemoTrace(
        run_id="dwell",
        objective="Demo",
        started_at=recorded_at,
        recording_started_at=recorded_at,
        events=[first, second],
    )

    windows = _editorial_cut_windows(
        trace,
        25,
        reading_holds_seconds={first.id: 6.0},
    )

    assert windows[0][1] >= 10.89
    assert windows[1][1] < 18


def test_cursor_geometry_is_normalized_from_css_viewport_to_recording_pixels():
    event = InteractionEvent(
        operation_id="target",
        kind=OperationKind.CLICK,
        intent="Open detail",
        target=Target(name="Detail"),
        target_rect=Rect(x=720, y=450, width=120, height=60),
        viewport=Viewport(width=1440, height=900),
        before={},
        after={},
        success=True,
        duration_ms=1,
    )
    trace = DemoTrace(
        run_id="geometry", objective="Demo", started_at=datetime.now(UTC), events=[event]
    )
    plan = PresentationPlan(
        trace_run_id="geometry",
        camera=[],
        cursor_event_ids=[event.id],
        cursor_paths=[
            {
                "event_id": event.id,
                "source": {"x": 0, "y": 0},
                "destination": {"x": 780, "y": 480},
                "waypoints": [],
            }
        ],
    )

    path = _recording_space_cursor_paths(plan, trace, source_width=1920, source_height=1080)[0]

    assert path["destination"] == {"x": 1040.0, "y": 576.0}


def test_render_timeout_is_bounded_and_configurable(monkeypatch):
    monkeypatch.delenv("PRODUCTLENS_REMOTION_TIMEOUT_SECONDS", raising=False)
    assert _render_timeout_seconds() == 1800
    monkeypatch.setenv("PRODUCTLENS_REMOTION_TIMEOUT_SECONDS", "10")
    assert _render_timeout_seconds() == 120
    monkeypatch.setenv("PRODUCTLENS_REMOTION_TIMEOUT_SECONDS", "invalid")
    assert _render_timeout_seconds() == 1800


def test_timeout_reuses_a_complete_muxed_segment(tmp_path: Path):
    output = tmp_path / "segment.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x180:r=30",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    command = [
        "remotion",
        "render",
        "src/index.ts",
        "ProductLensDemo",
        str(output),
        "--frames",
        "0-29",
    ]
    assert _completed_segment_after_timeout(command)


def test_evidence_timed_captions_allocate_non_overlapping_slots_for_same_time_events():
    recorded_at = datetime.now(UTC)
    events = [
        InteractionEvent(
            operation_id=f"step-{index}",
            kind=OperationKind.SCROLL_TO,
            intent="Inspect section",
            action_at=recorded_at + timedelta(seconds=9),
            occurred_at=recorded_at + timedelta(seconds=10),
            before={},
            after={},
            success=True,
            duration_ms=1,
        )
        for index in range(3)
    ]
    trace = DemoTrace(
        run_id="same-time",
        objective="Demo",
        started_at=recorded_at,
        recording_started_at=recorded_at,
        events=events,
    )
    captions = [
        {"scene_id": event.id, "text": f"Observed item {index}"}
        for index, event in enumerate(events)
    ]
    timed = _evidence_timed_captions(trace, captions, screen_seconds=12)
    assert timed is not None
    assert all(timed[index]["end"] <= timed[index + 1]["start"] for index in range(len(timed) - 1))
    assert timed[-1]["end"] <= 12


def test_evidence_timed_captions_handoff_before_the_next_action_dispatch():
    recorded_at = datetime.now(UTC)
    first = InteractionEvent(
        operation_id="first",
        kind=OperationKind.SCROLL_TO,
        intent="Inspect first",
        action_at=recorded_at + timedelta(seconds=1),
        occurred_at=recorded_at + timedelta(seconds=3),
        before={},
        after={},
        success=True,
        duration_ms=1,
    )
    second = InteractionEvent(
        operation_id="second",
        kind=OperationKind.OPEN_NAVIGATION_ITEM,
        intent="Open next",
        action_at=recorded_at + timedelta(seconds=5),
        occurred_at=recorded_at + timedelta(seconds=10),
        before={},
        after={},
        success=True,
        duration_ms=1,
    )
    trace = DemoTrace(
        run_id="handoff",
        objective="Demo",
        started_at=recorded_at,
        recording_started_at=recorded_at,
        events=[first, second],
    )
    timed = _evidence_timed_captions(
        trace,
        [
            {"scene_id": first.id, "text": "First state"},
            {"scene_id": second.id, "text": "Second state"},
        ],
        screen_seconds=12,
    )
    assert timed is not None
    assert timed[0]["end"] <= 4.8


def test_scroll_caption_uses_the_visible_reveal_interval_not_remote_verification_tail():
    recorded_at = datetime.now(UTC)
    first = InteractionEvent(
        operation_id="first",
        kind=OperationKind.SCROLL_TO,
        intent="Inspect feature",
        action_at=recorded_at + timedelta(seconds=2),
        occurred_at=recorded_at + timedelta(seconds=8),
        before={},
        after={"scroll_motion": {"duration_ms": 1200}},
        success=True,
        duration_ms=1,
    )
    second = InteractionEvent(
        operation_id="second",
        kind=OperationKind.SCROLL_TO,
        intent="Inspect outcome",
        action_at=recorded_at + timedelta(seconds=6),
        occurred_at=recorded_at + timedelta(seconds=12),
        before={},
        after={"scroll_motion": {"duration_ms": 1200}},
        success=True,
        duration_ms=1,
    )
    trace = DemoTrace(
        run_id="reveal-slot",
        objective="Demo",
        started_at=recorded_at,
        recording_started_at=recorded_at,
        events=[first, second],
    )
    timed = _evidence_timed_captions(
        trace,
        [
            {"scene_id": first.id, "text": "The feature result is now visible."},
            {"scene_id": second.id, "text": "The outcome confirms the workflow."},
        ],
        screen_seconds=12,
    )
    assert timed is not None
    assert timed[0]["start"] < 4
    assert timed[0]["end"] - timed[0]["start"] >= 1.4
    assert timed[0]["end"] <= timed[1]["start"]


def test_render_rejects_narration_that_would_outlast_real_capture(monkeypatch, tmp_path: Path):
    artifacts = RunArtifacts(tmp_path, "sync")
    raw = artifacts.execution / "browser-recording.webm"
    raw.write_bytes(b"video")
    narration = artifacts.root / "audio.mp3"
    narration.write_bytes(b"audio")

    class Probe:
        stdout = json.dumps(
            {"format": {"duration": "10"}, "streams": [{"width": 1440, "height": 900}]}
        )

    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if Path(args[0]).name.startswith("remotion") or Path(args[0]).name.startswith("npx"):
            Path(next(arg for arg in args if str(arg).endswith(".mp4"))).write_bytes(
                b"rendered-video"
            )
        return Probe()

    monkeypatch.setattr("productlens.video.render.subprocess.run", run)
    monkeypatch.setattr("productlens.video.render.shutil.copy2", lambda *args: None)
    monkeypatch.setattr("productlens.video.render.audio_duration_seconds", lambda _: 30.0)
    with pytest.raises(NarrationTimingError):
        render_remotion(
            DemoTrace(
                run_id="sync",
                objective="Demo",
                started_at=datetime.now(UTC),
                events=[
                    InteractionEvent(
                        operation_id="op",
                        kind=OperationKind.CLICK,
                        intent="Open dashboard",
                        before={},
                        after={},
                        success=True,
                        duration_ms=1,
                    )
                ],
            ),
            PresentationPlan(trace_run_id="sync", camera=[], cursor_event_ids=[]),
            artifacts,
            narration_path=narration,
        )
    assert not any(Path(args[0]).name.startswith(("remotion", "npx")) for args in calls)


def test_render_rejects_native_capture_that_cannot_fit_approved_duration(
    monkeypatch, tmp_path: Path
):
    artifacts = RunArtifacts(tmp_path, "overlong")
    (artifacts.execution / "browser-recording.webm").write_bytes(b"video")

    class Probe:
        stdout = json.dumps(
            {"format": {"duration": "180"}, "streams": [{"width": 1440, "height": 900}]}
        )

    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return Probe()

    monkeypatch.setattr("productlens.video.render.subprocess.run", run)
    monkeypatch.setattr("productlens.video.render.shutil.copy2", lambda *args: None)
    with pytest.raises(CaptureDurationError):
        render_remotion(
            DemoTrace(run_id="overlong", objective="Demo", started_at=datetime.now(UTC)),
            PresentationPlan(trace_run_id="overlong", camera=[], cursor_event_ids=[]),
            artifacts,
            maximum_duration_seconds=180,
        )
    assert not any(Path(args[0]).name.startswith(("remotion", "npx")) for args in calls)


def test_render_places_beats_using_recording_evidence_time(monkeypatch, tmp_path: Path):
    artifacts = RunArtifacts(tmp_path, "timed")
    (artifacts.execution / "browser-recording.webm").write_bytes(b"video")
    recorded_at = datetime.now(UTC)

    class Probe:
        stdout = json.dumps(
            {"format": {"duration": "12"}, "streams": [{"width": 1440, "height": 900}]}
        )

    def run(args, **kwargs):
        if Path(args[0]).name.startswith("remotion") or Path(args[0]).name.startswith("npx"):
            Path(next(arg for arg in args if str(arg).endswith(".mp4"))).write_bytes(
                b"rendered-video"
            )
        return Probe()

    monkeypatch.setattr("productlens.video.render.subprocess.run", run)
    monkeypatch.setattr("productlens.video.render.shutil.copy2", lambda *args: None)
    trace = DemoTrace(
        run_id="timed",
        objective="Demo",
        started_at=recorded_at + timedelta(seconds=2),
        recording_started_at=recorded_at,
        events=[
            InteractionEvent(
                operation_id="one",
                kind=OperationKind.CLICK,
                intent="First",
                occurred_at=recorded_at + timedelta(seconds=3),
                before={},
                after={},
                success=True,
                duration_ms=1,
            ),
            InteractionEvent(
                operation_id="two",
                kind=OperationKind.CLICK,
                intent="Second",
                occurred_at=recorded_at + timedelta(seconds=9),
                before={},
                after={},
                success=True,
                duration_ms=1,
            ),
        ],
    )
    render_remotion(
        trace,
        PresentationPlan(trace_run_id="timed", camera=[], cursor_event_ids=[]),
        artifacts,
        captions=[
            {"scene_id": trace.events[0].id, "start": 0, "end": 1, "text": "First scene"},
            {"scene_id": trace.events[1].id, "start": 1, "end": 2, "text": "Second scene"},
        ],
    )
    props = json.loads((artifacts.presentation / "remotion-props.json").read_text())
    beats = props["beats"]
    assert beats[0]["start"] < beats[1]["start"]
    assert beats[0]["start"] > 0
    assert beats[0]["start"] < beats[0]["clickFrame"] < beats[1]["start"]
    assert beats[0]["eventId"] == trace.events[0].id
    # The second caption must stay with the event at nine seconds, rather than
    # being assigned to the middle of the twelve-second recording.
    assert props["captions"][1]["start"] > 8


def test_render_uses_action_dispatch_time_for_cursor_beats(monkeypatch, tmp_path: Path):
    artifacts = RunArtifacts(tmp_path, "dispatch")
    (artifacts.execution / "browser-recording.webm").write_bytes(b"video")
    recorded_at = datetime.now(UTC)

    class Probe:
        stdout = json.dumps(
            {"format": {"duration": "12"}, "streams": [{"width": 1440, "height": 900}]}
        )

    def run(args, **kwargs):
        if Path(args[0]).name.startswith("remotion") or Path(args[0]).name.startswith("npx"):
            Path(next(arg for arg in args if str(arg).endswith(".mp4"))).write_bytes(
                b"rendered-video"
            )
        return Probe()

    monkeypatch.setattr("productlens.video.render.subprocess.run", run)
    monkeypatch.setattr("productlens.video.render.shutil.copy2", lambda *args: None)
    event = InteractionEvent(
        operation_id="one",
        kind=OperationKind.CLICK,
        intent="Open",
        action_at=recorded_at + timedelta(seconds=2),
        occurred_at=recorded_at + timedelta(seconds=8),
        before={},
        after={},
        success=True,
        duration_ms=1,
    )
    render_remotion(
        DemoTrace(
            run_id="dispatch",
            objective="Demo",
            started_at=recorded_at,
            recording_started_at=recorded_at,
            events=[event],
        ),
        PresentationPlan(trace_run_id="dispatch", camera=[], cursor_event_ids=[]),
        artifacts,
    )
    props = json.loads((artifacts.presentation / "remotion-props.json").read_text())
    assert props["beats"][0]["clickFrame"] < 3 * 30


def test_caption_waits_for_recorded_visible_result_after_navigation_dispatch(
    monkeypatch, tmp_path: Path
):
    artifacts = RunArtifacts(tmp_path, "caption-result")
    (artifacts.execution / "browser-recording.webm").write_bytes(b"video")
    recorded_at = datetime.now(UTC)

    class Probe:
        stdout = json.dumps(
            {"format": {"duration": "12"}, "streams": [{"width": 1440, "height": 900}]}
        )

    def run(args, **kwargs):
        if Path(args[0]).name.startswith("remotion") or Path(args[0]).name.startswith("npx"):
            Path(next(arg for arg in args if str(arg).endswith(".mp4"))).write_bytes(
                b"rendered-video"
            )
        return Probe()

    monkeypatch.setattr("productlens.video.render.subprocess.run", run)
    event = InteractionEvent(
        operation_id="nav",
        kind=OperationKind.OPEN_NAVIGATION_ITEM,
        intent="Open timeline",
        action_at=recorded_at + timedelta(seconds=2),
        occurred_at=recorded_at + timedelta(seconds=7),
        before={},
        after={},
        success=True,
        duration_ms=1,
    )
    render_remotion(
        DemoTrace(
            run_id="caption-result",
            objective="Demo",
            started_at=recorded_at,
            recording_started_at=recorded_at,
            events=[event],
        ),
        PresentationPlan(trace_run_id="caption-result", camera=[], cursor_event_ids=[]),
        artifacts,
        captions=[
            {
                "scene_id": event.id,
                "start": 0,
                "end": 1,
                "text": "The Timeline shows recorded experience.",
            }
        ],
    )
    props = json.loads((artifacts.presentation / "remotion-props.json").read_text())
    assert props["beats"][0]["clickFrame"] < 3 * 30
    assert props["captions"][0]["start"] >= 6.7


def test_caption_timing_scales_late_evidence_without_collapsing_it_at_the_final_frame():
    recorded_at = datetime.now(UTC)
    events = [
        InteractionEvent(
            operation_id=f"event-{index}",
            kind=OperationKind.CLICK,
            intent=f"Step {index}",
            occurred_at=recorded_at + timedelta(seconds=second),
            before={},
            after={},
            success=True,
            duration_ms=1,
        )
        for index, second in enumerate((3, 10, 18, 30), start=1)
    ]
    trace = DemoTrace(
        run_id="late-events",
        objective="Demo",
        started_at=recorded_at,
        recording_started_at=recorded_at,
        events=events,
    )
    timed = _evidence_timed_captions(
        trace,
        [{"scene_id": event.id, "text": event.intent} for event in events],
        screen_seconds=12,
    )
    assert timed is not None
    assert [caption["start"] for caption in timed] == sorted(caption["start"] for caption in timed)
    assert timed[-1]["start"] < 12
    assert all(caption["end"] <= 12 for caption in timed)


def test_result_caption_is_not_clamped_to_the_next_cursor_action_beat(monkeypatch, tmp_path: Path):
    artifacts = RunArtifacts(tmp_path, "result-caption")
    (artifacts.execution / "browser-recording.webm").write_bytes(b"video")
    recorded_at = datetime.now(UTC)

    class Probe:
        stdout = json.dumps(
            {"format": {"duration": "12"}, "streams": [{"width": 1440, "height": 900}]}
        )

    def run(args, **kwargs):
        if Path(args[0]).name.startswith("remotion") or Path(args[0]).name.startswith("npx"):
            Path(next(arg for arg in args if str(arg).endswith(".mp4"))).write_bytes(
                b"rendered-video"
            )
        return Probe()

    monkeypatch.setattr("productlens.video.render.subprocess.run", run)
    events = [
        InteractionEvent(
            operation_id="first",
            kind=OperationKind.OPEN_NAVIGATION_ITEM,
            intent="Open users",
            action_at=recorded_at + timedelta(seconds=2),
            occurred_at=recorded_at + timedelta(seconds=5),
            before={},
            after={},
            success=True,
            duration_ms=1,
        ),
        InteractionEvent(
            operation_id="second",
            kind=OperationKind.FILL_EMAIL,
            intent="Enter email",
            target=Target(name="Email"),
            target_rect=Rect(x=100, y=200, width=360, height=48),
            action_at=recorded_at + timedelta(seconds=5.3),
            occurred_at=recorded_at + timedelta(seconds=9),
            before={},
            after={},
            success=True,
            duration_ms=1,
        ),
    ]
    render_remotion(
        DemoTrace(
            run_id="result-caption",
            objective="Demo",
            started_at=recorded_at,
            recording_started_at=recorded_at,
            events=events,
        ),
        PresentationPlan(trace_run_id="result-caption", camera=[], cursor_event_ids=[]),
        artifacts,
        captions=[
            {"scene_id": event.id, "start": 0, "end": 1, "text": event.intent} for event in events
        ],
    )
    props = json.loads((artifacts.presentation / "remotion-props.json").read_text())
    assert all(caption["end"] > caption["start"] for caption in props["captions"])
    first_beat = props["beats"][0]
    assert props["captions"][0]["start"] > first_beat["end"] / props["frameRate"]


def test_presenter_welcome_begins_on_the_first_stable_product_frame():
    recorded_at = datetime.now(UTC)
    event = InteractionEvent(
        operation_id="opening",
        kind=OperationKind.SCROLL_TO,
        intent="Establish home",
        action_at=recorded_at + timedelta(seconds=4),
        occurred_at=recorded_at + timedelta(seconds=7),
        before={},
        after={},
        success=True,
        duration_ms=1,
    )
    trace = DemoTrace(
        run_id="opening",
        objective="Portfolio",
        started_at=recorded_at,
        recording_started_at=recorded_at,
        events=[event],
    )
    timed = _evidence_timed_captions(
        trace,
        [
            {
                "scene_id": event.id,
                "text": "Welcome to Dhruv's portfolio tour. Today, we will explore the work and career story.",
            }
        ],
        screen_seconds=12,
    )
    assert timed is not None
    assert timed[0]["start"] == 0.0


def test_presenter_welcome_waits_for_its_product_event_after_authentication():
    recorded_at = datetime.now(UTC)
    login = InteractionEvent(
        operation_id="login",
        kind=OperationKind.SUBMIT,
        intent="Sign in",
        action_at=recorded_at + timedelta(seconds=1),
        occurred_at=recorded_at + timedelta(seconds=3),
        before={},
        after={},
        success=True,
        duration_ms=1,
    )
    opening = InteractionEvent(
        operation_id="opening",
        kind=OperationKind.VERIFY_STATE,
        intent="Establish workspace",
        action_at=recorded_at + timedelta(seconds=6),
        occurred_at=recorded_at + timedelta(seconds=7),
        before={},
        after={},
        success=True,
        duration_ms=1,
    )
    trace = DemoTrace(
        run_id="post-auth-opening",
        objective="Demo",
        started_at=recorded_at,
        recording_started_at=recorded_at,
        events=[login, opening],
    )
    timed = _evidence_timed_captions(
        trace,
        [
            {
                "scene_id": opening.id,
                "opening": True,
                "text": "Welcome to the workspace. We will review the verified workflow.",
            }
        ],
        screen_seconds=12,
    )
    assert timed is not None
    assert timed[0]["start"] >= 6


def test_render_namespaces_media_asset_to_current_retry_artifact_directory(
    monkeypatch, tmp_path: Path
):
    artifacts = RunArtifacts(tmp_path, "retry-child")
    (artifacts.execution / "browser-recording.webm").write_bytes(b"video")

    class Probe:
        stdout = json.dumps(
            {"format": {"duration": "6"}, "streams": [{"width": 1440, "height": 900}]}
        )

    def run(args, **kwargs):
        if Path(args[0]).name.startswith("remotion") or Path(args[0]).name.startswith("npx"):
            Path(next(arg for arg in args if str(arg).endswith("demo.candidate.mp4"))).write_bytes(
                b"rendered-video"
            )
        return Probe()

    monkeypatch.setattr("productlens.video.render.subprocess.run", run)
    # Simulate a cloned trace retaining its historical parent run id.
    trace = DemoTrace(
        run_id="retry-parent",
        objective="Demo",
        started_at=datetime.now(UTC),
        recording_started_at=datetime.now(UTC),
        events=[],
    )
    render_remotion(
        trace,
        PresentationPlan(trace_run_id="retry-parent", camera=[], cursor_event_ids=[]),
        artifacts,
    )
    props = json.loads((artifacts.presentation / "remotion-props.json").read_text())
    # Browser recordings are materialized as a run-scoped H.264 source for
    # reliable long OffthreadVideo rendering; the original WebM stays in the
    # immutable execution evidence directory.
    assert props["screenVideo"] == "retry-child.mp4"
