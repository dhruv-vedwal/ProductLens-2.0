from pathlib import Path
import json

from app.quality.multimodal import build_review_packet, review_multimodal


def test_multimodal_packet_is_non_secret_and_provider_neutral(tmp_path: Path):
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"video")
    packet = build_review_packet(
        video=video,
        run_id="run-1",
        trace={"events": [{"id": "e1"}]},
        storyboard={"scenes": [{"id": "s1"}]},
        sample_seconds=[1, 2.5],
    )
    assert packet["event_count"] == 1
    assert "password" not in str(packet).lower()
    assert review_multimodal(packet)["status"] == "deterministic_complete"


def test_multimodal_reviewer_findings_are_normalized(tmp_path: Path):
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"video")
    packet = build_review_packet(video=video, run_id="run", trace={})
    seen = {}
    report = review_multimodal(
        packet,
        lambda payload: (seen.update(payload), {"provider": "test", "findings": ["smooth"]})[1],
    )
    assert report["status"] == "complete"
    assert report["findings"] == ["smooth"]
    assert "frames" in seen


def test_multimodal_reviewer_parse_failure_is_unavailable(tmp_path: Path, monkeypatch):
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"video")
    packet = build_review_packet(video=video, run_id="run", trace={})
    monkeypatch.setattr("app.quality.multimodal.extract_review_frames", lambda **_kwargs: [])

    def broken_reviewer(_payload):
        raise json.JSONDecodeError("Expecting value", "not-json{", 0)

    report = review_multimodal(packet, broken_reviewer)
    assert report["status"] == "unavailable"
    assert "MULTIMODAL_REVIEW_UNAVAILABLE:JSONDecodeError" in report["hard_failures"]
