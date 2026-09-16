from pathlib import Path

from app.quality.video import (
    _frame_pacing,
    _mapped_source_second,
    _timestamp_pacing,
    inspect_video,
)


def test_editorial_source_time_map_tracks_concatenated_windows():
    windows = [(17.5, 32.5), (44.5, 52.5)]
    assert _mapped_source_second(0.0, windows) == 17.5
    assert _mapped_source_second(15.0, windows) == 32.5
    assert _mapped_source_second(16.0, windows) == 45.5
    assert _mapped_source_second(24.0, windows) == 52.5


def test_missing_video_is_a_hard_failure(tmp_path: Path):
    report = inspect_video(tmp_path / "missing.mp4", execution_verified=True)
    assert "MISSING_OR_EMPTY_RENDER" in report["hard_failures"]


def test_uniform_sampled_frames_are_rejected(monkeypatch, tmp_path: Path):
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x" * 10_001)
    monkeypatch.setattr(
        "app.quality.video.subprocess.run",
        lambda *args, **kwargs: type(
            "R",
            (),
            {
                "stdout": '{"streams":[{"codec_type":"video","width":1920,"height":1080,"avg_frame_rate":"24/1"}],"format":{"duration":"10","bit_rate":"800000"}}',
                "stderr": "",
            },
        )(),
    )
    monkeypatch.setattr(
        "app.quality.video._sample_frame_quality", lambda *args: [{"variance": 0.0}]
    )
    report = inspect_video(video, execution_verified=True)
    assert "VISUALLY_EMPTY_RENDER" in report["hard_failures"]


def test_near_white_opening_is_rejected_even_with_compositor_edges(monkeypatch, tmp_path: Path):
    video = tmp_path / "white-opening.mp4"
    video.write_bytes(b"x" * 10_001)
    monkeypatch.setattr(
        "app.quality.video.subprocess.run",
        lambda *args, **kwargs: type(
            "R",
            (),
            {
                "stdout": '{"streams":[{"codec_type":"video","width":1920,"height":1080,"avg_frame_rate":"30/1"}],"format":{"duration":"10","bit_rate":"800000"}}',
                "stderr": "",
            },
        )(),
    )
    monkeypatch.setattr(
        "app.quality.video._sample_frame_quality",
        lambda *args: [
            {"second": 2.2, "mean_luma": 252.0, "variance": 12.0},
            {"second": 5.0, "mean_luma": 120.0, "variance": 12.0},
        ],
    )
    report = inspect_video(video, execution_verified=True)
    assert "UNESTABLISHED_OR_BLANK_OPENING_FRAME" in report["hard_failures"]


def test_sustained_blank_product_region_is_rejected(monkeypatch, tmp_path: Path):
    video = tmp_path / "blank-middle.mp4"
    video.write_bytes(b"x" * 10_001)
    monkeypatch.setattr(
        "app.quality.video.subprocess.run",
        lambda *args, **kwargs: type(
            "R",
            (),
            {
                "stdout": '{"streams":[{"codec_type":"video","width":1920,"height":1080,"avg_frame_rate":"30/1"}],"format":{"duration":"120","bit_rate":"800000"}}',
                "stderr": "",
            },
        )(),
    )
    monkeypatch.setattr(
        "app.quality.video._sample_frame_quality", lambda *args: [{"variance": 12.0}]
    )
    monkeypatch.setattr(
        "app.quality.video._sample_content_quality",
        lambda *args: [
            {"second": 24.0, "mean_luma": 254.5, "variance": 10.0},
            {"second": 60.0, "mean_luma": 254.2, "variance": 12.0},
        ],
    )
    report = inspect_video(video, execution_verified=True)
    assert "BLANK_PRODUCT_CONTENT_INTERVAL" in report["hard_failures"]


