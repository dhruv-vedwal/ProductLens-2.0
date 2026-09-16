"""Render status errors, timeouts, and remotion process helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from datetime import timedelta
from pathlib import Path

from app.artifacts.store import RunArtifacts
from app.contracts.models import (
    DemoTrace,
    EditorialStoryboard,
    InteractionEvent,
    OperationKind,
    PresentationPlan,
)
from app.narration.audio import audio_duration_seconds
from app.presentation.title import concise_demo_title
from app.video.source_timing import align_trace_to_recording

DEFAULT_FRAME_RATE = 30


class RenderTimeoutError(RuntimeError):
    """Remotion did not complete before the configured bounded render window."""


class CaptureDurationError(RuntimeError):
    """Native-speed evidence cannot fit the approved final duration envelope."""


class RecordingProvenanceError(RuntimeError):
    """The media file does not belong to the run whose trace is being rendered."""


def _validate_recording_provenance(trace: DemoTrace, artifacts: RunArtifacts, raw: Path) -> None:
    """Fail closed when provider metadata points at another run's recording.

    A copied Browserbase metadata file used to be enough for a render to pair a
    fresh trace with an old recording.  That produces a plausible MP4 while
    silently lying about every click and caption.  Provider metadata is
    optional for local fixture captures, but when present it must identify this
    run and the exact file selected by :class:`RunArtifacts`.
    """
    metadata_path = artifacts.execution / "browserbase-recording.json"
    if not metadata_path.is_file():
        return
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise RecordingProvenanceError("invalid Browserbase recording metadata") from error
    if not isinstance(payload, dict):
        raise RecordingProvenanceError("invalid Browserbase recording metadata")
    if payload.get("native_recording") in {"unavailable", "failed"}:
        return
    provider = str(payload.get("provider", "")).casefold()
    if provider != "browserbase":
        return
    metadata_run = payload.get("run_id")
    if metadata_run is not None and str(metadata_run) != str(trace.run_id):
        raise RecordingProvenanceError(
            f"recording metadata belongs to run {metadata_run}, expected {trace.run_id}"
        )
    expected = raw.resolve()
    artifact_ref = payload.get("artifact")
    if artifact_ref:
        try:
            recorded = Path(str(artifact_ref)).resolve()
        except (OSError, ValueError) as error:
            raise RecordingProvenanceError("invalid recording artifact reference") from error
        if recorded != expected:
            raise RecordingProvenanceError(
                "recording metadata artifact does not match this run's source video"
            )
    recorded_sha = payload.get("sha256")
    if recorded_sha:
        digest = hashlib.sha256(raw.read_bytes()).hexdigest()
        if str(recorded_sha) != digest:
            raise RecordingProvenanceError("recording bytes changed after provider capture")


def _completed_segment_after_timeout(command: list[str]) -> bool:
    """Accept a fully muxed segment when Remotion's launcher outlives encoding.

    On Windows, the Remotion Node launcher can retain its process after FFmpeg
    has already written and closed the requested MP4.  Treating that as a
    failed render discards valid 1080p evidence and forces an expensive replay.
    This intentionally checks both the requested frame span and media probe;
    a partial or merely-created file is never accepted.
    """
    try:
        output_index = next(
            index
            for index, value in enumerate(command)
            if index > 0 and str(value).lower().endswith(".mp4")
        )
        output = Path(command[output_index])
        frames_value = command[command.index("--frames") + 1]
        first, last = (int(value) for value in str(frames_value).split("-", maxsplit=1))
        expected_seconds = (last - first + 1) / DEFAULT_FRAME_RATE
        if not output.is_file() or output.stat().st_size < 10_000:
            return False
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        duration = float(json.loads(probe.stdout).get("format", {}).get("duration", 0))
        return probe.returncode == 0 and duration >= max(0.5, expected_seconds - 0.35)
    except (IndexError, StopIteration, ValueError, json.JSONDecodeError, OSError):
        return False


def _segment_is_complete(path: Path, *, expected_frames: int, frame_rate: int) -> bool:
    """Recognize a fully muxed segment after a post-encode bookkeeping crash."""
    try:
        if not path.is_file() or path.stat().st_size < 10_000:
            return False
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        duration = float(json.loads(probe.stdout).get("format", {}).get("duration", 0))
        return probe.returncode == 0 and duration >= max(0.5, expected_frames / frame_rate - 0.35)
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _run_remotion_segment(command: list[str], *, renderer: Path, timeout_seconds: int) -> None:
    """Render a segment and absorb one known cold-Chrome startup failure.

    Remotion occasionally times out while its first local Chromium process is
    opening on a busy Windows worker. That condition happens before any frame
    is rendered and is safe to retry once. A render, composition, or encode
    failure is never retried blindly: it is surfaced with a bounded diagnostic
    so the repair layer can own it.
    """
    for attempt in range(2):
        try:
            result = subprocess.run(
                command,
                cwd=renderer,
                check=False,
                timeout=timeout_seconds,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as error:
            if _completed_segment_after_timeout(command):
                return
            raise RenderTimeoutError(
                f"Remotion segment timed out after {timeout_seconds} seconds"
            ) from error
        # Lightweight test/dry-run probes historically expose stdout only;
        # subprocess itself always provides returncode. Treat that compatible
        # shape as success while retaining strict handling in production.
        if getattr(result, "returncode", 0) == 0:
            return
        diagnostics = f"{result.stdout}\n{result.stderr}"
        cold_browser_timeout = "trying to connect to the browser" in diagnostics.lower()
        if cold_browser_timeout and attempt == 0:
            time.sleep(2)
            continue
        raise RuntimeError("REMOTION_SEGMENT_RENDER_FAILED: " + diagnostics.strip()[-1_000:])


def _render_concurrency() -> int:
    """Use a safe but practical compositor fan-out for browser walkthroughs.

    A single Chromium compositor serialises every 1080p frame and makes an
    otherwise healthy, evidence-backed demo take hours on a normal developer
    workstation.  Two workers keep memory pressure bounded while allowing
    decode and JPEG/encode work to overlap.  Deployments which are genuinely
    constrained can still explicitly select one worker through the provider
    configuration.
    """
    try:
        requested = int(os.getenv("PRODUCTLENS_REMOTION_CONCURRENCY", "2"))
    except ValueError:
        requested = 2
    # High parallelism regularly destabilises long 1080p browser captures on
    # constrained workers. This controls only parallel compositor pages; it
    # never reduces frame rate, resolution, source duration, or video quality.
    return min(4, max(1, requested))


def _render_timeout_seconds() -> int:
    """Bound an unhealthy compositor without constraining healthy long demos."""
    try:
        requested = int(os.getenv("PRODUCTLENS_REMOTION_TIMEOUT_SECONDS", "1800"))
    except ValueError:
        requested = 1800
    return min(7_200, max(120, requested))


def _remotion_setup_timeout_ms() -> int:
    """Allow a cold local renderer to load a large evidence-backed composition."""
    try:
        requested = int(os.getenv("PRODUCTLENS_REMOTION_SETUP_TIMEOUT_MS", "120000"))
    except ValueError:
        requested = 120000
    return min(300_000, max(30_000, requested))


def _remotion_hardware_acceleration() -> str:
    """Select hardware encoding opportunistically without requiring it."""
    value = os.getenv("PRODUCTLENS_REMOTION_HARDWARE_ACCELERATION", "if-possible").strip().lower()
    return value if value in {"disable", "if-possible", "required"} else "if-possible"


def _render_chunk_frames() -> int:
    """Bound one resumable compositor segment for long product demos."""
    try:
        # A 1080p browser frame is intentionally rendered at native cadence,
        # but CPU-only workers can take longer than the stage timeout for a
        # two-minute monolithic Chromium segment.  Thirty-second chunks keep
        # each unit resumable and below the timeout while the concat pass
        # preserves one continuous CFR timeline and every source frame.
        requested = int(os.getenv("PRODUCTLENS_REMOTION_CHUNK_FRAMES", "900"))
    except ValueError:
        requested = 3600
    # At 30fps the default is a thirty-second segment. This keeps long demo
    # renders resumable on CPU workers without changing presentation data.
    return min(7_200, max(150, requested))


class NarrationTimingError(RuntimeError):
    """Audio cannot fit the proven browser evidence without distorting it."""

__all__ = [
    "DEFAULT_FRAME_RATE",
    "RenderTimeoutError",
    "CaptureDurationError",
    "RecordingProvenanceError",
    "_validate_recording_provenance",
    "_completed_segment_after_timeout",
    "_segment_is_complete",
    "_run_remotion_segment",
    "_render_concurrency",
    "_render_timeout_seconds",
    "_remotion_setup_timeout_ms",
    "_remotion_hardware_acceleration",
    "_render_chunk_frames",
    "NarrationTimingError",
]
