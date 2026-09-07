from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import time
from datetime import timedelta
from pathlib import Path

from productlens.artifacts.store import RunArtifacts
from productlens.contracts.models import (
    DemoTrace,
    EditorialStoryboard,
    InteractionEvent,
    OperationKind,
    PresentationPlan,
)
from productlens.narration.audio import audio_duration_seconds
from productlens.presentation.title import concise_demo_title
from productlens.video.source_timing import align_trace_to_recording

DEFAULT_FRAME_RATE = 30


class RenderTimeoutError(RuntimeError):
    """Remotion did not complete before the configured bounded render window."""


class CaptureDurationError(RuntimeError):
    """Native-speed evidence cannot fit the approved final duration envelope."""


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
            index for index, value in enumerate(command)
            if index > 0 and str(value).lower().endswith(".mp4")
        )
        output = Path(command[output_index])
        frames_value = command[command.index("--frames") + 1]
        first, last = (int(value) for value in str(frames_value).split("-", maxsplit=1))
        expected_seconds = (last - first + 1) / DEFAULT_FRAME_RATE
        if not output.is_file() or output.stat().st_size < 10_000:
            return False
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(output)],
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
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, check=False,
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
        raise RuntimeError(
            "REMOTION_SEGMENT_RENDER_FAILED: " + diagnostics.strip()[-1_000:]
        )


def _render_concurrency() -> int:
    """Use a conservative compositor fan-out for long browser walkthroughs."""
    try:
        requested = int(os.getenv("PRODUCTLENS_REMOTION_CONCURRENCY", "1"))
    except ValueError:
        requested = 1
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
        requested = int(os.getenv("PRODUCTLENS_REMOTION_CHUNK_FRAMES", "3600"))
    except ValueError:
        requested = 3600
    # At 30fps the default is a two-minute segment. This keeps long demo
    # renders resumable while avoiding repeated Chromium setup for every short
    # scene block in an otherwise healthy walkthrough. The environment can
    # lower this for constrained workers without changing presentation data.
    return min(7_200, max(150, requested))


class NarrationTimingError(RuntimeError):
    """Audio cannot fit the proven browser evidence without distorting it."""


def _prepare_remotion_source(raw: Path, public: Path, run_id: str) -> str:
    """Materialize browser evidence in the codec Remotion renders reliably.

    Browserbase/Playwright recordings are frequently VP8 WebM. Chromium can
    play those files interactively, yet long `OffthreadVideo` renders can
    degrade to the browser's loading canvas on some Remotion/Windows builds.
    The production source remains immutable under ``execution/``; this creates
    a run-scoped H.264 render input only.  It is therefore a compatibility
    conversion, never a timing, resolution, or frame-rate transformation.
    """
    source_asset = f"{run_id}.mp4"
    destination = public / source_asset
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw),
            "-map", "0:v:0", "-an", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(destination),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return source_asset


def _frame_rate(value: object) -> float:
    """Read ffprobe's rational frame-rate format defensively."""
    try:
        numerator, denominator = str(value or "0/1").split("/", maxsplit=1)
        return float(numerator) / max(1.0, float(denominator))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _recording_space_rect(rect, *, viewport, source_width: int, source_height: int):
    """Translate browser CSS geometry into the native recording pixel space.

    Browserbase can record at 1920x1080 while the page's CSS viewport is
    1440x900. Passing CSS coordinates straight to Remotion points the cursor
    and camera at empty space on the right/bottom of the video. Geometry is
    semantic only in its captured viewport; normalize it before composition.
    """
    if rect is None or viewport is None:
        return rect
    scale_x = source_width / max(1, viewport.width)
    scale_y = source_height / max(1, viewport.height)
    return rect.model_copy(update={
        "x": rect.x * scale_x, "y": rect.y * scale_y,
        "width": rect.width * scale_x, "height": rect.height * scale_y,
    })


def _recording_space_cursor_paths(presentation: PresentationPlan, trace: DemoTrace, *, source_width: int, source_height: int) -> list[dict]:
    """Return cursor paths in the same pixel space as the recorded video."""
    events = {event.id: event for event in trace.events if event.success}
    converted: list[dict] = []
    for path in presentation.cursor_paths:
        event = events.get(str(path.get("event_id", "")))
        viewport = event.viewport if event else None
        if viewport is None:
            converted.append(path)
            continue
        scale_x = source_width / max(1, viewport.width)
        scale_y = source_height / max(1, viewport.height)
        def point(value: object, *, x_scale: float = scale_x, y_scale: float = scale_y) -> dict:
            raw = value if isinstance(value, dict) else {}
            return {"x": float(raw.get("x", 0)) * x_scale, "y": float(raw.get("y", 0)) * y_scale}
        converted.append({
            **path,
            "source": point(path.get("source")),
            "destination": point(path.get("destination")),
            "waypoints": [point(item) for item in path.get("waypoints", []) if isinstance(item, dict)],
        })
    return converted


