from __future__ import annotations

import asyncio
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from productlens.providers.errors import ProviderError


@dataclass(frozen=True)
class BrowserbaseSessionInfo:
    session_id: str
    connect_url: str


class BrowserbaseProvider:
    """Creates remote browser sessions; Playwright remains the execution owner."""

    def __init__(self, api_key: str, project_id: str | None = None, *, session_timeout_seconds: int = 1800):
        self.api_key = api_key
        self.project_id = project_id
        self.session_timeout_seconds = max(60, min(1800, int(session_timeout_seconds)))

    async def create_session(self) -> str:
        return (await self.create_session_info()).session_id

    async def create_session_info(self) -> BrowserbaseSessionInfo:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    "https://api.browserbase.com/v1/sessions",
                    headers={"x-bb-api-key": self.api_key, "Content-Type": "application/json"},
                    # Native Browserbase recording is the production visual
                    # source. CDP screencasts are retained only as a compact
                    # diagnostic fallback because remote compositor frames are
                    # sparse during real reading holds.
                    json={
                        **({"projectId": self.project_id} if self.project_id else {}),
                        "timeout": self.session_timeout_seconds,
                        # Developer-plan Identity includes automatic CAPTCHA
                        # solving. Keep it explicit so a project-level setting
                        # cannot silently disable the capability required by a
                        # protected target. Proxies remain opt-in because they
                        # consume separately metered bandwidth.
                        "browserSettings": {
                            "recordSession": True,
                            "solveCaptchas": True,
                        },
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError as error:
            response = getattr(error, "response", None)
            raise ProviderError("browserbase", getattr(response, "status_code", None), str(error)) from error
        payload = response.json()
        connect_url = payload.get("connectUrl") or payload.get("connect_url")
        if not payload.get("id") or not connect_url:
            raise RuntimeError("Browserbase session response did not include id and connectUrl")
        return BrowserbaseSessionInfo(session_id=payload["id"], connect_url=connect_url)

    async def close_session(self, session_id: str) -> None:
        """Release a remote session after CDP disconnect, including failed discovery runs."""
        # DNS and transient transport loss must not turn an otherwise complete
        # exploration into a failed run on the first close attempt. Browserbase
        # releases sessions through the Sessions API update endpoint (rather
        # than DELETE); the request is idempotent and does not repeat any
        # browser action or user-side effect.
        last_error: httpx.HTTPError | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.post(
                        f"https://api.browserbase.com/v1/sessions/{session_id}",
                        headers={"x-bb-api-key": self.api_key, "Content-Type": "application/json"},
                        json={
                            "status": "REQUEST_RELEASE",
                            **({"projectId": self.project_id} if self.project_id else {}),
                        },
                    )
                    # Session termination is idempotent: Stagehand or a timed-out
                    # browser process may already have released it before the
                    # ProductLens lifecycle owner performs its final cleanup.
                    if response.status_code == 404:
                        return
                    response.raise_for_status()
                    return
            except httpx.HTTPError as error:
                last_error = error
                if attempt < 2:
                    await asyncio.sleep(1 + attempt)
        assert last_error is not None
        response = getattr(last_error, "response", None)
        raise ProviderError("browserbase", getattr(response, "status_code", None), str(last_error)) from last_error

    async def download_native_recording(
        self, session_id: str, output: Path, *, timeout_seconds: int = 120
    ) -> dict[str, str]:
        """Request and download Browserbase's source-faithful MP4 recording.

        Browserbase produces this asynchronously after the session ends. The
        signed URL is intentionally neither persisted nor logged; the returned
        metadata is sufficient for a durable artifact manifest.
        """
        headers = {"x-bb-api-key": self.api_key, "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                requested = await client.post(
                    f"https://api.browserbase.com/v1/sessions/{session_id}/recording/downloads",
                    headers=headers,
                    json={},
                )
                requested.raise_for_status()
                attempts = max(1, timeout_seconds // 2)
                completed: dict | None = None
                for _ in range(attempts):
                    response = await client.get(
                        f"https://api.browserbase.com/v1/sessions/{session_id}/recording/downloads",
                        headers=headers,
                    )
                    response.raise_for_status()
                    downloads = response.json().get("downloads", [])
                    failed = [item for item in downloads if str(item.get("status", "")).upper() == "FAILED"]
                    if failed:
                        raise RuntimeError("Browserbase native recording assembly failed")
                    completed = next(
                        (
                            item for item in downloads
                            if str(item.get("status", "")).upper() == "COMPLETED"
                            and isinstance(item.get("downloadUrl"), str)
                        ),
                        None,
                    )
                    if completed is not None:
                        break
                    await asyncio.sleep(2)
                if completed is None:
                    raise RuntimeError("Browserbase native recording assembly timed out")
                media = await client.get(str(completed["downloadUrl"]), follow_redirects=True)
                media.raise_for_status()
        except httpx.HTTPError as error:
            response = getattr(error, "response", None)
            raise ProviderError("browserbase", getattr(response, "status_code", None), str(error)) from error
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(media.content)
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("Browserbase native recording download was empty")
        return {
            "provider": "browserbase",
            "session_id": session_id,
            "page_id": str(completed.get("pageId", "unknown")),
            "artifact": str(output),
        }

    async def download_session_replay_video(
        self, session_id: str, output: Path, *, timeout_seconds: int = 180
    ) -> dict[str, str]:
        """Materialize Browserbase's current HLS session replay as an MP4.

        Browserbase's supported recording surface is the Session Replay API:
        it exposes a per-tab VOD playlist whose signed fMP4 segments are the
        same source-faithful recording shown in Session Inspector.  Keep the
        API key server-side and let ffmpeg assemble the playlist locally; no
        CDP/screencast fallback is substituted here.
        """
        headers = {"x-bb-api-key": self.api_key}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                deadline = asyncio.get_running_loop().time() + max(10, timeout_seconds)
                pages: list[dict] = []
                while asyncio.get_running_loop().time() < deadline:
                    response = await client.get(
                        f"https://api.browserbase.com/v1/sessions/{session_id}/replays",
                        headers=headers,
                    )
                    if response.status_code == 200:
                        pages = response.json().get("pages", [])
                        if pages:
                            break
                    elif response.status_code not in {404, 409}:
                        response.raise_for_status()
                    await asyncio.sleep(2)
                if not pages:
                    raise RuntimeError("Browserbase session replay is not available")
                page = max(
                    pages,
                    key=lambda item: int(item.get("endTimeMs", 0)) - int(item.get("startTimeMs", 0)),
                )
                page_id = str(page.get("pageId", "0"))
                playlist_response = await client.get(
                    f"https://api.browserbase.com/v1/sessions/{session_id}/replays/{page_id}",
                    headers=headers,
                )
                playlist_response.raise_for_status()
                playlist = playlist_response.text
        except httpx.HTTPError as error:
            response = getattr(error, "response", None)
            raise ProviderError("browserbase", getattr(response, "status_code", None), str(error)) from error
        if "#EXTM3U" not in playlist or not any(line.startswith("https://") for line in playlist.splitlines()):
            raise RuntimeError("Browserbase replay returned an invalid HLS playlist")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", suffix=".m3u8", delete=False, encoding="utf-8") as handle:
            handle.write(playlist)
            playlist_path = Path(handle.name)
        try:
            # Replay assembly is provider work too: bound it independently of
            # the HTTP playlist polling so a corrupt/stalled signed segment
            # cannot strand a worker after the Browserbase session is closed.
            assembly_timeout = max(30, min(300, int(timeout_seconds)))
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        subprocess.run,
                        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file,http,https,tcp,tls,crypto", "-i", str(playlist_path), "-map", "0:v:0", "-an", "-c", "copy", str(output)],
                        check=True, capture_output=True, text=True,
                        timeout=assembly_timeout,
                    ),
                    timeout=assembly_timeout + 5,
                )
            except (TimeoutError, subprocess.TimeoutExpired) as error:
                raise RuntimeError(f"Browserbase replay assembly timed out after {assembly_timeout}s") from error
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeError(f"Browserbase replay assembly failed: {error}") from error
        finally:
            playlist_path.unlink(missing_ok=True)
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("Browserbase replay assembly produced an empty video")
        return {"provider": "browserbase", "session_id": session_id, "page_id": page_id, "artifact": str(output)}
