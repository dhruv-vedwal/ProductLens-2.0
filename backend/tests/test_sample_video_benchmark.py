from pathlib import Path

from productlens.evaluation.sample_video_benchmark import (
    build_sample_benchmark,
    compare_to_sample_benchmark,
)


def test_sample_benchmark_derives_measured_and_editorial_envelopes(monkeypatch, tmp_path: Path):
    (tmp_path / "one.mp4").write_bytes(b"one")
    (tmp_path / "two.mov").write_bytes(b"two")
    monkeypatch.setattr(
        "productlens.evaluation.sample_video_benchmark.probe_video",
        lambda path: {
            "file": path.name, "codec": "h264", "width": 1920 if path.stem == "one" else 1280,
            "height": 1080 if path.stem == "one" else 720, "duration_seconds": 90 if path.stem == "one" else 120,
            "frame_rate": 30.0, "nominal_frame_rate": 30.0, "frame_count": 0, "bit_rate": 1_000_000,
        },
    )
    benchmark = build_sample_benchmark(tmp_path)
    assert benchmark["sample_count"] == 2
    assert benchmark["measured_envelope"]["minimum_frame_rate"] == 30.0
    assert benchmark["quality_target"]["width"] == 1600
    assert "story" in benchmark["editorial_rubric"]
    assert benchmark["limitations"]


def test_benchmark_comparison_does_not_claim_editorial_parity_from_media_probe(monkeypatch, tmp_path: Path):
    output = tmp_path / "output.mp4"
    output.write_bytes(b"output")
    monkeypatch.setattr(
        "productlens.evaluation.sample_video_benchmark.probe_video",
        lambda _: {
            "file": "output.mp4", "codec": "h264", "width": 1920, "height": 1080,
            "duration_seconds": 100.0, "frame_rate": 30.0, "nominal_frame_rate": 30.0,
            "frame_count": 3000, "bit_rate": 1_000_000,
        },
    )
    benchmark = {
        "benchmark_id": "sample", "measured_envelope": {
            "minimum_width": 1280, "minimum_height": 720,
            "minimum_frame_rate": 30.0, "shortest_duration_seconds": 75.0,
        },
        "quality_target": {"width": 1920, "height": 1080, "frame_rate": 30.0, "minimum_duration_seconds": 75.0},
    }
    report = compare_to_sample_benchmark(output, benchmark)
    assert report["measured_match"] is True
    assert report["editorial_review_required"]
    assert "not sufficient" in report["note"]


def test_benchmark_comparison_rejects_output_below_measured_sample_envelope(monkeypatch, tmp_path: Path):
    output = tmp_path / "output.mp4"
    output.write_bytes(b"output")
    monkeypatch.setattr(
        "productlens.evaluation.sample_video_benchmark.probe_video",
        lambda _: {
            "file": "output.mp4", "codec": "h264", "width": 640, "height": 360,
            "duration_seconds": 10.0, "frame_rate": 24.0, "nominal_frame_rate": 24.0,
            "frame_count": 240, "bit_rate": 1,
        },
    )
    report = compare_to_sample_benchmark(output, {"measured_envelope": {
        "minimum_width": 1280, "minimum_height": 720,
        "minimum_frame_rate": 30.0, "shortest_duration_seconds": 75.0,
    }})
    assert set(report["hard_failures"]) == {
        "BELOW_SAMPLE_RESOLUTION_ENVELOPE", "BELOW_SAMPLE_FRAME_RATE_ENVELOPE", "BELOW_SAMPLE_DURATION_ENVELOPE",
    }