def _editorial_cut_windows(
    trace: DemoTrace, source_seconds: float, *, minimum_seconds: float = 0.0,
) -> list[tuple[float, float]]:
    """Keep native action/reveal evidence while removing remote idle gaps.

    This is a cut list, never a playback-rate change. Long remote CDP calls
    often leave the browser visually settled while ProductLens waits for a
    follow-up DOM query. Retain the action onset, its completed visible result,
    and a readable post-result dwell; omit only the gap between those proven
    moments when it exceeds the editorial budget.
    """
    if trace.recording_started_at is None:
        return [(0.0, source_seconds)]
    successful = [event for event in trace.events if event.success and event.action_at is not None]
    # Keep the generous single-event envelope for focused demos and backward
    # compatibility. Long multi-scene tours use compact, evidence-backed
    # tails so repeated landmarks do not inflate the final duration.
    compact_tour = len(successful) > 1
    # Browserbase begins its provider-owned recording before ProductLens has
    # navigated and stabilised the requested page.  Never treat source second
    # zero as the opening proof: establish the opening immediately before the
    # first deliberate gesture, where the trace knows the requested page was
    # actually visible and readable.
    first_action = min(
        (
            max(0.0, min(source_seconds, (event.action_at - trace.recording_started_at).total_seconds()))
            for event in successful
        ),
        default=0.0,
    )
    opening_end = max(0.0, first_action - 0.45)
    opening_start = max(0.0, opening_end - 5.0)
    windows: list[tuple[float, float]] = (
        [(opening_start, opening_end)] if opening_end - opening_start >= 1.0 else [(0.0, min(5.0, source_seconds))]
    )
    for event in trace.events:
        if not event.success or event.action_at is None:
            continue
        action = max(0.0, min(source_seconds, (event.action_at - trace.recording_started_at).total_seconds()))
        reveal = max(action, min(source_seconds, (event.occurred_at - trace.recording_started_at).total_seconds()))
        # A directed scroll is itself viewer-facing evidence.  Even if a cloud
        # compositor or CDP call reports a long elapsed interval, cutting its
        # middle turns a natural page movement into the exact teleport this
        # layer is meant to prevent. Keep the full motion at native speed.
        # A normal native interaction also stays continuous. For a remote
        # latency gap, preserve both causal edges at normal speed and cut
        # between them.
        # Keep the complete motion itself, but retain only a short, readable
        # settle after the target is visible.  The old 2.9s tail on every
        # landmark accumulated into 6+ minute portfolio renders even though
        # the trace contained no additional evidence during those tails.
        # This is an evidence-backed cut, never a speed change: scroll frames
        # and the action/reveal edges remain at native cadence.
        if event.kind is OperationKind.SCROLL_TO:
            # ``occurred_at`` includes remote DOM verification round trips.
            # For cloud scrolls the browser records the actual compositor
            # duration in ``after.scroll_motion``; keep that physical motion
            # plus a short settle, rather than carrying several seconds of
            # invisible CDP latency into the final edit. Older traces without
            # this evidence retain the conservative action→reveal window.
            motion = event.after.get("scroll_motion") if isinstance(event.after, dict) else None
            motion_ms = motion.get("duration_ms") if isinstance(motion, dict) else None
            if isinstance(motion_ms, (int, float)) and motion_ms > 0:
                motion_end = min(source_seconds, action + float(motion_ms) / 1000.0 + 0.55)
                windows.append((
                    max(0.0, action - (0.1 if compact_tour else 0.6)),
                    max(action + 0.2, motion_end),
                ))
            else:
                windows.append((
                    max(0.0, action - (0.1 if compact_tour else 0.6)),
                    min(source_seconds, reveal + (0.35 if compact_tour else 2.9)),
                ))
        elif reveal - action <= 3.8:
            windows.append((
                max(0.0, action - (0.1 if compact_tour else 0.6)),
                min(source_seconds, reveal + (0.35 if compact_tour else 2.9)),
            ))
        else:
            # Preserve a visibly complete navigation dispatch and immediate
            # transition before cutting the remote wait; this is the causal
            # edge viewers need to understand the page change.
            windows.append((
                max(0.0, action - (0.1 if compact_tour else 0.6)),
                min(source_seconds, action + (1.9 if not compact_tour else 0.3)),
            ))
            windows.append((
                max(0.0, reveal - (0.3 if compact_tour else 1.9)),
                min(source_seconds, reveal + (0.35 if compact_tour else 2.9)),
            ))
    windows.sort()
    merged: list[tuple[float, float]] = []
    for start, end in windows:
        if end - start < 0.12:
            continue
        # Small gaps are encoder/clock jitter around one continuous gesture;
        # joining them avoids a visible micro-teleport at the opening edge.
        if merged and start <= merged[-1][1] + 0.4:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    merged = merged or [(0.0, source_seconds)]
    # A remote browser can spend substantial wall time waiting for a command
    # response after the visible page has already settled. Those gaps may be
    # editorially compressible, but a full walkthrough must still retain
    # enough *real, native-speed* context to be a coherent 2–3 minute story.
    # Expand every proven scene window symmetrically before admitting a short
    # route-sweep render; this preserves causal context around each action
    # rather than inventing duration with frozen footage or playback changes.
    desired = min(source_seconds, max(0.0, minimum_seconds))
    for _ in range(6):
        current = sum(end - start for start, end in merged)
        if current + 0.05 >= desired:
            break
        available = [
            (index, start, end)
            for index, (start, end) in enumerate(merged)
            if start > 0.01 or end < source_seconds - 0.01
        ]
        if not available:
            break
        padding = (desired - current) / (2 * len(available))
        expanded = [
            (
                max(0.0, start - (padding if any(index == item[0] for item in available) else 0.0)),
                min(source_seconds, end + (padding if any(index == item[0] for item in available) else 0.0)),
            )
            for index, (start, end) in enumerate(merged)
        ]
        expanded.sort()
        merged = []
        for start, end in expanded:
            if merged and start <= merged[-1][1] + 0.08:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
    return merged


