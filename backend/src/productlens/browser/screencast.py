"""ProductLens-owned CDP screencast capture for remote browser sessions.

Browserbase records sessions for its inspector, but that recording is not an
artifact a renderer can reliably download.  This recorder captures the same
verified cloud session through CDP and turns it into a local video artifact,
so cloud production uses the identical trace → presentation → render path as
local production rather than a dashboard-only replay.
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError


class CdpScreencastRecorder:
    def __init__(self, page: Any, output_directory: Path, *, frame_rate: int = 30):
        self.page = page
        self.output_directory = output_directory
        self.frame_rate = frame_rate
        self._session: Any | None = None
        self._frame_count = 0
        self._frame_timestamps: list[tuple[int, float]] = []
        # CDP timestamps describe the remote compositor; receipt timestamps
        # anchor those frames to the ProductLens trace clock.  Browserbase's
        # downloadable MP4 can have a different presentation timebase, so
        # preserving this correspondence is mandatory for later alignment.
        self._frame_receipts: list[dict[str, object]] = []
        self._write_tasks: set[asyncio.Task[None]] = set()
        self._capture_stopped = False
        self._last_transport_error: str | None = None

    async def start(self) -> None:
        self.output_directory.mkdir(parents=True, exist_ok=True)
        self._session = await self.page.context.new_cdp_session(self.page)
        self._session.on("Page.screencastFrame", self._on_frame)
        await self._session.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 88, "everyNthFrame": 1},
        )

    def _on_frame(self, payload: dict[str, Any]) -> None:
        if self._session is None:
            return
        index = self._frame_count
        self._frame_count += 1
        encoded = payload.get("data")
        session_id = payload.get("sessionId")
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        timestamp = metadata.get("timestamp")
        if not isinstance(encoded, str) or session_id is None:
            return
        # CDP only emits a screencast frame when the remote compositor has a
        # frame to send.  Frame count is therefore not wall-clock time.
        # Retaining the CDP timestamp prevents a 5-minute cloud recording from
        # becoming an 80-second fast-forward when FFmpeg later encodes it.
        self._frame_timestamps.append(
            (index, float(timestamp) if isinstance(timestamp, (int, float)) else 0.0)
        )
        self._frame_receipts.append(
            {
                "index": index,
                "cdp_timestamp": float(timestamp) if isinstance(timestamp, (int, float)) else None,
                "received_at": datetime.now(UTC).isoformat(),
            }
        )
        # CDP frame acknowledgement is flow control, not persistence. Waiting
        # for a filesystem write before acknowledging a Browserbase frame can
        # exhaust the remote screencast buffer and terminate the page mid-run.
        # Acknowledge immediately, then write the independent JPEG payload.
        ack = asyncio.create_task(self._ack_frame(session_id))
        self._write_tasks.add(ack)
        ack.add_done_callback(self._write_tasks.discard)
        task = asyncio.create_task(self._persist_frame(index, encoded))
        self._write_tasks.add(task)
        task.add_done_callback(self._write_tasks.discard)

    async def _ack_frame(self, session_id: int) -> None:
        if self._session is None:
            return
        try:
            await self._session.send("Page.screencastFrameAck", {"sessionId": session_id})
        except PlaywrightError as error:
            # Browserbase may close a session after the final frame. The image
            # payload is still valid evidence; never leave an unobserved task
            # exception that hides the actual execution outcome.
            self._last_transport_error = str(error)

    async def _persist_frame(self, index: int, encoded: str) -> None:
        data = base64.b64decode(encoded)
        await asyncio.to_thread(
            (self.output_directory / f"frame-{index:07d}.jpg").write_bytes, data
        )

    async def stop_capture(self) -> None:
        """Stop remote CDP delivery and flush frame writes, without encoding.

        Encoding thousands of HD JPEGs is local CPU work. It must never keep a
        Browserbase session open after the browser evidence is already safely
        on disk.
        """
        if self._capture_stopped:
            return
        if self._session is not None:
            try:
                try:
                    await self._session.send("Page.stopScreencast")
                except PlaywrightError as error:
                    # A provider-side close can race the final stop command.
                    self._last_transport_error = str(error)
            finally:
                try:
                    await self._session.detach()
                except PlaywrightError as error:
                    # The remote browser may have already closed after a
                    # provider timeout; retain any captured frames for QA.
                    self._last_transport_error = str(error)
                self._session = None
        if self._write_tasks:
            await asyncio.gather(*self._write_tasks)
        if self._frame_count < 2:
            raise RuntimeError("CLOUD_SCREENCAST_INSUFFICIENT_FRAMES")
        self._capture_stopped = True
        (self.output_directory / "timing.json").write_text(
            json.dumps({"frame_rate": self.frame_rate, "frames": self._frame_receipts}),
            encoding="utf-8",
        )

    async def encode(self, output: Path) -> Path:
        """Encode locally persisted frames after the cloud session is closed."""
        if not self._capture_stopped:
            raise RuntimeError("CLOUD_SCREENCAST_CAPTURE_NOT_STOPPED")
        output.parent.mkdir(parents=True, exist_ok=True)
        concat = self.output_directory / "frames.ffconcat"
        timestamps = sorted(self._frame_timestamps)
        lines = ["ffconcat version 1.0"]
        for position, (index, timestamp) in enumerate(timestamps):
            path = (
                (self.output_directory / f"frame-{index:07d}.jpg")
                .resolve()
                .as_posix()
                .replace("'", "\\'")
            )
            lines.append(f"file '{path}'")
            if position + 1 < len(timestamps):
                next_timestamp = timestamps[position + 1][1]
                duration = (
                    next_timestamp - timestamp
                    if timestamp and next_timestamp
                    else 1 / self.frame_rate
                )
                # Preserve real pacing but defend against malformed metadata
                # and pathological tab-suspension gaps.
                lines.append(f"duration {min(2.0, max(1 / 60, duration)):.6f}")
        # ffconcat needs the final file a second time for its previous duration.
        final_index = timestamps[-1][0]
        final_path = (
            (self.output_directory / f"frame-{final_index:07d}.jpg")
            .resolve()
            .as_posix()
            .replace("'", "\\'")
        )
        lines.append(f"file '{final_path}'")
        concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat),
                "-fps_mode",
                "vfr",
                "-c:v",
                "libvpx-vp9",
                "-deadline",
                "realtime",
                "-cpu-used",
                "8",
                "-row-mt",
                "1",
                "-pix_fmt",
                "yuv420p",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError(f"CLOUD_SCREENCAST_ENCODE_FAILED: {result.stderr[-500:]}")
        return output

    async def stop(self, output: Path) -> Path:
        """Backward-compatible one-shot stop for callers without a cloud session."""
        await self.stop_capture()
        return await self.encode(output)
