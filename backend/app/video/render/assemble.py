"""FFmpeg/Remotion assemble and render promotion."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.artifacts.store import RunArtifacts
from app.contracts.models import (
    DemoTrace,
    EditorialStoryboard,
    PresentationPlan,
)
from app.narration.audio import audio_duration_seconds
from app.presentation.title import concise_demo_title
from app.video.render.remotion_props import *
from app.video.render.status import *
from app.video.source_timing import align_trace_to_recording

# API contracts use Pythonic snake_case while the Remotion composition is a
# JavaScript boundary. Keep that translation explicit and allow-list the
# presentation surface so arbitrary request keys can never leak into render
# props. This also makes retries deterministic across API/worker versions.
_PRESENTATION_PROP_MAP = {
    "include_audio": "includeAudio",
    "narration_style": "narrationStyle",
    "pace": "pace",
    "browser_zoom_percent": "browserZoomPercent",
    "subtitles_enabled": "subtitlesEnabled",
    "subtitle_style": "subtitleStyle",
    "subtitle_position": "subtitlePosition",
    "subtitle_font_size": "subtitleFontSize",
    "intro_template": "introTemplate",
    "studio_polish": "studioPolish",
    "cursor_style": "cursorStyle",
    "highlight_style": "highlightStyle",
    "click_zoom": "clickZoom",
    "export_aspect": "exportAspect",
    "export_resolution": "exportResolution",
    "language": "language",
    "accent": "accent",
}


def normalize_remotion_presentation_options(
    options: Mapping[str, object] | None,
) -> dict[str, object]:
    """Convert persisted API presentation options to the Remotion contract."""
    if not options:
        return {}
    return {
        remotion_key: options[api_key]
        for api_key, remotion_key in _PRESENTATION_PROP_MAP.items()
        if api_key in options
    }


def render_remotion(
    trace: DemoTrace,
    presentation: PresentationPlan,
    artifacts: RunArtifacts,
    *,
    narration_path: Path | None = None,
    captions: list[dict[str, Any]] | None = None,
    scenes: list[dict[str, Any]] | None = None,
    target_duration_seconds: int | None = None,
    maximum_duration_seconds: int | None = None,
    storyboard: EditorialStoryboard | None = None,
    presentation_options: dict[str, object] | None = None,
) -> Path:
    """Render a final MP4 from evidence video plus trace-derived camera decisions."""
    raw = artifacts.browser_video_path()
    if not raw.exists():
        raise FileNotFoundError("Execution evidence video is required before rendering")
    _validate_recording_provenance(trace, artifacts, raw)
    renderer = Path(__file__).resolve().parents[3] / "video" / "remotion"
    # Render outside the delivery location.  A worker may be interrupted while
    # Chromium is encoding, and a partially written or stale file must never be
    # mistaken for the successful artifact of the current attempt.
    output = (artifacts.root / "final" / "demo.mp4").resolve()
    render_dir = artifacts.root / "render"
    render_dir.mkdir(exist_ok=True)
    candidate_output = (render_dir / "demo.candidate.mp4").resolve()
    # Keep render inputs run-scoped.  The shared Remotion ``public`` folder
    # accumulated hundreds of old browser captures, forcing every render to
    # copy hundreds of megabytes before the first frame and making long demos
    # appear stalled.  ``--public-dir`` below points Remotion at this minimal
    # input set instead; no historical artifact participates in a render.
    public = (render_dir / "public").resolve()
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
        raise CaptureDurationError(
            "native recording could not be aligned to browser-event evidence"
        )
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
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=width,height,avg_frame_rate",
            "-of",
            "json",
            str(raw),
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
        duration_floor = max(
            0.0, float(storyboard.minimum_duration_seconds) - presentation_chrome_seconds
        )
    if target_duration_seconds is not None and target_duration_seconds >= 180:
        duration_floor = max(duration_floor, 120.0 - presentation_chrome_seconds)
    caption_reading_holds = {
        str(item.get("scene_id", "")): max(
            2.4,
            len(str(item.get("text", "")).split()) / 3.2 + 0.25,
        )
        for item in (captions or [])
        if str(item.get("scene_id", "")).strip()
    }
    # A focused 1–2 minute demo must contain enough *real* footage for its
    # approved story.  The old editor only used the storyboard floor, which
    # could be shorter than the reader dwell implied by the approved script;
    # captions then fell back to an artificial timeline and the product felt
    # rushed.  Derive the floor from the requested envelope and the measured
    # caption holds instead of from a site-specific constant.  This expands
    # native source windows; it never freezes or slows browser footage.
    if target_duration_seconds is not None and target_duration_seconds >= 90:
        duration_floor = max(
            duration_floor,
            # ``screenFrames`` is the actual browser-footage duration in the
            # composition; title/close layers do not add hidden time to it.
            # Keep focused 1–2 minute requests at a true 60-second minimum,
            # while still deriving the value from the requested envelope.
            min(120.0, float(target_duration_seconds) * 0.5),
        )
    if caption_reading_holds:
        # Keep a small native margin for frame rounding and the title/close
        # composition chrome; equality at the floating-point boundary would
        # otherwise make evidence timing fall back to evenly spread captions.
        duration_floor = max(duration_floor, sum(caption_reading_holds.values()) + 1.0)
    sync_edl_path = artifacts.presentation / "sync-edl.json"
    sync_edl: dict[str, Any] = {}
    if sync_edl_path.is_file():
        try:
            loaded_edl = json.loads(sync_edl_path.read_text(encoding="utf-8"))
            if isinstance(loaded_edl, dict):
                sync_edl = loaded_edl
        except (OSError, ValueError, json.JSONDecodeError):
            sync_edl = {}
    edl_windows = sync_edl.get("source_windows")
    if (
        sync_edl.get("schema_version") == 2
        and sync_edl.get("authority") == "semantic_moments"
        and isinstance(edl_windows, list)
        and edl_windows
    ):
        windows = [
            (
                max(0.0, float(item["start"])),
                min(source_seconds, float(item["end"])),
            )
            for item in edl_windows
            if isinstance(item, dict)
            and float(item.get("end", 0)) > float(item.get("start", 0))
            and float(item.get("start", 0)) < source_seconds
        ]
        if not windows:
            raise CaptureDurationError("Sync EDL has no source window inside the recording")
    else:
        # Isolated renderer fixtures may predate the production EDL boundary.
        # URL delivery requires sync-edl.json, so this compatibility path can
        # never make an uncontracted live run deliverable.
        windows = _editorial_cut_windows(
            trace,
            source_seconds,
            minimum_seconds=duration_floor,
            reading_holds_seconds=caption_reading_holds,
        )
    removes_unestablished_prelude = bool(windows and windows[0][0] > 0.1)
    editorial_window_seconds = sum(end - start for start, end in windows)
    # Provider/browser command latency can create long, content-free gaps in
    # an otherwise short capture. Editing only when the maximum duration is
    # exceeded left those gaps in final videos, where they read as lag. The
    # cut list is already evidence-backed and retains each native action,
    # transition, scroll path, reveal, and reading dwell, so apply it whenever
    # it removes a material proven-dead interval—not merely under duration
    # pressure.
    # Very short sources are already governed by their explicit trace/caption
    # timing. Avoid an additional container rewrite for sub-30-second clips;
    # the material lag this guard repairs occurs in longer cloud recordings,
    # where provider command gaps would otherwise be plainly visible.
    removes_proven_dead_time = (
        source_seconds >= 30.0 and editorial_window_seconds + 0.75 < source_seconds
    )
    needs_duration_edit = (
        maximum_duration_seconds is not None
        and source_seconds + presentation_chrome_seconds > maximum_duration_seconds + 0.25
    )
    # Even a short capture can include Browserbase/local-context setup before
    # the page is stable. Apply the same evidence-backed cut list whenever it
    # removes that prelude; duration pressure is not the only reason an edit
    # is editorially necessary.
    if needs_duration_edit or removes_unestablished_prelude or removes_proven_dead_time:
        editorial_seconds = editorial_window_seconds
        if (
            maximum_duration_seconds is not None
            and editorial_seconds + presentation_chrome_seconds > maximum_duration_seconds + 0.25
        ):
            raise CaptureDurationError(
                "native capture exceeds the approved final duration envelope even after native-speed editorial cuts"
            )
        render_source = _build_editorial_source(raw, render_dir=render_dir, windows=windows)
        artifacts.write_json(
            "presentation/sync-edl.json",
            {
                **sync_edl,
                "render": {
                    "mode": "native_speed_cuts",
                    "source_seconds": round(source_seconds, 3),
                    "edited_seconds": round(editorial_seconds, 3),
                    "source_windows": [
                        {"start": round(start, 3), "end": round(end, 3)}
                        for start, end in windows
                    ],
                    "rendered_source": str(
                        render_source.resolve().relative_to(artifacts.root.resolve())
                    ),
                },
            },
        )
        trace = _remap_trace_for_cuts(trace, windows)
        artifacts.write_json("presentation/render-trace.json", trace.model_dump(mode="json"))
        source_seconds = editorial_seconds
    # Caption-led delivery is intentionally silent.  Strip any incidental
    # microphone/audio track from a provider recording unless an approved TTS
    # asset is present; otherwise Remotion would leak unrelated source audio.
    source_asset = _prepare_remotion_source(
        render_source, public, current_run_id, strip_audio=narration_path is None
    )
    # The public asset name is stable per run, but its bytes can change after
    # a timing/source-edit repair. Include that content identity in Remotion
    # props so durable frame segments cannot be reused against a replaced
    # source file with the same URL.
    source_sha256 = hashlib.sha256(render_source.read_bytes()).hexdigest()
    source_stream: dict[str, Any] = next(
        (
            stream
            for stream in source_probe.get("streams", [])
            if stream.get("width") and stream.get("height")
        ),
        {},
    )
    source_width, source_height = (
        int(source_stream.get("width", 1920)),
        int(source_stream.get("height", 1080)),
    )
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
    options = presentation_options or {}
    remotion_presentation_options = normalize_remotion_presentation_options(options)
    def rendered_time(source_time: float) -> float:
        elapsed = 0.0
        for window_start, window_end in windows:
            if source_time <= window_start:
                return elapsed
            if source_time <= window_end:
                return elapsed + source_time - window_start
            elapsed += window_end - window_start
        return elapsed

    edl_caption_rows = []
    if (
        sync_edl.get("schema_version") == 2
        and sync_edl.get("authority") == "semantic_moments"
    ):
        for moment in sync_edl.get("moments", []):
            if not isinstance(moment, dict):
                continue
            tracks = moment.get("tracks")
            caption_track = tracks.get("caption") if isinstance(tracks, dict) else None
            if not isinstance(caption_track, dict) or not caption_track.get("text"):
                continue
            start = rendered_time(float(caption_track.get("start_seconds", 0)))
            end = rendered_time(float(caption_track.get("end_seconds", 0)))
            if end > start:
                edl_caption_rows.append(
                    {
                        "scene_id": moment.get("id"),
                        "moment_id": moment.get("id"),
                        "text": str(caption_track["text"]),
                        "start": round(start, 3),
                        "end": round(end, 3),
                        "evidence_refs": moment.get("evidence_refs", []),
                    }
                )
    scaled_captions = edl_caption_rows or captions or []
    if options.get("subtitles_enabled") is False:
        scaled_captions = []
    if scaled_captions and narration_asset is None and not edl_caption_rows:
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
                        "start": round(
                            float(item.get("start", 0)) / source_span * screen_seconds, 2
                        ),
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
            raw_rect,
            viewport=event.viewport,
            source_width=source_width,
            source_height=source_height,
        )
        if timed_ranges is None:
            start = round(index * screen_frames / max(1, len(trace.events)))
            end = round((index + 1) * screen_frames / max(1, len(trace.events)))
            click_frame = min(end - 1, start + min(18, max(1, end - start)))
        else:
            start, end, click_frame = timed_ranges[index]
        # Multiple verified events can share a source timestamp (especially
        # the final scroll and its takeaway).  Their native beat range may
        # therefore collapse to one frame even though the cursor witness is
        # a few frames later. Extend only that beat to include its recorded
        # witness; never move the cursor onto an invented target/time.
        if click_frame >= end:
            end = min(screen_frames, click_frame + 1)
        beats.append(
            {
                "intent": event.intent,
                "eventId": event.id,
                "kind": event.kind,
                "start": start,
                "end": max(start + 1, end),
                "clickFrame": click_frame,
                # Remotion consumes x/y as the visual action point. Rect
                # coordinates are top-left based, so use the target centre;
                # feeding the origin made fallback cursor paths and camera
                # focus consistently miss the actual click target.
                "x": rect.x + rect.width / 2 if rect else source_width / 2,
                "y": rect.y + rect.height / 2 if rect else source_height / 2,
                "width": rect.width if rect else 1,
                "height": rect.height if rect else 1,
                "zoom": _safe_camera_zoom(
                    rect,
                    decision.zoom if decision else 1.0,
                    source_width=source_width,
                    source_height=source_height,
                ),
            }
        )
    presentation_redactions = _presentation_secret_redactions(
        trace,
        beats,
        frame_rate=frame_rate,
    )
    artifacts.write_json(
        "presentation/secret-redactions.json",
        {
            "mode": "trace-geometry-mask",
            "count": len(presentation_redactions),
            "full_frame_fallback_count": sum(
                item.get("mode") == "secure-full-frame" for item in presentation_redactions
            ),
            "redactions": presentation_redactions,
            "source": "credential-like operation semantics and captured target geometry",
        },
    )
    # Cursor beats begin before dispatch and transition as the next action is
    # prepared. Captions describe the readable *result*, so a navigation
    # caption may correctly begin after its click beat.  The stable scene/event
    # id joins the two layers; clamping caption timing to a cursor-only range
    # would narrate the old page or yield an invalid negative-duration line.
    artifacts.write_json("presentation/rendered-captions.json", scaled_captions)
    outro_title, outro_subtitle = _outro_copy(trace, storyboard)
    props = {
        "title": storyboard.brief.title
        if storyboard
        else concise_demo_title(
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
        "outroTitle": outro_title,
        "outroSubtitle": outro_subtitle,
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
        "redactions": presentation_redactions,
        "cursorPaths": rendered_cursor_paths,
        "eventViewports": {
            event.id: event.viewport.model_dump(mode="json")
            for event in trace.events
            if event.success and event.viewport is not None
        },
        "narration": narration_asset,
        "captions": scaled_captions,
        "scenes": scenes or [],
        "syncEdl": sync_edl,
        # Keep frontend-selected presentation preferences alongside the
        # render contract.  The compositor can evolve these independently,
        # while retries/rerenders remain faithful to the original request.
        "presentationOptions": remotion_presentation_options,
    }
    props_path = (artifacts.presentation / "remotion-props.json").resolve()
    # Remotion props are part of the durable presentation contract. Write
    # them through the same atomic artifact writer as every other checkpoint;
    # a worker interruption must never leave a truncated JSON file that a
    # resumed render mistakes for a valid camera/caption plan.
    artifacts.write_json("presentation/remotion-props.json", props)
    props_sha256 = hashlib.sha256(props_path.read_bytes()).hexdigest()
    # Remotion writes the candidate atomically only after ffmpeg has completed.
    # A worker can be interrupted after the compositor has finished but before
    # this Python process promotes the output. Reuse that run-scoped candidate
    # on a resumed render instead of re-encoding minutes of identical evidence.
    if candidate_output.exists() and candidate_output.stat().st_size >= 10_000:
        _promote_render(candidate_output, output)
        return output
    # Use the project-pinned CLI directly. ``npx`` can block before the
    # compositor is even started (for example while resolving its launcher),
    # leaving a durable render job falsely RUNNING with no browser process.
    local_remotion = (
        renderer / "node_modules" / ".bin" / ("remotion.cmd" if os.name == "nt" else "remotion")
    )
    remotion_cli = (
        str(local_remotion)
        if local_remotion.exists()
        else ("npx.cmd" if os.name == "nt" else "npx")
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
        "--public-dir",
        str(public),
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
        # Browser demos contain large flat UI regions; a fast x264 preset
        # preserves CRF quality while avoiding a multi-minute CPU-bound
        # compression pass on every resumable segment.
        "--x264-preset",
        "veryfast",
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
        manifest.append(
            {
                "start_frame": start,
                "end_frame": end,
                "path": str(segment_output.relative_to(artifacts.root.resolve())),
            }
        )
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
        _promote_render(candidate_output, output)
        return output
    concat = render_dir / "segments.ffconcat"
    concat.write_text(
        "ffconcat version 1.0\n"
        + "".join(
            f"file '{segment.resolve().as_posix().replace(chr(39), chr(92) + chr(39))}'\n"
            for segment in segment_outputs
        ),
        encoding="utf-8",
    )
    try:
        concat_result = subprocess.run(
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
                # Stream-copy concat retains independently encoded segment PTS and
                # can introduce a visible cadence gap at every boundary. A final
                # local CFR pass preserves all rendered frames while rebuilding one
                # continuous presentation timeline for delivery QA.
                "-vf",
                f"fps={frame_rate}",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                # CRF alone can encode a mostly static UI far below a legible delivery
                # bitrate.  Keep quality ownership in the renderer and guarantee a
                # sane 1080p delivery floor without altering timing or browser pixels.
                "-b:v",
                "2M",
                "-minrate",
                "2M",
                "-maxrate",
                "2M",
                "-bufsize",
                "4M",
                "-x264-params",
                "nal-hrd=cbr",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(candidate_output),
            ],
            capture_output=True,
            text=True,
            check=False,
            # The final CFR pass is a separate process from the resumable
            # Remotion segments. Keep it bounded so a killed worker cannot
            # leave an orphan ffmpeg process and a permanently RUNNING stage.
            timeout=_render_timeout_seconds(),
        )
    except subprocess.TimeoutExpired as error:
        raise RenderTimeoutError(
            f"Final render concat timed out after {_render_timeout_seconds()} seconds"
        ) from error
    if (
        concat_result.returncode != 0
        or not candidate_output.exists()
        or candidate_output.stat().st_size < 10_000
    ):
        raise RuntimeError(f"RENDER_SEGMENT_CONCAT_FAILED: {concat_result.stderr[-500:]}")
    if not candidate_output.exists() or candidate_output.stat().st_size == 0:
        raise RuntimeError("Remotion completed without a candidate video artifact")
    _promote_render(candidate_output, output)
    return output


def _promote_render(candidate: Path, output: Path) -> None:
    """Promote a completed render without failing on a viewer-held MP4.

    Windows media players and antivirus/indexing processes can briefly hold
    the previous delivery open.  ``Path.replace`` is atomic but raises
    ``PermissionError`` in that situation, leaving a valid candidate stranded
    and the durable job incorrectly failed.  Keep the atomic path first; the
    copy fallback preserves the exact encoded bytes and is safe because the
    candidate was already fully written and validated by the renderer.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        candidate.replace(output)
        return
    except PermissionError:
        pass
    # A direct copy is intentionally the final fallback: it works when the
    # destination allows writes but refuses a rename over an open file.  The
    # caller only invokes this after a complete candidate exists.
    shutil.copyfile(candidate, output)
    candidate.unlink(missing_ok=True)

__all__ = [
    "_promote_render",
    "render_remotion",
]
