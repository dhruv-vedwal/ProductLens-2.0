"""Derive stable editorial moments from verified execution evidence."""

from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Any

from app.contracts.models import DemoTrace, OperationKind, SemanticMoment


def _moment_kind(event_kind: OperationKind) -> str:
    if event_kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}:
        return "navigation"
    if event_kind in {
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
    }:
        return "interaction"
    if event_kind in {OperationKind.VERIFY_STATE, OperationKind.READ_VALUE}:
        return "verification"
    if event_kind in {OperationKind.SCROLL_TO, OperationKind.WAIT_FOR_STATE}:
        return "inspection"
    return "context"


def build_semantic_moments(trace: DemoTrace) -> list[SemanticMoment]:
    """Group low-level events into audience-meaningful verified outcomes."""

    form_kinds = {
        OperationKind.FILL_TEXT,
        OperationKind.FILL_EMAIL,
        OperationKind.FILL_PHONE,
        OperationKind.SELECT_OPTION,
        OperationKind.SELECT_DATE,
        OperationKind.SELECT_DATE_RANGE,
        OperationKind.CHECK,
        OperationKind.UNCHECK,
        OperationKind.CHOOSE_RADIO,
    }

    def group_key(event) -> tuple[str, str]:
        if str(event.operation_id).startswith("auth:"):
            return ("authentication", event.page_url or "")
        evidence = " ".join(event.required_content_groups + event.covered_content_groups)
        if event.kind in {OperationKind.SUBMIT, OperationKind.CREATE_RECORD}:
            return ("verification", event.page_url or "")
        if event.kind in form_kinds:
            return ("form_entry", event.page_url or "")
        if "->" in evidence:
            return ("diagram_connectors", event.page_url or "")
        if event.kind in {OperationKind.POINTER_SEQUENCE, OperationKind.KEY_PRESS} and (
            event.required_content_groups or event.covered_content_groups
        ):
            return ("diagram_nodes", event.page_url or "")
        if event.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}:
            return (f"navigation:{event.id}", event.page_url or "")
        return (_moment_kind(event.kind), event.page_url or "")

    def viewer_value(key: str, events) -> str:
        labels = list(
            dict.fromkeys(
                item
                for event in events
                for item in [*event.required_content_groups, *event.covered_content_groups]
                if item and "->" not in item
            )
        )
        if key == "form_entry":
            names = list(
                dict.fromkeys(
                    event.target.name
                    for event in events
                    if event.target is not None and event.target.name
                )
            )
            return (
                f"Complete the required {', '.join(names[:3])} details"
                if names
                else "Complete the workflow details in a deliberate order"
            )
        if key == "diagram_nodes":
            return (
                f"Build labeled architecture components for {', '.join(labels[:4])}"
                if labels
                else "Build the labeled architecture components"
            )
        if key == "diagram_connectors":
            return "Connect the verified components into a readable system flow"
        if key == "verification":
            return "Verify the newly created result with entity-specific evidence"
        if key == "authentication":
            return "Establish the authenticated workspace without exposing credentials"
        if key.startswith("navigation:"):
            target = events[-1].target.name if events[-1].target else "the requested workspace"
            return f"Open {target} and establish the next workflow state"
        if key == "inspection":
            return "Inspect the visible result and retain enough reading time"
        return events[-1].intent

    moments: list[SemanticMoment] = []
    groups: list[tuple[tuple[str, str], list]] = []
    for event in (item for item in trace.events if item.success):
        key = group_key(event)
        if groups and groups[-1][0] == key and len(groups[-1][1]) < 32:
            groups[-1][1].append(event)
        else:
            groups.append((key, [event]))
    for group_index, ((key, page_url), events) in enumerate(groups):
        start = min(event.action_at or event.occurred_at for event in events)
        end = max(
            event.occurred_at + timedelta(milliseconds=max(1, event.duration_ms))
            for event in events
        )
        evidence = [
            f"recovery:{event.id}:{index}"
            for event in events
            for index, _ in enumerate(event.recovery)
        ]
        evidence.extend(f"page:{event.page_url}" for event in events if event.page_url)
        evidence.extend(
            f"element:{event.target.name}"
            for event in events
            if event.target and event.target.name
        )
        moments.append(
            SemanticMoment(
                id=f"moment-{events[0].id}",
                kind=(
                    "interaction"
                    if key in {"form_entry", "diagram_nodes", "diagram_connectors"}
                    else "navigation"
                    if key.startswith("navigation:")
                    else "verification"
                    if key == "verification"
                    else "context"
                    if key == "authentication"
                    else key
                ),
                event_ids=[event.id for event in events],
                page_url=page_url or None,
                evidence_refs=list(dict.fromkeys(evidence)),
                viewer_value=viewer_value(key, events),
                start_at=start,
                end_at=max(start, end),
                verified=all(event.success for event in events),
            )
        )
    return moments


