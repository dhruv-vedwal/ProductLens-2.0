"""Remotion source/props preparation and editorial timing windows."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

from app.contracts.models import (
    DemoTrace,
    EditorialStoryboard,
    InteractionEvent,
    OperationKind,
    PresentationPlan,
)

# Kept as a module-level compatibility hook for render tests and integrations
# that patch the shared audio-duration probe before invoking the assembler.
from app.narration.audio import audio_duration_seconds  # noqa: F401
from app.presentation.title import concise_demo_title
from app.video.render.status import *


def _prepare_remotion_source(
    raw: Path, public: Path, run_id: str, *, strip_audio: bool = False
) -> str:
    """Materialize browser evidence in the codec Remotion renders reliably.

    Browserbase/Playwright recordings are frequently VP8 WebM. Chromium can
    play those files interactively, yet long `OffthreadVideo` renders can
    degrade to the browser's loading canvas on some Remotion/Windows builds.
    The production source remains immutable under ``execution/``; this creates
    a run-scoped H.264 render input only.  It is therefore a compatibility
    conversion, never a timing, resolution, or frame-rate transformation.
    """
    source_asset = f"{run_id}.mp4"
    public.mkdir(parents=True, exist_ok=True)
    destination = public / source_asset
    # Editorial cuts are already emitted as H.264/yuv420p MP4. Re-encoding
    # them again before Remotion costs several minutes on a CPU worker and
    # can subtly soften UI text without adding compatibility.  Probe rather
    # than trusting an extension: Browserbase WebM and unusual MP4 codecs
    # still use the conservative conversion path below.
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,pix_fmt",
            "-of",
            "json",
            str(raw),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        stream = json.loads(probe.stdout).get("streams", [{}])[0]
    except (TypeError, ValueError, json.JSONDecodeError, IndexError):
        stream = {}
    audio_probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "json",
            str(raw),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        has_audio = bool(json.loads(audio_probe.stdout or "{}").get("streams"))
    except (TypeError, ValueError, json.JSONDecodeError):
        has_audio = False
    if (
        not strip_audio
        and stream.get("codec_name") == "h264"
        and stream.get("pix_fmt") in {"yuv420p", "yuvj420p"}
    ) or (
        strip_audio
        and not has_audio
        and stream.get("codec_name") == "h264"
        and stream.get("pix_fmt") in {"yuv420p", "yuvj420p"}
    ):
        shutil.copy2(raw, destination)
        return source_asset
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(raw),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(destination),
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


_PRESENTATION_SECRET_TERMS = re.compile(
    r"\b(?:email|e-mail|password|passcode|otp|one[ -]?time|token|secret|api[ -]?key|access[ -]?key)\b",
    re.IGNORECASE,
)


def _presentation_secret_redactions(
    trace: DemoTrace,
    beats: list[dict[str, object]],
    *,
    frame_rate: float,
) -> list[dict[str, object]]:
    """Return source-coordinate masks for credentials visible in browser video.

    Trace/log redaction is not sufficient: a native browser recording can
    still show a credential while it is typed or remains in a form field.
    This intentionally relies on generic operation/field semantics, never on
    an application, user, URL, or known credential value.  A mask stays on a
    field until the first proven navigation away from that page, so the value
    cannot reappear while another authentication field is completed.
    """
    if not beats:
        return []
    beat_by_event = {str(item.get("eventId", "")): item for item in beats}
    redactions: list[dict[str, object]] = []
    for index, event in enumerate(trace.events):
        target = event.target
        semantic_text = " ".join(
            value
            for value in (
                event.kind.value if hasattr(event.kind, "value") else str(event.kind),
                event.intent,
                target.name if target else "",
                getattr(target, "autocomplete", "") if target else "",
            )
            if value
        )
        if not _PRESENTATION_SECRET_TERMS.search(semantic_text):
            continue
        beat = beat_by_event.get(event.id)
        if beat is None:
            # A presentation event without a beat is not visible product
            # evidence, therefore it cannot expose an on-screen value.
            continue
        start = int(beat.get("start", 0))
        end = int(beat.get("end", start + 1))
        current_url = event.page_url
        # The editorial source may begin with a stable login frame in which a
        # value was already present before Playwright emitted the corresponding
        # observer event (cloud recording and trace clocks are independent).
        # If this is the first credential-bearing event on that page, protect
        # the whole rendered prelude while keeping the mask field-local. This
        # prevents a typed value from appearing during the opening cut without
        # falling back to an opaque full-frame security card.
        if not any(
            prior.page_url == current_url
            and beat_by_event.get(prior.id) is not None
            and _PRESENTATION_SECRET_TERMS.search(
                " ".join(
                    value
                    for value in (
                        prior.kind.value if hasattr(prior.kind, "value") else str(prior.kind),
                        prior.intent,
                        prior.target.name if prior.target else "",
                        getattr(prior.target, "autocomplete", "") if prior.target else "",
                    )
                    if value
                )
            )
            for prior in trace.events[:index]
        ):
            start = 0
        for later in trace.events[index + 1 :]:
            later_beat = beat_by_event.get(later.id)
            if later_beat is None:
                continue
            if current_url and later.page_url and later.page_url != current_url:
                end = int(later_beat.get("start", end))
                break
            end = max(end, int(later_beat.get("end", end)))
        # A minimum avoids a one-frame flash at the action boundary.  The
        # terminal bound remains the proven source timeline.
        end = max(start + max(1, round(frame_rate * 0.2)), end)
        if event.target_rect is None:
            # Legacy/cloud traces may lack one action's DOM box despite a
            # recorded authentication screen. A full-frame, explicitly
            # labelled secure-sign-in card is the only non-fabricated way to
            # preserve a human-readable login transition without leaking the
            # credential. New captures are expected to use geometry masks.
            # The value may already be present before the browser adapter
            # reports the fill action (autofill and a delayed cloud snapshot
            # are both common).  With no grounded field box there is no safe
            # way to mask only the preceding pixels, so protect the complete
            # authentication prelude rather than risk a single-frame leak.
            # This is deliberately semantic: it applies to any credential
            # field, not a known login page, product, or provider.
            redactions.append(
                {
                    "eventId": event.id,
                    "start": 0,
                    "end": end,
                    "mode": "secure-full-frame",
                    "label": "Signing in securely",
                }
            )
            continue
        redactions.append(
            {
                "eventId": event.id,
                "start": start,
                "end": end,
                "mode": "target-mask",
                # Use the exact recording-space geometry already supplied to the
                # cursor/camera beat.  Cloud recordings often differ from the CSS
                # viewport, so raw DOM coordinates would mask the wrong field.
                "x": float(beat.get("x", event.target_rect.x)),
                "y": float(beat.get("y", event.target_rect.y)),
                "width": float(beat.get("width", event.target_rect.width)),
                "height": float(beat.get("height", event.target_rect.height)),
                "label": "Sensitive value redacted",
            }
        )
    return redactions


def _outro_copy(trace: DemoTrace, storyboard: EditorialStoryboard | None) -> tuple[str, str]:
    """Return a product-specific close from the approved editorial story."""
    if storyboard is not None:
        product = re.sub(r"\bwalkthrough\b", "", storyboard.brief.title, flags=re.IGNORECASE).strip(
            " -:|"
        )
        product = product or concise_demo_title(trace.objective)
        final_scene = next(
            (scene for scene in reversed(storyboard.scenes) if scene.operation_id is not None),
            storyboard.scenes[-1] if storyboard.scenes else None,
        )
        takeaway = " ".join(
            (final_scene.narration if final_scene else storyboard.brief.product_purpose).split()
        )[:180]
    else:
        product = concise_demo_title(trace.objective)
        takeaway = " ".join(trace.objective.split())[:180]
    return (f"That concludes the {product} walkthrough.", takeaway)


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
    return rect.model_copy(
        update={
            "x": rect.x * scale_x,
            "y": rect.y * scale_y,
            "width": rect.width * scale_x,
            "height": rect.height * scale_y,
        }
    )


def _recording_space_cursor_paths(
    presentation: PresentationPlan, trace: DemoTrace, *, source_width: int, source_height: int
) -> list[dict]:
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

        converted.append(
            {
                **path,
                "source": point(path.get("source")),
                "destination": point(path.get("destination")),
                "waypoints": [
                    point(item) for item in path.get("waypoints", []) if isinstance(item, dict)
                ],
            }
        )
    return converted


def _safe_camera_zoom(rect, requested: float, *, source_width: int, source_height: int) -> float:
    """Bound a target zoom so the target and a context margin stay in-frame.

    Camera decisions are produced before the native recording dimensions are
    known.  Re-checking them here prevents edge targets (toolbar buttons,
    sidebars, form fields) from being magnified into a clipped browser frame.
    The returned value is deterministic and never invents a zoom when the
    requested camera can not be made safe.
    """
    try:
        zoom = min(1.36, max(1.0, float(requested)))
    except (TypeError, ValueError):
        zoom = 1.0
    if rect is None or source_width <= 0 or source_height <= 0:
        return zoom
    # Keep at least a small margin around the focused evidence.  This is a
    # target-specific bound, not a global browser-scale change.
    margin = 0.04
    cx = (float(rect.x) + float(rect.width) / 2) / source_width
    cy = (float(rect.y) + float(rect.height) / 2) / source_height
    half_w = (float(rect.width) / source_width) * 0.5 + margin
    half_h = (float(rect.height) / source_height) * 0.5 + margin
    # Scaling around the top-left then translating the focus to centre gives
    # an available half-span of 0.5/zoom in each axis.  Reduce only as much as
    # needed to keep the evidence rectangle visible.
    max_zoom_x = 0.5 / max(half_w, abs(cx - 0.5), 0.001)
    max_zoom_y = 0.5 / max(half_h, abs(cy - 0.5), 0.001)
    safe = min(zoom, max(1.0, max_zoom_x), max(1.0, max_zoom_y))
    return round(min(1.36, max(1.0, safe)), 4)


def _editorial_cut_windows(
    trace: DemoTrace,
    source_seconds: float,
    *,
    minimum_seconds: float = 0.0,
    reading_holds_seconds: dict[str, float] | None = None,
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
            max(
                0.0,
                min(source_seconds, (event.action_at - trace.recording_started_at).total_seconds()),
            )
            for event in successful
        ),
        default=0.0,
    )
    opening_end = max(0.0, first_action - 0.45)
    opening_start = max(0.0, opening_end - 5.0)
    windows: list[tuple[float, float]] = (
        [(opening_start, opening_end)]
        if opening_end - opening_start >= 1.0
        else [(0.0, min(5.0, source_seconds))]
    )
    reading_holds_seconds = reading_holds_seconds or {}
    for event in trace.events:
        if not event.success or event.action_at is None:
            continue
        action = max(
            0.0, min(source_seconds, (event.action_at - trace.recording_started_at).total_seconds())
        )
        reveal = max(
            action,
            min(source_seconds, (event.occurred_at - trace.recording_started_at).total_seconds()),
        )
        # A selected caption owns a reader-sized native dwell after the state
        # it describes becomes visible. Unnarrated trace events retain the
        # compact evidence tail, so this increases time only for the actual
        # editorial story rather than returning to an uncut browser recording.
        post_reveal_hold = max(
            0.35 if compact_tour else 2.9,
            float(reading_holds_seconds.get(event.id, 0.0)),
        )
        # A directed scroll is itself viewer-facing evidence.  Even if a cloud
        # compositor or CDP call reports a long elapsed interval, cutting its
        # middle turns a natural page movement into the exact teleport this
        # layer is meant to prevent. Keep the full motion at native speed.
        # A normal native interaction also stays continuous. For a remote
        # latency gap, preserve both causal edges at normal speed and cut
        # between them.
        # Keep the complete motion itself, but retain only a short, readable
        # settle after the target is visible.  The old 2.9s tail on every
        # landmark accumulated into 6+ minute product renders even though
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
                # A scroll's physical motion is followed by a short settle.
                # Caption-selected scrolls may own a longer reader dwell, but
                # an uncaptioned scroll must not inherit the focused-demo
                # interaction tail (2.9s) merely because it is the only event
                # in a fixture trace.
                scroll_settle = max(
                    0.55,
                    float(reading_holds_seconds.get(event.id, 0.0)),
                )
                motion_end = min(source_seconds, action + float(motion_ms) / 1000.0 + scroll_settle)
                windows.append(
                    (
                        max(0.0, action - (0.1 if compact_tour else 0.6)),
                        max(action + 0.2, motion_end),
                    )
                )
            else:
                windows.append(
                    (
                        max(0.0, action - (0.1 if compact_tour else 0.6)),
                        min(source_seconds, reveal + post_reveal_hold),
                    )
                )
        # Human-visible operations (typing, selecting, dragging, submitting,
        # and pointer gestures) are the proof of a workflow.  Never split the
        # physical gesture from its result just because a remote DOM witness
        # arrived several seconds later; that old micro-cut made fields appear
        # to be pasted and canvas actions look like unexplained jumps.
        elif event.kind in {
            OperationKind.FILL_TEXT,
            OperationKind.FILL_EMAIL,
            OperationKind.FILL_PHONE,
            OperationKind.SELECT_OPTION,
            OperationKind.SELECT_DATE,
            OperationKind.SELECT_DATE_RANGE,
            OperationKind.SUBMIT,
            OperationKind.CREATE_RECORD,
            OperationKind.DRAG,
            OperationKind.POINTER_SEQUENCE,
            OperationKind.KEY_PRESS,
            OperationKind.CLICK,
            OperationKind.OPEN_MODAL,
            OperationKind.CLOSE_MODAL,
        }:
            # Browser/cloud command latency is sometimes folded into the
            # operation duration even though no meaningful product footage is
            # changing during that interval. For genuinely long remote
            # operations, retain a native-speed action beat and a separate
            # result/reveal beat; this removes only the proven idle middle and
            # keeps typing/click causality intact. Short/local typing remains
            # one contiguous interval so values are visibly entered.
            remote_gap = reveal - action
            provider_latency = event.duration_ms / 1000.0 > 20.0 and (
                remote_gap > 3.8
                or event.kind
                in {
                    OperationKind.POINTER_SEQUENCE,
                    OperationKind.KEY_PRESS,
                    OperationKind.CLICK,
                    OperationKind.OPEN_MODAL,
                    OperationKind.CLOSE_MODAL,
                }
            )
            if provider_latency:
                # Browserbase/Stagehand latency is not browser footage. Keep
                # the native gesture itself and a short causal lead-in, then
                # jump to the measured visible result. A blanket 2.5-second
                # action window made long canvas tours exceed the approved
                # editorial envelope even though the pointer/key gesture was
                # sub-second. Form typing retains the larger default so the
                # entered value remains readable.
                action_window = 2.5
                if event.kind is OperationKind.POINTER_SEQUENCE:
                    gesture = event.after.get("gesture") if isinstance(event.after, dict) else None
                    gesture_ms = gesture.get("duration_ms") if isinstance(gesture, dict) else None
                    action_window = max(
                        0.85,
                        min(1.8, float(gesture_ms) / 1000.0 + 0.55)
                        if isinstance(gesture_ms, (int, float))
                        else 1.25,
                    )
                elif event.kind is OperationKind.KEY_PRESS:
                    action_window = 1.25
                elif event.kind in {
                    OperationKind.CLICK,
                    OperationKind.OPEN_MODAL,
                    OperationKind.CLOSE_MODAL,
                }:
                    action_window = 1.45
                windows.append(
                    (
                        max(0.0, action - (0.35 if compact_tour else 0.6)),
                        min(source_seconds, action + action_window),
                    )
                )
                windows.append(
                    (
                        max(0.0, reveal - 1.0),
                        min(source_seconds, reveal + post_reveal_hold),
                    )
                )
            else:
                windows.append(
                    (
                        max(0.0, action - (0.35 if compact_tour else 0.6)),
                        min(
                            source_seconds,
                            max(reveal, action + event.duration_ms / 1000.0) + post_reveal_hold,
                        ),
                    )
                )
        elif reveal - action <= 3.8:
            windows.append(
                (
                    max(0.0, action - (0.1 if compact_tour else 0.6)),
                    min(source_seconds, reveal + post_reveal_hold),
                )
            )
        else:
            # Preserve a visibly complete navigation dispatch and immediate
            # transition before cutting the remote wait; this is the causal
            # edge viewers need to understand the page change.
            windows.append(
                (
                    max(0.0, action - (0.1 if compact_tour else 0.6)),
                    min(source_seconds, action + (1.9 if not compact_tour else 0.3)),
                )
            )
            windows.append(
                (
                    max(0.0, reveal - (0.3 if compact_tour else 1.9)),
                    min(source_seconds, reveal + post_reveal_hold),
                )
            )
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
                min(
                    source_seconds,
                    end + (padding if any(index == item[0] for item in available) else 0.0),
                ),
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
        events.append(
            event.model_copy(
                update={
                    "action_at": trace.recording_started_at
                    + timedelta(seconds=remap(action_seconds)),
                    "occurred_at": trace.recording_started_at
                    + timedelta(seconds=remap(occurred_seconds)),
                }
            )
        )
    return trace.model_copy(update={"events": events})


def _build_editorial_source(
    raw: Path, *, render_dir: Path, windows: list[tuple[float, float]]
) -> Path:
    """Concatenate deliberate cuts without altering the speed of retained footage."""
    # A timing repair can change the retained source windows while keeping the
    # same run id. Ordinal names such as ``000.mp4`` are therefore unsafe as a
    # cache key: they can silently compose a new caption/trace plan over old
    # footage. Bind every reusable clip set to both the exact raw recording
    # fingerprint and the exact native-speed cut list.
    raw_stat = raw.stat()
    cut_key = hashlib.sha256(
        json.dumps(
            {
                "raw": str(raw.resolve()),
                "size": raw_stat.st_size,
                "mtime_ns": raw_stat.st_mtime_ns,
                "windows": [[round(start, 3), round(end, 3)] for start, end in windows],
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    output = render_dir / f"editorial-source-{cut_key}.mp4"
    # The source identity above includes the immutable recording fingerprint
    # and every retained native-speed window. Reusing a complete result is
    # therefore safe across caption/camera/redaction repairs and avoids
    # repeatedly decoding a long cloud recording for presentation-only work.
    if output.is_file() and output.stat().st_size >= 10_000:
        return output
    clips_dir = render_dir / "editorial-clips" / cut_key
    # A first render on a fresh run has no render directory yet.  Create the
    # complete cache path so content-addressed source assembly is independent
    # of whether an earlier failed attempt happened to materialize parents.
    clips_dir.mkdir(parents=True, exist_ok=True)
    clips: list[Path] = []
    for index, (start, end) in enumerate(windows):
        clip = clips_dir / f"{index:03d}.mp4"
        if not clip.exists() or clip.stat().st_size < 10_000:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{start:.3f}",
                    "-t",
                    f"{end - start:.3f}",
                    "-i",
                    str(raw),
                    "-map",
                    "0:v:0",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    str(clip),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        clips.append(clip)
    listing = clips_dir / "clips.ffconcat"
    listing.write_text(
        "ffconcat version 1.0\n"
        + "".join(f"file '{clip.resolve().as_posix()}'\n" for clip in clips),
        encoding="utf-8",
    )
    subprocess.run(
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
            str(listing),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
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
        max(
            0.0,
            min(
                source_seconds,
                (
                    (event.action_at or event.occurred_at) - trace.recording_started_at
                ).total_seconds(),
            ),
        )
        for event in trace.events
    ]
    if max(source_positions) - min(source_positions) < 0.5:
        return None
    frame_scale = screen_frames / source_seconds
    lead_frames = max(1, round(min(0.65, source_seconds / 12) * frame_scale))
    starts = [
        max(0, min(screen_frames - 1, round(position * frame_scale) - lead_frames))
        for position in source_positions
    ]
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
        click_frame = max(
            start, min(screen_frames - 1, round(source_positions[index] * frame_scale))
        )
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
        max(
            0.0,
            ((event.action_at or event.occurred_at) - trace.recording_started_at).total_seconds(),
        )
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
    scale = (
        screen_seconds / evidence_span if evidence_span > screen_seconds and evidence_span else 1.0
    )
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
    # Caption-only output is the narration product until TTS is enabled. A
    # fixed 1.45-second slot made evidence-rich 16–22 word lines unreadable.
    # Allocate a normal silent-reading dwell for each approved editorial beat;
    # if the native recording cannot contain those beats, QA rejects it rather
    # than silently speeding the viewer through the story.
    minimum_dwells = [
        max(2.4, len(str(caption.get("text", "")).split()) / 3.2 + 0.25) for caption in captions
    ]
    if sum(minimum_dwells) > screen_seconds:
        # The renderer must not manufacture reading time by freezing source
        # footage. Let the existing timing path preserve native evidence; the
        # synchronization gate will report the insufficient capture/story
        # envelope and route repair to planning or execution.
        return None
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
        # A narrator may introduce the *first product scene*, while the raw
        # recording still begins with an authentication transition.  Starting
        # that welcome at frame zero would narrate the product over a login
        # form (and, worse, hold it over unrelated intervening footage). Only
        # start at zero when the attached evidence is genuinely the first
        # visible trace event. Otherwise the welcome starts at its own proven
        # product state exactly like every other scene.
        is_first_visible_event = bool(trace.events) and event.id == trace.events[0].id
        # If authentication is the first visible production event, its
        # credential-free presenter line must begin at frame zero. Waiting for
        # the input's DOM result creates an unexplained opening silence even
        # though the trace already proves the login interaction is on screen.
        first_is_authentication = str(event.operation_id or "").startswith("auth:")
        desired_start = (
            0.0
            if is_first_visible_event and (is_presenter_opening or first_is_authentication)
            else visible_start(index, event)
        )
        latest_start = max(0.0, screen_seconds - sum(minimum_dwells[index:]))
        start = min(desired_start, latest_start)
        if starts:
            start = max(start, starts[-1] + minimum_dwells[index - 1])
        starts.append(min(screen_seconds - minimum_dwells[index], start))
    timed: list[dict] = []
    for index, (caption, start) in enumerate(zip(captions, starts, strict=True)):
        next_start = starts[index + 1] if index + 1 < len(starts) else screen_seconds
        # A new action begins the transition away from this scene. Holding the
        # old scene's copy until the next action's *settled* result produces a
        # visibly wrong caption over the incoming route. End before dispatch;
        # the intentional transition gap is preferable to false narration.
        next_action = (
            action_positions[index + 1] if index + 1 < len(action_positions) else screen_seconds
        )
        # Preserve a readable, local dwell without bleeding into the following
        # verified state.  One-event traces simply retain the entire evidence.
        # Beat ranges begin 0.65 seconds before the following event. End the
        # outgoing line inside that range so the incoming caption never
        # narrates a state that is already on screen.
        if index == 0 and (
            bool(caption.get("opening", False))
            or str(caption.get("text", "")).lstrip().lower().startswith("welcome to ")
        ):
            # A welcome should establish the product, not monopolise a long
            # opening hold. Keep enough time for a calm read while allowing
            # the first page-local explanation to start promptly.
            reading_seconds = len(str(caption.get("text", "")).split()) / 2.8 + 0.7
            # A stable opening may legitimately precede the first deliberate
            # scroll by several seconds. Keep its grounded welcome visible
            # through that setup rather than leaving a silent, unexplained
            # gap, while capping the hold so one line never monopolises the
            # page. The subsequent caption still waits for its own visible
            # evidence.
            # Keep the opening caption on the established frame until the
            # first deliberate reveal.  A hard cap here made a long,
            # presenter-style welcome disappear while the browser was still
            # holding the opening page, creating both an unexplained silence
            # gap and a reader-dwell failure.  The next verified action is the
            # natural hand-off boundary; it is already bounded by the source
            # recording and the storyboard minimums.
            bridge_hold = max(reading_seconds, next_action - start - 0.2)
            # The next caption's allocated start is the authoritative
            # hand-off boundary.  Navigation/action clocks can lag that
            # editorial slot; using them alone allowed the welcome to overlap
            # the first scene caption and made the rendered timeline invalid.
            handoff = next_start - 0.1 if index + 1 < len(starts) else screen_seconds
            # The first event is the opening-page establish/scroll beat. Keep
            # its presenter line through that deliberate opening motion; the
            # next caption's allocated start is the authoritative handoff,
            # so a pre-dispatch cap would create a silent gap and truncate a
            # valid welcome whenever the first gesture begins early.
            end = min(handoff, max(start + minimum_dwells[index], bridge_hold))
        else:
            end = max(start + minimum_dwells[index], min(next_start - 0.1, next_action - 0.2))
            # Remote authentication and SPA transitions can leave a several-
            # second interval between the outgoing action clock and the next
            # readable state.  Caption-only delivery must not go silent while
            # the same witnessed scene is still on screen: extend the
            # outgoing, evidence-backed explanation to the next scene handoff
            # (without overlapping it).  This preserves the real browser
            # footage and avoids synthetic filler or speed changes.
            if index + 1 < len(starts) and next_start - end > 6.0:
                end = min(screen_seconds, next_start - 0.1)
        end = min(screen_seconds, end)
        if end <= start:
            start = max(0.0, min(start, screen_seconds - 0.1))
            end = screen_seconds
        timed.append({**caption, "start": round(start, 2), "end": round(end, 2)})
    return timed

__all__ = [
    "_PRESENTATION_SECRET_TERMS",
    "_build_editorial_source",
    "_editorial_cut_windows",
    "_evidence_timed_beat_ranges",
    "_evidence_timed_captions",
    "_frame_rate",
    "_outro_copy",
    "_prepare_remotion_source",
    "_presentation_secret_redactions",
    "_recording_space_cursor_paths",
    "_recording_space_rect",
    "_remap_trace_for_cuts",
    "_safe_camera_zoom",
]