def _remap_trace_for_cuts(trace: DemoTrace, windows: list[tuple[float, float]]) -> DemoTrace:
    """Map trace clocks into a concatenated native-speed editorial source."""
    if trace.recording_started_at is None:
        return trace

    def remap(seconds: float) -> float:
        elapsed = 0.0
        for start, end in windows:
            if seconds <= start:
                return elapsed
            if seconds < end:
                return elapsed + seconds - start
            elapsed += end - start
        return elapsed

    events = []
    for event in trace.events:
        action = event.action_at or event.occurred_at
        action_seconds = (action - trace.recording_started_at).total_seconds()
        occurred_seconds = (event.occurred_at - trace.recording_started_at).total_seconds()
        events.append(event.model_copy(update={
            "action_at": trace.recording_started_at + timedelta(seconds=remap(action_seconds)),
            "occurred_at": trace.recording_started_at + timedelta(seconds=remap(occurred_seconds)),
        }))
    return trace.model_copy(update={"events": events})


def _build_editorial_source(raw: Path, *, render_dir: Path, windows: list[tuple[float, float]]) -> Path:
    """Concatenate deliberate cuts without altering the speed of retained footage."""
    # A timing repair can change the retained source windows while keeping the
    # same run id. Ordinal names such as ``000.mp4`` are therefore unsafe as a
    # cache key: they can silently compose a new caption/trace plan over old
    # footage. Bind every reusable clip set to both the exact raw recording
    # fingerprint and the exact native-speed cut list.
    raw_stat = raw.stat()
    cut_key = hashlib.sha256(json.dumps(
        {
            "raw": str(raw.resolve()),
            "size": raw_stat.st_size,
            "mtime_ns": raw_stat.st_mtime_ns,
            "windows": [[round(start, 3), round(end, 3)] for start, end in windows],
        },
        sort_keys=True,
    ).encode("utf-8")).hexdigest()[:16]
    clips_dir = render_dir / "editorial-clips" / cut_key
    clips_dir.mkdir(exist_ok=True)
    clips: list[Path] = []
    for index, (start, end) in enumerate(windows):
        clip = clips_dir / f"{index:03d}.mp4"
        if not clip.exists() or clip.stat().st_size < 10_000:
            subprocess.run([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}",
                "-t", f"{end - start:.3f}", "-i", str(raw), "-map", "0:v:0", "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(clip),
            ], check=True, capture_output=True, text=True)
        clips.append(clip)
    listing = clips_dir / "clips.ffconcat"
    listing.write_text("ffconcat version 1.0\n" + "".join(
        f"file '{clip.resolve().as_posix()}'\n" for clip in clips
    ), encoding="utf-8")
    output = render_dir / f"editorial-source-{cut_key}.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
    ], check=True, capture_output=True, text=True)
    return output


def _evidence_timed_beat_ranges(
    trace: DemoTrace, *, screen_frames: int, source_seconds: float, frame_rate: int
) -> list[tuple[int, int, int]] | None:
    """Map verified event timestamps to the slowed evidence recording.

    Older traces lack a recorder clock, and synthetic tests commonly assign the
    same timestamp to every event.  Those inputs deliberately retain the stable
    evenly-spaced fallback rather than pretending their timing is meaningful.
    """
    if trace.recording_started_at is None or len(trace.events) < 2 or source_seconds <= 0:
        return None
    source_positions = [
        max(0.0, min(source_seconds, ((event.action_at or event.occurred_at) - trace.recording_started_at).total_seconds()))
        for event in trace.events
    ]
    if max(source_positions) - min(source_positions) < 0.5:
        return None
    frame_scale = screen_frames / source_seconds
    lead_frames = max(1, round(min(0.65, source_seconds / 12) * frame_scale))
    starts = [max(0, min(screen_frames - 1, round(position * frame_scale) - lead_frames)) for position in source_positions]
    # Timestamp order is the execution order, but clamp defensively so malformed
    # imported evidence cannot create backwards camera moves.
    starts = [max(starts[index], max(starts[:index], default=0)) for index in range(len(starts))]
    minimum_hold = max(frame_rate, round(2.4 * frame_rate))
    ranges: list[tuple[int, int, int]] = []
    for index, start in enumerate(starts):
        next_start = starts[index + 1] if index + 1 < len(starts) else screen_frames
        end = max(start + 1, next_start)
        if index + 1 == len(starts):
            end = max(end, min(screen_frames, start + minimum_hold))
        click_frame = max(start, min(screen_frames - 1, round(source_positions[index] * frame_scale)))
        ranges.append((start, min(screen_frames, end), click_frame))
    return ranges


