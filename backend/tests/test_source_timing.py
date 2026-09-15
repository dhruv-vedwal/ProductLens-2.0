import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from productlens.contracts.models import DemoTrace, InteractionEvent, OperationKind
from productlens.video.render import _editorial_cut_windows
from productlens.video.source_timing import align_trace_to_recording


def test_cloud_source_timing_maps_trace_to_native_recording_evidence(monkeypatch, tmp_path: Path):
    started = datetime.now(UTC)
    frames = tmp_path / "cloud-screencast-frames"
    frames.mkdir()
    (frames / "timing.json").write_text(
        json.dumps(
            {
                "frames": [
                    {
                        "index": index,
                        "received_at": (started + timedelta(seconds=second)).isoformat(),
                    }
                    for index, second in enumerate((0, 3, 6, 9))
                ]
            }
        ),
        encoding="utf-8",
    )
    recording = tmp_path / "browser-recording.mp4"
    recording.write_bytes(b"recording")
    events = [
        InteractionEvent(
            operation_id=f"step-{index}",
            kind=OperationKind.CLICK,
            intent="Open section",
            action_at=started + timedelta(seconds=action),
            occurred_at=started + timedelta(seconds=reveal),
            before={},
            after={},
            success=True,
            duration_ms=1,
        )
        for index, (action, reveal) in enumerate(((3, 6), (8, 9)), start=1)
    ]
    trace = DemoTrace(
        run_id="timed",
        objective="Demo",
        started_at=started,
        recording_started_at=started,
        events=events,
    )
    monkeypatch.setattr(
        "productlens.video.source_timing._video_hashes",
        lambda _: [(0.0, 1), (5.0, 2), (10.0, 3), (15.0, 4)],
    )
    monkeypatch.setattr(
        "productlens.video.source_timing._image_hash",
        lambda path: int(path.stem.rsplit("-", 1)[-1]) + 1,
    )

    aligned, report = align_trace_to_recording(trace, recording=recording, screencast_frames=frames)

    assert report["status"] == "aligned"
    assert aligned.events[0].action_at == started + timedelta(seconds=5)
    assert aligned.events[0].occurred_at == started + timedelta(seconds=10)
    assert aligned.events[1].action_at <= aligned.events[1].occurred_at
    assert len(report["findings"]) == 4


def test_cloud_source_timing_refuses_missing_clock_evidence(tmp_path: Path):
    started = datetime.now(UTC)
    recording = tmp_path / "browser-recording.mp4"
    recording.write_bytes(b"recording")
    trace = DemoTrace(
        run_id="missing", objective="Demo", started_at=started, recording_started_at=started
    )

    aligned, report = align_trace_to_recording(
        trace, recording=recording, screencast_frames=tmp_path / "none"
    )

    assert aligned is trace
    assert report["status"] == "unavailable"


def test_cloud_scroll_alignment_uses_temporal_witness_when_dispatch_frame_is_sparse(
    monkeypatch, tmp_path: Path
):
    """A valid cloud scroll is not rejected because its dispatch pixel is transitional."""
    started = datetime.now(UTC)
    frames = tmp_path / "cloud-screencast-frames"
    frames.mkdir()
    (frames / "timing.json").write_text(
        json.dumps(
            {
                "frames": [
                    {
                        "index": index,
                        "received_at": (started + timedelta(seconds=second)).isoformat(),
                    }
                    for index, second in enumerate((0, 3, 6, 9))
                ]
            }
        ),
        encoding="utf-8",
    )
    recording = tmp_path / "browser-recording.mp4"
    recording.write_bytes(b"recording")
    event = InteractionEvent(
        operation_id="scroll",
        kind=OperationKind.SCROLL_TO,
        intent="Reveal the next section",
        action_at=started + timedelta(seconds=3),
        occurred_at=started + timedelta(seconds=6),
        before={},
        after={},
        success=True,
        duration_ms=1,
    )
    trace = DemoTrace(
        run_id="scroll-timing",
        objective="Demo",
        started_at=started,
        recording_started_at=started,
        events=[event],
    )
    monkeypatch.setattr(
        "productlens.video.source_timing._video_hashes",
        lambda _: [(0.0, 0), (5.0, 0), (10.0, 0), (15.0, 0)],
    )
    # Deliberately unrelated pixels: temporal evidence should still ground
    # the scroll dispatch and reveal in the native recording.
    monkeypatch.setattr(
        "productlens.video.source_timing._image_hash",
        lambda _: (1 << 575) - 1,
    )

    aligned, report = align_trace_to_recording(trace, recording=recording, screencast_frames=frames)

    assert report["status"] == "aligned"
    assert report["average_confidence"] >= 0.75
    assert aligned.events[0].action_at <= aligned.events[0].occurred_at


def test_editorial_cut_windows_expand_native_context_to_story_duration_floor():
    started = datetime.now(UTC)
    trace = DemoTrace(
        run_id="floor",
        objective="full walkthrough",
        started_at=started,
        recording_started_at=started,
        events=[
            InteractionEvent(
                operation_id="one",
                kind=OperationKind.CLICK,
                intent="Open detail",
                action_at=started + timedelta(seconds=20),
                occurred_at=started + timedelta(seconds=24),
                success=True,
                duration_ms=4_000,
            ),
            InteractionEvent(
                operation_id="two",
                kind=OperationKind.CLICK,
                intent="Show outcome",
                action_at=started + timedelta(seconds=80),
                occurred_at=started + timedelta(seconds=84),
                success=True,
                duration_ms=4_000,
            ),
        ],
    )

    windows = _editorial_cut_windows(trace, 120.0, minimum_seconds=90.0)

    assert sum(end - start for start, end in windows) >= 89.9
    assert windows[0][0] >= 0
    assert windows[-1][1] <= 120


def test_cloud_scroll_cut_uses_recorded_motion_not_remote_verification_latency():
    started = datetime.now(UTC)
    event = InteractionEvent(
        operation_id="scroll",
        kind=OperationKind.SCROLL_TO,
        intent="Reveal the projects",
        action_at=started + timedelta(seconds=10),
        occurred_at=started + timedelta(seconds=30),
        before={},
        after={"scroll_motion": {"duration_ms": 2_000, "steps": 12}},
        success=True,
        duration_ms=20_000,
    )
    trace = DemoTrace(
        run_id="cloud-scroll",
        objective="Demo",
        started_at=started,
        recording_started_at=started,
        events=[event],
    )
    windows = _editorial_cut_windows(trace, 40.0)
    # The retained source contains the physical two-second scroll and settle,
    # not the eighteen seconds spent waiting on remote postcondition queries.
    assert windows == [(pytest.approx(4.55), pytest.approx(12.55))]