def sync_edl_from_moments(trace: DemoTrace) -> dict:
    """Return the sole timeline contract for editorial, render, and QA.

    Keep useful browser footage (gestures, typing, reveals, short reading
    settles). Trim only long idle / silence gaps between proven moments —
    never chop mid-interaction with flat duration caps.
    """

    origin = trace.recording_started_at or trace.started_at
    # Only excise settled chrome when nothing meaningful happens for this long.
    idle_gap_seconds = 3.5

    def seconds(value: datetime) -> float:
        return max(0.0, (value - origin).total_seconds())

    moment_rows = []
    source_windows: list[dict[str, Any]] = []
    event_by_id = {event.id: event for event in trace.events}
    visual_story = bool(
        re.search(
            r"\b(?:draw|drawing|diagram|architecture|whiteboard|canvas|flowchart|map)\b",
            str(trace.objective or ""),
            flags=re.IGNORECASE,
        )
    )
    for moment in trace.moments:
        events = [event_by_id[event_id] for event_id in moment.event_ids if event_id in event_by_id]
        protected_spans = []
        for event in events:
            action_at = event.action_at or event.occurred_at
            action_start = seconds(action_at)
            reveal = max(action_start, seconds(event.occurred_at))
            raw_duration = max(0.001, event.duration_ms / 1_000)
            if event.kind == OperationKind.SCROLL_TO:
                # Continuous scroll paths must remain intact in the EDL.
                span_end = seconds(event.occurred_at) + raw_duration
                reason = "continuous-scroll"
            elif event.kind in {
                OperationKind.FILL_TEXT,
                OperationKind.FILL_EMAIL,
                OperationKind.FILL_PHONE,
                OperationKind.SEARCH,
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
                OperationKind.OPEN_NAVIGATION_ITEM,
                OperationKind.CHECK,
                OperationKind.UNCHECK,
                OperationKind.CHOOSE_RADIO,
            }:
                # Toolbar/tool-selection clicks in a visual editor are
                # transitional beats. Preserve the visible click itself, but
                # do not carry the remote DOM round-trip that follows it into
                # the final cut; the next drawing/typing scene proves the
                # resulting state.
                if visual_story and event.kind is OperationKind.CLICK:
                    protected_spans.append(
                        {
                            "event_id": event.id,
                            "start_seconds": round(action_start, 3),
                            "end_seconds": round(action_start + 0.95, 3),
                            "reason": "visible-tool-selection",
                        }
                    )
                    continue
                # Keep the full native gesture through its visible result.
                # ``duration_ms`` often includes post-result editorial holds and
                # cloud round-trips; prefer action→reveal plus a short settle
                # so we do not invent minutes of frozen chrome, and never
                # truncate typing/clicks with a flat 2.8s cap.
                typed = str(getattr(event, "value", None) or "")
                typing_floor = (
                    max(1.25, len(typed) * 0.09 + 0.9)
                    if event.kind
                    in {
                        OperationKind.FILL_TEXT,
                        OperationKind.FILL_EMAIL,
                        OperationKind.FILL_PHONE,
                        OperationKind.SEARCH,
                    }
                    else 0.0
                )
                span_end = max(reveal, action_start + typing_floor) + 0.85
                # Remote latency sometimes parks the browser for a long gap
                # after the gesture with no new product motion. Keep the
                # gesture and the reveal; drop only that idle middle.
                remote_idle = reveal - action_start
                if remote_idle > idle_gap_seconds and typing_floor == 0.0 and raw_duration > 12.0:
                    # Split into two protected edges; the gap between them is
                    # omitted when windows are assembled below.
                    protected_spans.append(
                        {
                            "event_id": event.id,
                            "start_seconds": round(action_start, 3),
                            "end_seconds": round(action_start + min(2.5, max(0.85, remote_idle * 0.15)), 3),
                            "reason": "visible-interaction-onset",
                        }
                    )
                    protected_spans.append(
                        {
                            "event_id": event.id,
                            "start_seconds": round(max(action_start, reveal - 1.0), 3),
                            "end_seconds": round(reveal + 0.85, 3),
                            "reason": "visible-result",
                        }
                    )
                    continue
                reason = "visible-interaction"
            else:
                # Inspection / verify: keep action through reveal, not a stub.
                span_end = reveal + 0.85
                reason = "visible-interaction"
            protected_spans.append(
                {
                    "event_id": event.id,
                    "start_seconds": round(action_start, 3),
                    "end_seconds": round(span_end, 3),
                    "reason": reason,
                }
            )
        # Dense visual editors produce many short, causal beats (choose a
        # tool, draw, label, connect).  Holding every beat for the generic
        # eight-second reading ceiling turns a human interaction into a long
        # slideshow and can push a detailed but valid canvas story past its
        # requested delivery envelope. Keep a compact, readable native hold
        # for these gesture-heavy moments; form fields and ordinary page
        # explanations retain the fuller editorial dwell.
        visual_gesture = any(
            event.kind
            in {
                OperationKind.POINTER_SEQUENCE,
                OperationKind.KEY_PRESS,
            }
            for event in events
        )
        if visual_gesture or visual_story:
            reading_hold = min(
                1.9,
                max(1.55, len(str(moment.viewer_value or "").split()) / 7.0 + 0.2),
            )
        else:
            reading_hold = min(
                8.0,
                max(2.6, len(str(moment.viewer_value or "").split()) / 2.8 + 0.45),
            )
        interaction_start = min(
            (float(span["start_seconds"]) for span in protected_spans),
            default=seconds(moment.start_at),
        )
        interaction_end = max(
            (float(span["end_seconds"]) for span in protected_spans),
            default=seconds(moment.end_at),
        )
        start = max(0.0, interaction_start - 0.55)
        end = max(start + 0.25, interaction_end + reading_hold)
        # Never invent footage past the observed moment end.
        end = min(end, seconds(moment.end_at) + 0.35)
        active_rect = next(
            (event.target_rect for event in reversed(events) if event.target_rect is not None),
            None,
        )
        cursor_keyframes = []
        for event in events:
            if event.target_rect is None:
                continue
            action_seconds = seconds(event.action_at or event.occurred_at)
            x = event.target_rect.x + event.target_rect.width / 2
            y = event.target_rect.y + event.target_rect.height / 2
            cursor_keyframes.extend(
                [
                    {"at_seconds": round(max(start, action_seconds - 0.35), 3), "state": "travel", "x": x, "y": y},
                    {"at_seconds": round(max(start, action_seconds - 0.1), 3), "state": "settle", "x": x, "y": y},
                    {"at_seconds": round(action_seconds, 3), "state": "press", "x": x, "y": y},
                    {"at_seconds": round(action_seconds + 0.09, 3), "state": "release", "x": x, "y": y},
                    {"at_seconds": round(min(end, action_seconds + 0.35), 3), "state": "dwell", "x": x, "y": y},
                ]
            )
        caption_start = min(end - 0.1, start + 0.35)
        caption_end = max(caption_start + 0.1, end - 0.25)
        scroll_events = [
            event
            for event in events
            if event.kind == OperationKind.SCROLL_TO and event.scroll_path
        ]
        # Build windows that cover useful spans continuously. Cut only when
        # the gap between spans is long idle/silence.
        ordered_spans = sorted(
            protected_spans,
            key=lambda span: (float(span["start_seconds"]), float(span["end_seconds"])),
        )
        candidate_windows: list[tuple[float, float]] = []
        if ordered_spans:
            window_start = max(0.0, float(ordered_spans[0]["start_seconds"]) - 0.45)
            window_end = max(
                window_start + 0.25,
                float(ordered_spans[0]["end_seconds"]),
            )
            for span in ordered_spans[1:]:
                span_start = max(0.0, float(span["start_seconds"]) - 0.45)
                span_end = max(span_start + 0.25, float(span["end_seconds"]))
                if span_start <= window_end + idle_gap_seconds:
                    window_end = max(window_end, span_end)
                else:
                    candidate_windows.append((window_start, window_end))
                    window_start, window_end = span_start, span_end
            # Caption reading belongs on the last useful beat of the moment.
            window_end = window_end + reading_hold
            window_end = min(window_end, seconds(moment.end_at) + 0.35)
            candidate_windows.append((window_start, window_end))
        else:
            candidate_windows.append((start, end))
        for window_start, window_end in candidate_windows:
            if (
                source_windows
                and window_start <= float(source_windows[-1]["end"]) + idle_gap_seconds
            ):
                source_windows[-1]["end"] = max(
                    float(source_windows[-1]["end"]), window_end
                )
                source_windows[-1]["moment_ids"] = [
                    *source_windows[-1].get("moment_ids", []),
                    moment.id,
                ]
            else:
                source_windows.append(
                    {
                        "start": window_start,
                        "end": window_end,
                        "moment_ids": [moment.id],
                    }
                )
        moment_rows.append(
            {
                "id": moment.id,
                "kind": moment.kind,
                "event_ids": moment.event_ids,
                "page_url": moment.page_url,
                "evidence_refs": moment.evidence_refs,
                "viewer_value": moment.viewer_value,
                "source_start_seconds": round(start, 3),
                "source_end_seconds": round(end, 3),
                "protected_interaction_spans": protected_spans,
                "active_geometry": active_rect.model_dump(mode="json") if active_rect else None,
                "verified": moment.verified,
                "action_classification": (
                    "transitional"
                    if moment.kind in {"navigation", "transition"}
                    else "essential"
                    if moment.verified
                    else "dead_time"
                ),
                "tracks": {
                    "camera": {
                        "state": "target-focus"
                        if moment.kind in {"interaction", "verification"}
                        else "full-frame",
                        "keyframes": [
                            {"at_seconds": round(start, 3), "state": "establish"},
                            {
                                "at_seconds": round(min(end, start + 0.35), 3),
                                "state": "focus"
                                if active_rect is not None
                                else "hold",
                                "geometry": active_rect.model_dump(mode="json")
                                if active_rect
                                else None,
                            },
                            {"at_seconds": round(end, 3), "state": "hold"},
                        ],
                    },
                    "cursor": {
                        "state": "visible"
                        if moment.kind in {"navigation", "interaction"}
                        else "settled",
                        "keyframes": cursor_keyframes,
                    },
                    "caption": {
                        "text": moment.viewer_value,
                        "start_seconds": round(caption_start, 3),
                        "end_seconds": round(caption_end, 3),
                    },
                    "narration": {
                        "start_seconds": round(caption_start, 3),
                        "end_seconds": round(caption_end, 3),
                        "audio_ref": None,
                    },
                    "scroll": {
                        "state": "continuous" if scroll_events else "settled",
                        "intervals": [
                            {
                                "event_id": event.id,
                                "start_seconds": round(
                                    seconds(event.action_at or event.occurred_at), 3
                                ),
                                "end_seconds": round(
                                    seconds(event.occurred_at)
                                    + max(0.001, event.duration_ms / 1_000),
                                    3,
                                ),
                                "path": event.scroll_path,
                            }
                            for event in scroll_events
                        ],
                    },
                    "redaction": {
                        "intervals": [
                            {
                                "event_id": event.id,
                                "start_seconds": round(start, 3),
                                "end_seconds": round(end, 3),
                            }
                            for event in events
                            if str(event.operation_id).startswith("auth:")
                        ]
                    },
                    "transition": {
                        "state": "cut"
                        if moment.kind == "navigation"
                        else "hold",
                        "cut_reason": "verified-navigation-boundary"
                        if moment.kind == "navigation"
                        else "protected-moment-continuity",
                    },
                },
            }
        )
    return {
        "schema_version": 2,
        "run_id": trace.run_id,
        "authority": "semantic_moments",
        "source": "verified_demo_trace",
        "source_origin": origin.isoformat(),
        "source_windows": [
            {
                **window,
                "start": round(float(window["start"]), 3),
                "end": round(float(window["end"]), 3),
            }
            for window in source_windows
        ],
        "moments": moment_rows,
    }
