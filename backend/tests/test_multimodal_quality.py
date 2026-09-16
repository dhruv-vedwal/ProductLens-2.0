from pathlib import Path

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
