from pathlib import Path

from productlens.providers.openrouter import OpenRouterVisualReviewer


def test_visual_reviewer_refuses_to_send_a_request_without_real_frames(tmp_path: Path):
    reviewer = OpenRouterVisualReviewer("test-key", "provider/vision")
    report = reviewer({"run_id": "run", "frames": [{"path": str(tmp_path / "missing.png")}]})
    assert report["provider"] == "openrouter"
    assert report["hard_failures"] == ["MULTIMODAL_REVIEW_FRAMES_UNAVAILABLE"]
