from __future__ import annotations

import asyncio
import subprocess
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import httpx

from productlens.providers.errors import ProviderError
from productlens.providers.limits import provider_limit


@dataclass(frozen=True)
class BrowserbaseSessionInfo:
    session_id: str
    connect_url: str
    stagehand_extension_id: str | None = None


class BrowserbaseProvider:
    """Creates remote browser sessions; Playwright remains the execution owner."""

    def __init__(
        self,
        api_key: str,
        project_id: str | None = None,
        *,
        session_timeout_seconds: int = 1800,
        stagehand_extension_path: Path | None = None,
    ):
        self.api_key = api_key
        self.project_id = project_id
        self.session_timeout_seconds = max(60, min(1800, int(session_timeout_seconds)))
        self.stagehand_extension_path = stagehand_extension_path or (
            Path(__file__).resolve().parents[3]
            / "stagehand/node_modules/@browserbasehq/stagehand/dist/assets/stagehand-extension.zip"
        )
        self._stagehand_extensions: dict[str, str] = {}
        self._request_limit = asyncio.Semaphore(provider_limit("browserbase"))

    def _bounded_deadline(self, requested_seconds: int, *, minimum: int = 10) -> int:
        """Keep provider waits within the isolated session's configured lease."""
        return max(minimum, min(self.session_timeout_seconds, int(requested_seconds)))

    async def create_session(self) -> str:
        return (await self.create_session_info()).session_id

    async def create_session_info(
        self,
        *,
        viewport: dict[str, int] | None = None,
        user_metadata: dict[str, object] | None = None,
        region: str | None = None,
        keep_alive: bool | None = None,
        proxies: bool | list[dict[str, object]] | None = None,
    ) -> BrowserbaseSessionInfo:
        async with self._request_limit:
            return await self._create_session_info_unbounded(
                viewport=viewport,
                user_metadata=user_metadata,
                region=region,
                keep_alive=keep_alive,
                proxies=proxies,
            )

    async def _create_session_info_unbounded(
        self,
        *,
        viewport: dict[str, int] | None = None,
        user_metadata: dict[str, object] | None = None,
        region: str | None = None,
        keep_alive: bool | None = None,
        proxies: bool | list[dict[str, object]] | None = None,
    ) -> BrowserbaseSessionInfo:
        """Create an isolated session with the recording contract up front.

        Browserbase applies viewport and browser settings when the session is
        provisioned.  Setting the viewport later through CDP can change DOM
        layout but cannot reliably change the native Session Replay geometry,
        which is the source used for production rendering.  Keep all options
        optional for compatibility, while allowing production to pass its
        already-selected viewport and non-secret run metadata.
        """
        extension_id: str | None = None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                extension_id = await self._upload_stagehand_extension(client)
                response = await client.post(
                    "https://api.browserbase.com/v1/sessions",
                    headers={"x-bb-api-key": self.api_key, "Content-Type": "application/json"},
                    # Native Browserbase recording is the production visual
                    # source. CDP screencasts are retained only as a compact
                    # diagnostic fallback because remote compositor frames are
                    # sparse during real reading holds.
                    json={
                        **({"projectId": self.project_id} if self.project_id else {}),
                        **({"extensionId": extension_id} if extension_id else {}),
                        "timeout": self.session_timeout_seconds,
                        **({"region": region} if region else {}),
                        **({"keepAlive": keep_alive} if keep_alive is not None else {}),
                        **({"proxies": proxies} if proxies is not None else {}),
                        **({"userMetadata": user_metadata} if user_metadata else {}),
                        # Developer-plan Identity includes automatic CAPTCHA
                        # solving. Keep it explicit so a project-level setting
                        # cannot silently disable the capability required by a
                        # protected target. Proxies remain opt-in because they
                        # consume separately metered bandwidth.
                        "browserSettings": {
                            "recordSession": True,
                            "solveCaptchas": True,
                            **({"viewport": viewport} if viewport else {}),
                        },
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError as error:
            if extension_id:
                await self._delete_stagehand_extension(extension_id)
            response = getattr(error, "response", None)
            raise ProviderError(
                "browserbase", getattr(response, "status_code", None), str(error)
            ) from error
        payload = response.json()
        connect_url = payload.get("connectUrl") or payload.get("connect_url")
        if not payload.get("id") or not connect_url:
            raise RuntimeError("Browserbase session response did not include id and connectUrl")
        if extension_id:
            self._stagehand_extensions[str(payload["id"])] = extension_id
        return BrowserbaseSessionInfo(
            session_id=payload["id"],
            connect_url=connect_url,
            stagehand_extension_id=extension_id,
        )

    async def _upload_stagehand_extension(self, client: httpx.AsyncClient) -> str | None:
        """Provision the official Stagehand extension for this session.

        Stagehand's own Browserbase launcher performs this upload before
        creating a session. ProductLens creates the session first so
        Playwright can remain the execution owner, therefore the provider must
        perform the same documented extension lifecycle explicitly.
        """
        archive = self.stagehand_extension_path
        if not archive.is_file():
            return None
        try:
            with archive.open("rb") as handle:
                response = await client.post(
                    "https://api.browserbase.com/v1/extensions",
                    headers={"x-bb-api-key": self.api_key},
                    files={"file": (archive.name, handle, "application/zip")},
                )
                response.raise_for_status()
            extension_id = response.json().get("id")
            return str(extension_id).strip() if extension_id else None
        except (OSError, httpx.HTTPError, ValueError, TypeError):
            # Stagehand is advisory; a provider outage must not discard the
            # Playwright-grounded discovery. The persisted Stagehand artifact
            # will classify the unavailable advisory path for QA.
            return None

    async def _delete_stagehand_extension(self, extension_id: str) -> None:
        with suppress(Exception):
            async with httpx.AsyncClient(timeout=30) as client:
                await client.delete(
                    f"https://api.browserbase.com/v1/extensions/{extension_id}",
                    headers={"x-bb-api-key": self.api_key},
                )

    async def close_session(self, session_id: str) -> None:
        """Release a remote session after CDP disconnect, including failed discovery runs."""
        # DNS and transient transport loss must not turn an otherwise complete
        # exploration into a failed run on the first close attempt. Browserbase
        # releases sessions through the Sessions API update endpoint (rather
        # than DELETE); the request is idempotent and does not repeat any
        # browser action or user-side effect.
        last_error: httpx.HTTPError | None = None
        released = False
        try:
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(timeout=30) as client:
                        response = await client.post(
                            f"https://api.browserbase.com/v1/sessions/{session_id}",
                            headers={
                                "x-bb-api-key": self.api_key,
                                "Content-Type": "application/json",
                            },
                            json={
                                "status": "REQUEST_RELEASE",
                                **({"projectId": self.project_id} if self.project_id else {}),
                            },
                        )
                        # Session termination is idempotent: Stagehand or a
                        # timed-out browser process may already have released
                        # it before ProductLens performs final cleanup.
                        if response.status_code == 404:
                            released = True
                            break
                        response.raise_for_status()
                        released = True
                        break
                except httpx.HTTPError as error:
                    last_error = error
                    if attempt < 2:
                        await asyncio.sleep(1 + attempt)
            if not released:
                response = getattr(last_error, "response", None)
                raise ProviderError(
                    "browserbase", getattr(response, "status_code", None), str(last_error)
                ) from last_error
        finally:
            # Extension cleanup is best effort: it must not turn a released
            # browser into a failed run, and leaked extension IDs are not
            # execution evidence.
            extension_id = self._stagehand_extensions.pop(session_id, None)
            if extension_id:
                await self._delete_stagehand_extension(extension_id)

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
                attempts = max(1, self._bounded_deadline(timeout_seconds) // 2)
                completed: dict | None = None
                for _ in range(attempts):
                    response = await client.get(
                        f"https://api.browserbase.com/v1/sessions/{session_id}/recording/downloads",
                        headers=headers,
                    )
                    response.raise_for_status()
                    downloads = response.json().get("downloads", [])
                    failed = [
                        item
                        for item in downloads
                        if str(item.get("status", "")).upper() == "FAILED"
                    ]
                    if failed:
                        raise RuntimeError("Browserbase native recording assembly failed")
                    completed = next(
                        (
                            item
                            for item in downloads
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
            raise ProviderError(
                "browserbase", getattr(response, "status_code", None), str(error)
            ) from error
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
                replay_timeout = self._bounded_deadline(timeout_seconds)
                deadline = asyncio.get_running_loop().time() + replay_timeout
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
                    key=lambda item: (
                        int(item.get("endTimeMs", 0)) - int(item.get("startTimeMs", 0))
                    ),
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
            raise ProviderError(
                "browserbase", getattr(response, "status_code", None), str(error)
            ) from error
        if "#EXTM3U" not in playlist or not any(
            line.startswith("https://") for line in playlist.splitlines()
        ):
            raise RuntimeError("Browserbase replay returned an invalid HLS playlist")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".m3u8", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(playlist)
            playlist_path = Path(handle.name)
        try:
            # Replay assembly is provider work too: bound it independently of
            # the HTTP playlist polling so a corrupt/stalled signed segment
            # cannot strand a worker after the Browserbase session is closed.
            # Respect the caller's bounded replay deadline.  The previous
            # 300-second ceiling was unrelated to the configured Browserbase
            # session timeout and caused long, valid walkthroughs to fail
            # during local HLS assembly even after the remote recording had
            # completed.  Callers still provide the bound; this provider does
            # not invent a shorter product-duration limit.
            assembly_timeout = self._bounded_deadline(timeout_seconds, minimum=30)
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        subprocess.run,
                        [
                            "ffmpeg",
                            "-y",
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-protocol_whitelist",
                            "file,http,https,tcp,tls,crypto",
                            "-i",
                            str(playlist_path),
                            "-map",
                            "0:v:0",
                            "-an",
                            "-c",
                            "copy",
                            str(output),
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=assembly_timeout,
                    ),
                    timeout=assembly_timeout + 5,
                )
            except (TimeoutError, subprocess.TimeoutExpired) as error:
                raise RuntimeError(
                    f"Browserbase replay assembly timed out after {assembly_timeout}s"
                ) from error
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeError(f"Browserbase replay assembly failed: {error}") from error
        finally:
            playlist_path.unlink(missing_ok=True)
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("Browserbase replay assembly produced an empty video")
        return {
            "provider": "browserbase",
            "session_id": session_id,
            "page_id": page_id,
            "artifact": str(output),
        }
