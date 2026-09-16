from pathlib import Path

import pytest

from app.providers.browserbase import BrowserbaseProvider


@pytest.mark.asyncio
async def test_close_session_treats_already_closed_browserbase_session_as_success(monkeypatch):
    class Response:
        status_code = 404

        def raise_for_status(self):
            raise AssertionError("404 must be accepted as an idempotent close")

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, _url, **kwargs):
            assert kwargs["json"] == {"status": "REQUEST_RELEASE"}
            return Response()

    monkeypatch.setattr(
        "app.providers.browserbase.httpx.AsyncClient", lambda **kwargs: Client()
    )
    await BrowserbaseProvider("test-key").close_session("already-closed")


@pytest.mark.asyncio
async def test_close_session_retries_transient_transport_failure(monkeypatch):
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

    class Client:
        attempts = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, _url, **kwargs):
            assert kwargs["json"] == {"status": "REQUEST_RELEASE"}
            self.attempts += 1
            if self.attempts == 1:
                raise httpx.ConnectError("temporary DNS failure")
            return Response()

    import httpx

    client = Client()
    monkeypatch.setattr(
        "app.providers.browserbase.httpx.AsyncClient", lambda **kwargs: client
    )

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.providers.browserbase.asyncio.sleep", no_sleep)
    await BrowserbaseProvider("test-key").close_session("session-1")
    assert client.attempts == 2


@pytest.mark.asyncio
async def test_create_session_uses_only_the_browserbase_api_key(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, _url, **kwargs):
            assert kwargs["headers"]["x-bb-api-key"] == "test-key"
            if _url.endswith("/extensions"):
                assert "file" in kwargs["files"]
                return Response({"id": "extension-1"})
            assert kwargs["json"] == {
                "extensionId": "extension-1",
                "timeout": 1800,
                "browserSettings": {"recordSession": True, "solveCaptchas": True},
            }
            return Response({"id": "session-1", "connectUrl": "wss://example.test/session-1"})

    monkeypatch.setattr(
        "app.providers.browserbase.httpx.AsyncClient", lambda **kwargs: Client()
    )
    session = await BrowserbaseProvider("test-key").create_session_info()
    assert session.session_id == "session-1"
    assert session.stagehand_extension_id == "extension-1"


@pytest.mark.asyncio
async def test_create_session_passes_recording_viewport_and_non_secret_metadata(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            if url.endswith("/extensions"):
                return Response({})
            assert kwargs["json"]["browserSettings"]["viewport"] == {"width": 1440, "height": 900}
            assert kwargs["json"]["userMetadata"] == {
                "productlens_run_id": "run-1",
                "stage": "production",
            }
            return Response({"id": "session-2", "connectUrl": "wss://example.test/session-2"})

    monkeypatch.setattr(
        "app.providers.browserbase.httpx.AsyncClient", lambda **kwargs: Client()
    )
    session = await BrowserbaseProvider("test-key").create_session_info(
        viewport={"width": 1440, "height": 900},
        user_metadata={"productlens_run_id": "run-1", "stage": "production"},
    )
    assert session.session_id == "session-2"


@pytest.mark.asyncio
async def test_native_recording_download_uses_signed_url_without_returning_it(
    monkeypatch, tmp_path: Path
):
    class Response:
        def __init__(self, payload=None, content=b""):
            self.payload = payload or {}
            self.content = content

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        poll_count = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            assert url.endswith("/sessions/session-1/recording/downloads")
            assert kwargs["headers"]["x-bb-api-key"] == "test-key"
            return Response({"downloads": [{"pageId": "0", "status": "PENDING"}]})

        async def get(self, url, **kwargs):
            if url == "https://signed.example.test/recording.mp4":
                assert kwargs["follow_redirects"] is True
                return Response(content=b"native-mp4")
            self.poll_count += 1
            return Response(
                {
                    "downloads": [
                        {
                            "pageId": "0",
                            "status": "COMPLETED",
                            "downloadUrl": "https://signed.example.test/recording.mp4",
                        }
                    ]
                }
            )

    monkeypatch.setattr(
        "app.providers.browserbase.httpx.AsyncClient", lambda **kwargs: Client()
    )
    output = tmp_path / "browser-recording.mp4"
    result = await BrowserbaseProvider("test-key").download_native_recording("session-1", output)
    assert output.read_bytes() == b"native-mp4"
    assert result == {
        "provider": "browserbase",
        "session_id": "session-1",
        "page_id": "0",
        "artifact": str(output),
    }
    assert "signed" not in str(result)


@pytest.mark.asyncio
async def test_session_replay_video_assembles_documented_hls_playlist(monkeypatch, tmp_path: Path):
    class Response:
        status_code = 200
        text = "#EXTM3U\nhttps://cdn.example/segment.m4s\n"

        def raise_for_status(self):
            return None

        def json(self):
            return {"pages": [{"pageId": "0", "startTimeMs": 0, "endTimeMs": 120000}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            return Response()

    async def fake_to_thread(_fn, *args, **kwargs):
        Path(args[0][-1]).write_bytes(b"assembled-mp4")

    monkeypatch.setattr(
        "app.providers.browserbase.httpx.AsyncClient", lambda **kwargs: Client()
    )
    monkeypatch.setattr("app.providers.browserbase.asyncio.to_thread", fake_to_thread)
    output = tmp_path / "replay.mp4"
    result = await BrowserbaseProvider("test-key").download_session_replay_video(
        "session-1", output
    )
    assert output.read_bytes() == b"assembled-mp4"
    assert result["page_id"] == "0"


@pytest.mark.asyncio
async def test_session_replay_assembly_respects_long_caller_deadline(monkeypatch, tmp_path: Path):
    """Long editorial captures must not be truncated by a provider-local cap."""

    class Response:
        status_code = 200
        text = "#EXTM3U\nhttps://cdn.example/segment.m4s\n"

        def raise_for_status(self):
            return None

        def json(self):
            return {"pages": [{"pageId": "0", "startTimeMs": 0, "endTimeMs": 600000}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            return Response()

    seen: dict[str, object] = {}

    async def fake_to_thread(_fn, *args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        Path(args[0][-1]).write_bytes(b"assembled-mp4")

    monkeypatch.setattr(
        "app.providers.browserbase.httpx.AsyncClient", lambda **kwargs: Client()
    )
    monkeypatch.setattr("app.providers.browserbase.asyncio.to_thread", fake_to_thread)
    output = tmp_path / "long-replay.mp4"
    await BrowserbaseProvider("test-key").download_session_replay_video(
        "session-1", output, timeout_seconds=900
    )
    assert seen["timeout"] == 900


def test_browserbase_deadlines_remain_bounded_by_the_session_policy():
    provider = BrowserbaseProvider("test-key", session_timeout_seconds=600)
    assert provider._bounded_deadline(900) == 600
    assert provider._bounded_deadline(5) == 10
