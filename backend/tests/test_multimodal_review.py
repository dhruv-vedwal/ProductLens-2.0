from pathlib import Path

from app.quality.delivery import delivery_report
from app.quality.multimodal import build_review_packet, review_multimodal


def test_default_visual_review_extracts_real_review_frames(monkeypatch, tmp_path: Path):
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"video")

    def fake_run(command, **_kwargs):
        output = Path(command[-1])
        output.write_bytes(b"png")
        return type("R", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr("app.quality.multimodal.subprocess.run", fake_run)
    packet = build_review_packet(
        video=video, run_id="run", trace={"events": []}, sample_seconds=[1]
    )
    review = review_multimodal(packet)

    assert review["status"] == "deterministic_complete"
    assert review["hard_failures"] == []
    assert review["findings"][0]["kind"] == "rendered_frame"


def test_visual_review_hard_failures_block_delivery():
    report = delivery_report(
        artifacts={"video": True},
        execution={"execution_score": 1},
        story={"story_score": 1},
        video={"visual_score": 1},
        visual_review={"hard_failures": ["RENDERED_FRAME_EXTRACTION_FAILED"]},
    )
    assert not report["deliverable"]
    assert "RENDERED_FRAME_EXTRACTION_FAILED" in report["hard_failures"]