def _evidence_timed_captions(
    trace: DemoTrace, captions: list[dict], *, screen_seconds: float
) -> list[dict] | None:
    """Place each caption over the verified event it describes.

    Caption scripts deliberately retain the event id.  Using that id here is
    important: distributing lines evenly across a variable-length recording
    creates a plausible-looking but false narration whenever a route load or
    a gradual scroll takes longer than another action.
    """
    if trace.recording_started_at is None or not captions or screen_seconds <= 0:
        return None
    events = {event.id: event for event in trace.events if event.success}
    ordered = [events.get(str(item.get("scene_id", ""))) for item in captions]
    if any(event is None for event in ordered):
        return None
    # Captions explain the *visible result*, not the mouse-down.  In
    # particular a navigation action can be dispatched several seconds before
    # the destination finishes its transition. Cursor/click beats still use
    # ``action_at`` elsewhere; scene copy starts at ``occurred_at`` only after
    # the trace has recorded the readable postcondition.
    raw_positions = [
        max(0.0, (event.occurred_at - trace.recording_started_at).total_seconds())
        for event in ordered
        if event is not None
    ]
    raw_action_positions = [
        max(0.0, ((event.action_at or event.occurred_at) - trace.recording_started_at).total_seconds())
        for event in ordered
        if event is not None
    ]
    # Recorder clocks and browser-operation clocks can drift apart in local
    # fixtures and after a browser pauses capture during an expensive action.
    # Clamping each event individually would collapse every late event onto the
    # final frame, yielding overlapping/out-of-timeline captions.  Preserve
    # their verified order by fitting the *whole* evidence timeline into the
    # recorded presentation window.  Normal capture has a scale of 1.0.
    evidence_span = max(raw_positions, default=0.0)
    scale = screen_seconds / evidence_span if evidence_span > screen_seconds and evidence_span else 1.0
    positions = [min(screen_seconds, position * scale) for position in raw_positions]
    action_positions = [min(screen_seconds, position * scale) for position in raw_action_positions]
    # Multiple semantic events can intentionally share a state (for example,
    # a final scroll landmark followed by its verified takeaway). Give each
    # approved line a small sequential reading slot instead of emitting
    # overlapping captions at the same source timestamp. This is timeline
    # layout only; it never changes capture, scene order, or evidence IDs.
    #
    # ``occurred_at`` is intentionally recorded as the verified result, but
    # cloud traces can contain an asynchronous DOM round trip after the
    # visible scroll has already finished. Starting *every* caption there
    # squeezed the available post-result window to a sub-second flash. For a
    # scroll or form, the viewer can see the result during the verified action
    # interval, so the caption begins after its physical reveal and remains
    # until the next action. Navigation is different: its destination must
    # still wait for the verified route/result state.
    minimum_dwell = min(1.45, screen_seconds / max(1, len(captions)))
    navigation_kinds = {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}

    def visible_start(index: int, event: InteractionEvent) -> float:
        action = action_positions[index]
        result = positions[index]
        if event.kind in navigation_kinds:
            return max(0.0, result - 0.1)
        if event.kind is OperationKind.SCROLL_TO:
            motion = event.after.get("scroll_motion") if isinstance(event.after, dict) else None
            duration_ms = motion.get("duration_ms") if isinstance(motion, dict) else None
            if isinstance(duration_ms, (int, float)) and duration_ms > 0:
                # Apply the same trace-to-screen scale as the event position.
                return min(result, action + float(duration_ms) / 1000.0 * scale + 0.12)
        # A typed/focused control is visible as soon as its action has begun;
        # keep a short lead so the overlay never covers the gesture itself.
        return min(result, action + 0.22)

    starts: list[float] = []
    for index, (caption, event) in enumerate(zip(captions, ordered, strict=True)):
        assert event is not None
        # The first line is the presenter's welcome and establishes the very
        # first stable product frame.  It must begin with that frame, not only
        # after the initial establish/scroll event has completed; otherwise a
        # viewer sees an unexplained silent opening and then hears the welcome
        # after the story has already started.  Later lines still wait for the
        # verified visible result they describe.
        # Captions are already ordered and validated against the storyboard;
        # the first line is the opening beat regardless of the provider's
        # wording.  Detecting a literal greeting here delayed model-authored
        # introductions until after the first browser action, creating an
        # unexplained silent opening.
        is_presenter_opening = index == 0 and (
            bool(caption.get("opening", False))
            or str(caption.get("text", "")).lstrip().lower().startswith("welcome to ")
        )
        desired_start = 0.0 if is_presenter_opening else visible_start(index, event)
        latest_start = max(0.0, screen_seconds - minimum_dwell * (len(captions) - index))
        start = min(desired_start, latest_start)
        if starts:
            start = max(start, starts[-1] + minimum_dwell)
        starts.append(min(screen_seconds - minimum_dwell, start))
    timed: list[dict] = []
    for index, (caption, start) in enumerate(zip(captions, starts, strict=True)):
        next_start = starts[index + 1] if index + 1 < len(starts) else screen_seconds
        # A new action begins the transition away from this scene. Holding the
        # old scene's copy until the next action's *settled* result produces a
        # visibly wrong caption over the incoming route. End before dispatch;
        # the intentional transition gap is preferable to false narration.
        next_action = action_positions[index + 1] if index + 1 < len(action_positions) else screen_seconds
        # Preserve a readable, local dwell without bleeding into the following
        # verified state.  One-event traces simply retain the entire evidence.
        # Beat ranges begin 0.65 seconds before the following event. End the
        # outgoing line inside that range so the incoming caption never
        # narrates a state that is already on screen.
        if index == 0 and bool(caption.get("opening", False)):
            # A welcome should establish the product, not monopolise a long
            # opening hold. Keep enough time for a calm read while allowing
            # the first page-local explanation to start promptly.
            reading_seconds = len(str(caption.get("text", "")).split()) / 2.8 + 0.7
            end = min(next_action - 0.2, max(start + minimum_dwell, min(7.5, reading_seconds)))
        else:
            end = max(start + minimum_dwell, min(next_start - 0.1, next_action - 0.2))
        end = min(screen_seconds, end)
        if end <= start:
            start = max(0.0, min(start, screen_seconds - 0.1))
            end = screen_seconds
        timed.append({**caption, "start": round(start, 2), "end": round(end, 2)})
    return timed