def test_render_is_rejected_when_it_does_not_preserve_any_browser_footage(
    monkeypatch, tmp_path: Path
):
    video = tmp_path / "demo.mp4"
    source = tmp_path / "browser.webm"
    video.write_bytes(b"x" * 10_001)
    source.write_bytes(b"source")
    monkeypatch.setattr(
        "app.quality.video.subprocess.run",
        lambda *args, **kwargs: type(
            "R",
            (),
            {
                "stdout": '{"streams":[{"codec_type":"video","width":1920,"height":1080,"avg_frame_rate":"30/1"}],"format":{"duration":"120","bit_rate":"800000"}}',
                "stderr": "",
            },
        )(),
    )
    monkeypatch.setattr(
        "app.quality.video._sample_frame_quality", lambda *args: [{"variance": 12.0}]
    )
    monkeypatch.setattr(
        "app.quality.video._source_faithfulness",
        lambda **kwargs: [{"second": 24.0, "correlation": 0.14}],
    )

    report = inspect_video(video, execution_verified=True, source_video=source)

    assert "SOURCE_FOOTAGE_STRUCTURALLY_UNRELATED" in report["hard_failures"]


def test_render_is_rejected_when_only_one_sample_resembles_browser_evidence(
    monkeypatch, tmp_path: Path
):
    video = tmp_path / "demo.mp4"
    source = tmp_path / "browser.webm"
    video.write_bytes(b"x" * 10_001)
    source.write_bytes(b"source")
    monkeypatch.setattr(
        "app.quality.video.subprocess.run",
        lambda *args, **kwargs: type(
            "R",
            (),
            {
                "stdout": '{"streams":[{"codec_type":"video","width":1920,"height":1080,"avg_frame_rate":"30/1"}],"format":{"duration":"120","bit_rate":"800000"}}',
                "stderr": "",
            },
        )(),
    )
    monkeypatch.setattr(
        "app.quality.video._sample_frame_quality", lambda *args: [{"variance": 12.0}]
    )
    monkeypatch.setattr(
        "app.quality.video._source_faithfulness",
        lambda **kwargs: [
            {"second": 24.0, "correlation": 0.20},
            {"second": 60.0, "correlation": 0.93},
            {"second": 96.0, "correlation": 0.18},
        ],
    )

    report = inspect_video(video, execution_verified=True, source_video=source)

    assert "SOURCE_FOOTAGE_LOW_STRUCTURAL_SIMILARITY" in report["hard_failures"]


def test_frame_pacing_reports_material_cadence_error():
    report = _frame_pacing({"nb_frames": "150"}, duration=10, measured_rate=30)
    assert report["cadence_error_ratio"] == 0.5


def test_timestamp_pacing_rejects_a_visible_gap(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "app.quality.video.subprocess.run",
        lambda *args, **kwargs: type(
            "R", (), {"stdout": "0.000\n0.033\n0.066\n1.400\n", "stderr": ""}
        )(),
    )
    report = _timestamp_pacing(tmp_path / "demo.mp4", measured_rate=30)
    assert report["material_gap_count"] == 1.0
    assert report["max_gap_seconds"] > 1


def test_objective_duration_envelope_is_a_delivery_gate(monkeypatch, tmp_path: Path):
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x" * 10_001)
    monkeypatch.setattr(
        "app.quality.video.subprocess.run",
        lambda *args, **kwargs: type(
            "R",
            (),
            {
                "stdout": '{"streams":[{"codec_type":"video","width":1920,"height":1080,"avg_frame_rate":"30/1"}],"format":{"duration":"181","bit_rate":"800000"}}',
                "stderr": "",
            },
        )(),
    )
    monkeypatch.setattr(
        "app.quality.video._sample_frame_quality", lambda *args: [{"variance": 12.0}]
    )
    report = inspect_video(
        video,
        execution_verified=True,
        minimum_duration_seconds=90,
        maximum_duration_seconds=180,
    )
    assert "RENDER_EXCEEDS_OBJECTIVE_MAXIMUM_DURATION" in report["hard_failures"]
    assert report["duration_bounds_seconds"] == {"minimum": 90, "maximum": 180}
