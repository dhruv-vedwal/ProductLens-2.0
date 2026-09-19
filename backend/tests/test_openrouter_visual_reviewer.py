from pathlib import Path

from app.providers.openrouter import OpenRouterVisualReviewer


def test_visual_reviewer_refuses_to_send_a_request_without_real_frames(tmp_path: Path):
    reviewer = OpenRouterVisualReviewer("test-key", "provider/vision")
    report = reviewer({"run_id": "run", "frames": [{"path": str(tmp_path / "missing.png")}]})
    assert report["provider"] == "openrouter"
    assert report["hard_failures"] == ["MULTIMODAL_REVIEW_FRAMES_UNAVAILABLE"]


def test_visual_reviewer_treats_parse_errors_as_hard_failures(monkeypatch, tmp_path: Path):
    reviewer = OpenRouterVisualReviewer("test-key", "provider/vision")
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"png")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "not-json{"}}]}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr("app.providers.openrouter.httpx.Client", Client)
    report = reviewer({"run_id": "run", "frames": [{"path": str(frame)}]})
    assert report["hard_failures"] == ["MULTIMODAL_REVIEW_UNAVAILABLE:JSONDecodeError"]
    assert report["warnings"] == []