def render_remotion(
    trace: DemoTrace,
    presentation: PresentationPlan,
    artifacts: RunArtifacts,
    *,
    narration_path: Path | None = None,
    captions: list[dict] | None = None,
    scenes: list[dict] | None = None,
    target_duration_seconds: int | None = None,
    maximum_duration_seconds: int | None = None,
    storyboard: EditorialStoryboard | None = None,
) -> Path:
    """Render a final MP4 from evidence video plus trace-derived camera decisions."""
    raw = artifacts.browser_video_path()
    if not raw.exists():
        raise FileNotFoundError("Execution evidence video is required before rendering")
    renderer = Path(__file__).resolve().parents[3] / "video" / "remotion"
    # Render outside the delivery location.  A worker may be interrupted while
    # Chromium is encoding, and a partially written or stale file must never be
    # mistaken for the successful artifact of the current attempt.
    output = (artifacts.root / "final" / "demo.mp4").resolve()
    render_dir = artifacts.root / "render"
    render_dir.mkdir(exist_ok=True)
    candidate_output = (render_dir / "demo.candidate.mp4").resolve()
    public = renderer / "public"
    public.mkdir(exist_ok=True)
    # The trace can be inherited by a targeted retry. Its historical
    # ``run_id`` is useful for lineage, but media assets must be namespaced to
    # the current artifact directory; otherwise a retry's props point at a
    # non-existent parent-run asset in Remotion's public directory.
    current_run_id = artifacts.root.name
    # A Browserbase native MP4 is visually richer than a CDP screencast, but
    # it can have provider-owned PTS that differ from Playwright wall time.
    # Align it to the timestamped CDP image witnesses before any editor or
    # presentation component uses the trace as a video clock.
    timing_trace, timing_report = align_trace_to_recording(
        trace,
        recording=raw,
        screencast_frames=artifacts.execution / "cloud-screencast-frames",
    )
    artifacts.write_json("execution/source-timing-alignment.json", timing_report)
    if timing_report.get("status") == "rejected":
        raise CaptureDurationError("native recording could not be aligned to browser-event evidence")
    if timing_report.get("status") == "aligned":
        trace = timing_trace
    narration_asset: str | None = None
    if narration_path is not None:
        if not narration_path.exists() or narration_path.stat().st_size == 0:
            raise FileNotFoundError("Narration was requested but no audio asset was created")
        narration_asset = f"{current_run_id}{narration_path.suffix or '.mp3'}"
        shutil.copy2(narration_path, public / narration_asset)
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=width,height,avg_frame_rate", "-of", "json", str(raw),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    source_probe = json.loads(probe.stdout)
    source_seconds = float(source_probe["format"]["duration"])
    if timing_report.get("status") == "unavailable" and trace.recording_started_at and trace.events:
        trace_span = max(
            (event.occurred_at - trace.recording_started_at).total_seconds()
            for event in trace.events
        )
        # Local Playwright captures normally share the trace clock. A large
        # mismatch is characteristic of a Browserbase native recording whose
        # provider PTS was not accompanied by CDP timing evidence. Refuse the
        # render: fabricated alignment is worse than a recoverable execution
        # retry, especially for captions and cursor proof.
        if trace_span > 1 and source_seconds > trace_span * 1.35 + 3:
            raise CaptureDurationError(
                "native recording duration does not match trace time and lacks source timing evidence"
            )
    # Product footage is deliberately never globally sped up or frozen to
    # satisfy a duration request.  Rendering adds a 1.5s title and 1s close,
    # so reject an overlong native capture before spending a long compositor
    # job on an MP4 that delivery QA must reject anyway.  The owning repair is
    # a smaller/stronger validated ScenePlan followed by clean re-execution.
    presentation_chrome_seconds = 2.5
    render_source = raw
    # Full walkthrough acceptance is 2–3 minutes. Preserve at least two
    # minutes of real scene evidence when the approved target is 180 seconds;
    # product footage is never slowed or frozen merely to reach that floor.
    duration_floor = 0.0
    if storyboard is not None:
        duration_floor = max(0.0, float(storyboard.minimum_duration_seconds) - presentation_chrome_seconds)
    if target_duration_seconds is not None and target_duration_seconds >= 180:
        duration_floor = max(duration_floor, 120.0 - presentation_chrome_seconds)
    windows = _editorial_cut_windows(trace, source_seconds, minimum_seconds=duration_floor)
    removes_unestablished_prelude = bool(windows and windows[0][0] > 0.1)
    needs_duration_edit = (
        maximum_duration_seconds is not None
        and source_seconds + presentation_chrome_seconds > maximum_duration_seconds + 0.25
    )
    # Even a short capture can include Browserbase/local-context setup before
    # the page is stable. Apply the same evidence-backed cut list whenever it
    # removes that prelude; duration pressure is not the only reason an edit
    # is editorially necessary.
    if needs_duration_edit or removes_unestablished_prelude:
        editorial_seconds = sum(end - start for start, end in windows)
        artifacts.write_json(
            "presentation/source-edit-plan.json",
            {
                "mode": "native_speed_cuts",
                "source_seconds": round(source_seconds, 3),
                "edited_seconds": round(editorial_seconds, 3),
                "windows": [{"start": round(start, 3), "end": round(end, 3)} for start, end in windows],
                "reason": "remove proven remote transport/dead intervals; retained footage is never accelerated",
            },
        )
        if (
            maximum_duration_seconds is not None
            and editorial_seconds + presentation_chrome_seconds > maximum_duration_seconds + 0.25
        ):
            raise CaptureDurationError(
                "native capture exceeds the approved final duration envelope even after native-speed editorial cuts"
            )
        render_source = _build_editorial_source(raw, render_dir=render_dir, windows=windows)
        source_edit_path = artifacts.presentation / "source-edit-plan.json"
        try:
            source_edit_payload = json.loads(source_edit_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            source_edit_payload = {}
        artifacts.write_json(
            "presentation/source-edit-plan.json",
            {
                **source_edit_payload,
                "rendered_source": str(render_source.relative_to(artifacts.root.resolve())),
            },
        )
        trace = _remap_trace_for_cuts(trace, windows)
        artifacts.write_json("presentation/render-trace.json", trace.model_dump(mode="json"))
        source_seconds = editorial_seconds
    source_asset = _prepare_remotion_source(render_source, public, current_run_id)
    # The public asset name is stable per run, but its bytes can change after
    # a timing/source-edit repair. Include that content identity in Remotion
    # props so durable frame segments cannot be reused against a replaced
    # source file with the same URL.
    source_sha256 = hashlib.sha256(render_source.read_bytes()).hexdigest()
    source_stream = next(
        (stream for stream in source_probe.get("streams", []) if stream.get("width") and stream.get("height")),
        {},
    )
    source_width, source_height = int(source_stream.get("width", 1920)), int(source_stream.get("height", 1080))
    source_frame_rate = _frame_rate(source_stream.get("avg_frame_rate"))
    # Do not synthesize smoothness by interpolating a 30fps browser recording.
    # When the browser evidence genuinely supports 60fps, preserve it through
    # the composition; otherwise retain its native 30fps delivery cadence.
    frame_rate = 60 if source_frame_rate >= 55 else DEFAULT_FRAME_RATE
    # A walkthrough must be built from enough real interaction footage.  Do
    # not turn a 14 second trace into a 75 second slideshow: it makes page
    # motion feel broken and divorces captions from the event the viewer sees.
    # Recording holds, gradual scrolls, and typed input create readable source
    # evidence; the final render keeps that evidence at native speed.
    target_screen_seconds = source_seconds
    # Narration and screen evidence share the same composition interval. A
    # correctly authored, event-aligned track fits the real capture. Never
    # slow, freeze, or silently cut browser evidence merely to fit speech.
    if narration_path is not None:
        audio_seconds = audio_duration_seconds(narration_path)
        if audio_seconds > source_seconds + 0.15:
            raise NarrationTimingError(
                "measured narration exceeds captured evidence; repair scene script or capture timing"
            )
    playback_rate = source_seconds / target_screen_seconds
    screen_frames = max(frame_rate * 3, math.ceil(target_screen_seconds * frame_rate))
    # Caption-only scripts are placed on their evidence event, not spread
    # evenly across the video. The latter was the cause of caption/story drift
    # in long route transitions and gradual scrolls.
    scaled_captions = captions or []
    if scaled_captions and narration_asset is None:
        screen_seconds = screen_frames / frame_rate
        evidence_captions = _evidence_timed_captions(
            trace, scaled_captions, screen_seconds=screen_seconds
        )
        if evidence_captions is not None:
            scaled_captions = evidence_captions
        else:
            source_span = max(float(item.get("end", 0)) for item in scaled_captions)
            if source_span > 0:
                scaled_captions = [
                    {
                        **item,
                        "start": round(float(item.get("start", 0)) / source_span * screen_seconds, 2),
                        "end": round(float(item.get("end", 0)) / source_span * screen_seconds, 2),
                    }
                    for item in scaled_captions
                ]
    # QA and delivery need the exact timeline passed to Remotion, not the
    # pre-slowdown narration timeline.
    artifacts.write_json("presentation/rendered-captions.json", scaled_captions)
    beats = []
    decisions = {item.event_id: item for item in presentation.camera}
    rendered_cursor_paths = _recording_space_cursor_paths(
        presentation, trace, source_width=source_width, source_height=source_height
    )
    timed_ranges = _evidence_timed_beat_ranges(
        trace, screen_frames=screen_frames, source_seconds=source_seconds, frame_rate=frame_rate
    )
    for index, event in enumerate(trace.events):
        decision = decisions.get(event.id)
        raw_rect = decision.focus if decision else event.target_rect
        rect = _recording_space_rect(
            raw_rect, viewport=event.viewport, source_width=source_width, source_height=source_height
        )
        if timed_ranges is None:
            start = round(index * screen_frames / max(1, len(trace.events)))
            end = round((index + 1) * screen_frames / max(1, len(trace.events)))
            click_frame = min(end - 1, start + min(18, max(1, end - start)))
        else:
            start, end, click_frame = timed_ranges[index]
        beats.append(
            {
                "intent": event.intent,
                "eventId": event.id,
                "kind": event.kind,
                "start": start,
                "end": max(start + 1, end),
                "clickFrame": click_frame,
                "x": rect.x if rect else 960,
                "y": rect.y if rect else 540,
                "width": rect.width if rect else 1,
                "height": rect.height if rect else 1,
                "zoom": decision.zoom if decision else 1.0,
            }
        )
    # Cursor beats begin before dispatch and transition as the next action is
    # prepared. Captions describe the readable *result*, so a navigation
    # caption may correctly begin after its click beat.  The stable scene/event
    # id joins the two layers; clamping caption timing to a cursor-only range
    # would narrate the old page or yield an invalid negative-duration line.
    artifacts.write_json("presentation/rendered-captions.json", scaled_captions)
    props = {
        "title": storyboard.brief.title if storyboard else concise_demo_title(
            trace.objective, action_labels=[event.intent for event in trace.events if event.success]
        ),
        # Keep the title card aligned with the approved editorial brief.  A
        # generic subtitle made unrelated products look like the same demo and
        # duplicated no evidence from the actual opening page.
        "subtitle": (
            " ".join(str(storyboard.brief.opening_message).split())[:180]
            if storyboard is not None and storyboard.brief.opening_message.strip()
            else "A verified workflow, captured in real interaction time."
        ),
        "targetDurationSeconds": target_duration_seconds,
        "screenVideo": source_asset,
        "screenVideoSha256": source_sha256,
        "sourceWidth": source_width,
        "sourceHeight": source_height,
        "screenFrames": screen_frames,
        "frameRate": frame_rate,
        "sourceFrameRate": round(source_frame_rate, 3),
        "playbackRate": playback_rate,
        "beats": beats,
        "cursorPaths": rendered_cursor_paths,
        "eventViewports": {
            event.id: event.viewport.model_dump(mode="json")
            for event in trace.events if event.success and event.viewport is not None
        },
        "narration": narration_asset,
        "captions": scaled_captions,
        "scenes": scenes or [],
    }
    props_path = (artifacts.presentation / "remotion-props.json").resolve()
    props_path.write_text(json.dumps(props), encoding="utf-8")
    props_sha256 = hashlib.sha256(props_path.read_bytes()).hexdigest()
    # Remotion writes the candidate atomically only after ffmpeg has completed.
    # A worker can be interrupted after the compositor has finished but before
    # this Python process promotes the output. Reuse that run-scoped candidate
    # on a resumed render instead of re-encoding minutes of identical evidence.
    if candidate_output.exists() and candidate_output.stat().st_size >= 10_000:
        candidate_output.replace(output)
        return output
    # Use the project-pinned CLI directly. ``npx`` can block before the
    # compositor is even started (for example while resolving its launcher),
    # leaving a durable render job falsely RUNNING with no browser process.
    local_remotion = renderer / "node_modules" / ".bin" / (
        "remotion.cmd" if os.name == "nt" else "remotion"
    )
    remotion_cli = str(local_remotion) if local_remotion.exists() else (
        "npx.cmd" if os.name == "nt" else "npx"
    )
    remotion_args = [] if local_remotion.exists() else ["remotion"]
    command_prefix = [
            remotion_cli,
            *remotion_args,
            "render",
            "src/index.ts",
            "ProductLensDemo",
            str(candidate_output),
            "--props",
            str(props_path),
            "--concurrency",
            str(_render_concurrency()),
            "--timeout",
            str(_remotion_setup_timeout_ms()),
            "--hardware-acceleration",
            _remotion_hardware_acceleration(),
            # PNG intermediate frames dominate long browser-demo renders on
            # Windows workers. High-quality JPEG intermediates are visually
            # transparent for an already raster browser source while avoiding
            # a multi-minute lossless-frame encode bottleneck.
            "--image-format",
            "jpeg",
            "--jpeg-quality",
            "95",
            # Browser UI needs a constant-quality encode; bitrate remains a
            # ceiling/target because static product frames compress efficiently
            # and should not fail merely for being simple.
            "--crf",
            # CRF 20 retains sharp UI text at 1080p while materially reducing
            # the encode cost of a two-to-three minute browser walkthrough.
            # Resolution, source frame cadence, and scene dwell are never
            # traded away to make a render faster.
            "20",
            "--log=error",
        ]
    # Long browser walkthroughs are rendered in durable frame ranges. A worker
    # crash or deployment interruption then resumes from completed segments
    # instead of discarding hundreds of compositor seconds. The ranges retain
    # their original absolute frame numbers, so captions/cursor/camera timing
    # stays identical to a one-shot render.
    # Remotion executes from ``video/remotion``. Segment output paths must be
    # absolute just like the candidate path, otherwise a provider-free retry
    # with a relative artifact root writes files under the renderer project and
    # ProductLens subsequently reports a completed-but-missing segment.
    # Segment files are only reusable for the exact presentation props that
    # produced them. A caption, camera, or source-edit repair must never
    # silently concatenate an older completed frame range merely because its
    # frame numbers happen to match the new render.
    segments_dir = (render_dir / "segments" / props_sha256[:16]).resolve()
    segments_dir.mkdir(parents=True, exist_ok=True)
    chunk_frames = _render_chunk_frames()
    segment_outputs: list[Path] = []
    manifest: list[dict[str, object]] = []
    for start in range(0, screen_frames, chunk_frames):
        end = min(screen_frames - 1, start + chunk_frames - 1)
        segment = segments_dir / f"{start:07d}-{end:07d}.mp4"
        # Retain the historic candidate path for a short one-segment render;
        # this also keeps the final promotion atomic. Long renders use durable
        # segment files and therefore resume independently.
        segment_output = candidate_output if screen_frames <= chunk_frames else segment
        segment_complete = _segment_is_complete(
            segment_output, expected_frames=end - start + 1, frame_rate=frame_rate
        )
        if not segment_complete:
            command = [
                str(segment_output) if value == str(candidate_output) else value
                for value in command_prefix
            ]
            command.extend(["--frames", f"{start}-{end}"])
            _run_remotion_segment(
                command,
                renderer=renderer,
                timeout_seconds=_render_timeout_seconds(),
            )
        if not segment_output.exists() or segment_output.stat().st_size == 0:
            raise RuntimeError(f"Remotion completed without segment {start}-{end}")
        segment_outputs.append(segment_output)
        manifest.append({
            "start_frame": start,
            "end_frame": end,
            "path": str(segment_output.relative_to(artifacts.root.resolve())),
        })
        artifacts.write_json(
            "render/segment-manifest.json",
            {
                "frame_rate": frame_rate,
                "props_sha256": props_sha256,
                "chunk_frames": chunk_frames,
                "segments": manifest,
            },
        )
    if len(segment_outputs) == 1 and segment_outputs[0] == candidate_output:
        candidate_output.replace(output)
        return output
    concat = render_dir / "segments.ffconcat"
    concat.write_text(
        "ffconcat version 1.0\n" + "".join(
            f"file '{segment.resolve().as_posix().replace(chr(39), chr(92) + chr(39))}'\n"
            for segment in segment_outputs
        ),
        encoding="utf-8",
    )
    concat_result = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
         # Stream-copy concat retains independently encoded segment PTS and
         # can introduce a visible cadence gap at every boundary. A final
         # local CFR pass preserves all rendered frames while rebuilding one
         # continuous presentation timeline for delivery QA.
         "-vf", f"fps={frame_rate}", "-c:v", "libx264", "-preset", "veryfast",
         # CRF alone can encode a mostly static UI far below a legible delivery
         # bitrate.  Keep quality ownership in the renderer and guarantee a
         # sane 1080p delivery floor without altering timing or browser pixels.
         "-b:v", "2M", "-minrate", "2M", "-maxrate", "2M", "-bufsize", "4M",
         "-x264-params", "nal-hrd=cbr",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(candidate_output)],
        capture_output=True, text=True, check=False,
    )
    if concat_result.returncode != 0 or not candidate_output.exists() or candidate_output.stat().st_size < 10_000:
        raise RuntimeError(f"RENDER_SEGMENT_CONCAT_FAILED: {concat_result.stderr[-500:]}")
    if not candidate_output.exists() or candidate_output.stat().st_size == 0:
        raise RuntimeError("Remotion completed without a candidate video artifact")
    candidate_output.replace(output)
    return output
