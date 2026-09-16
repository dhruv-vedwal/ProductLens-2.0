from __future__ import annotations

from app.contracts.models import DemoTrace, InteractionEvent, OperationKind


def bind_opening_to_first_event(
    script: list[dict[str, object]], trace: DemoTrace
) -> list[dict[str, object]]:
    """Attach a presenter welcome to the first visible product evidence.

    Editorial storyboard selection may omit a low-level opening ``verify``
    beat from narrated scenes and bind the welcome to the first navigation.
    That leaves an unexplained silent prelude in the rendered browser footage.
    Rebind only the opening line to the first successful trace event; no
    operation is replayed and the approved wording/evidence remain unchanged.
    """
    if not script or not script[0].get("opening"):
        return script
    first_event = next((event for event in trace.events if event.success), None)
    if first_event is None or str(script[0].get("event_id", "")) == first_event.id:
        return script
    opening = {**script[0], "event_id": first_event.id}
    # One event owns at most one caption. If storyboard selection already
    # included that low-level event later, retain the presenter opening as the
    # authoritative line and remove the duplicate rather than breaking the
    # caption/trace identity contract.
    return [
        opening,
        *[line for line in script[1:] if str(line.get("event_id", "")) != first_event.id],
    ]


def _sentence(intent: str) -> str:
    return intent.strip().rstrip(".")[:1].lower() + intent.strip().rstrip(".")[1:]


def _target_name(event: InteractionEvent) -> str:
    """Turn stable target evidence into a caption-safe label, not a selector."""
    name = (event.target.name if event.target else event.intent).replace("-", " ").replace("_", " ")
    return " ".join(name.split())


def _narrative_action(event: InteractionEvent) -> str:
    target = _target_name(event)
    value = str(
        event.after.get("value") or event.before.get("value") or event.target.text
        if event.target and event.target.text
        else ""
    ).strip()
    if event.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}:
        return f"move into {target}, establishing its visible context before examining the meaningful details"
    if event.kind in {OperationKind.WAIT_FOR_STATE, OperationKind.VERIFY_STATE}:
        return (
            f"start in {target}, establishing the current workspace before following the workflow"
        )
    if event.kind in {
        OperationKind.FILL_TEXT,
        OperationKind.FILL_EMAIL,
        OperationKind.FILL_PHONE,
        OperationKind.SEARCH,
    }:
        return f"enter the required details in {target} so the next step has the right context"
    if event.kind in {
        OperationKind.SELECT_OPTION,
        OperationKind.SELECT_DATE,
        OperationKind.SELECT_DATE_RANGE,
        OperationKind.CHOOSE_RADIO,
    }:
        choice = f" {value}" if value else ""
        return f"choose{choice} in {target} to set the intended option"
    if event.kind in {OperationKind.SUBMIT, OperationKind.CREATE_RECORD}:
        return f"submit {target} to complete the workflow"
    if event.kind is OperationKind.SCROLL_TO:
        return f"continue through {target}, pausing on the visible information needed to understand this part of the product"
    if event.kind in {OperationKind.OPEN_MODAL, OperationKind.CLICK, OperationKind.APPLY_FILTER}:
        return f"select {target} to reveal the next part of the product story"
    if event.kind is OperationKind.HOVER:
        return f"pause over {target} to reveal the contextual affordance"
    if event.kind is OperationKind.KEY_PRESS:
        return f"use the keyboard on {target} to advance the current interaction"
    if event.kind in {OperationKind.DRAG, OperationKind.POINTER_SEQUENCE}:
        return "move the observed control along its visible path to demonstrate the interaction"
    if event.kind is OperationKind.UPLOAD:
        return f"add the selected file through {target} so the product can process the supplied material"
    return _sentence(event.intent)


def script_from_trace(
    trace: DemoTrace, *, audience: str = "product prospect"
) -> list[dict[str, object]]:
    """Create a concise, evidence-only narrative when a writer model is unavailable.

    Every sentence is anchored to one successful event.  This deliberately
    produces a demonstrator's progression rather than a raw list of click
    labels, while retaining the same event ids used by captions and rendering.
    """
    events = [event for event in trace.events if event.success]
    script: list[dict[str, object]] = []
    for index, event in enumerate(events):
        action = _narrative_action(event)
        audience_lower = audience.lower()
        if "recruit" in audience_lower or "hiring" in audience_lower:
            lens = "highlighting the capability and engineering context"
        elif "support" in audience_lower or "internal" in audience_lower:
            lens = "making the relevant workflow and resulting state clear"
        elif "developer" in audience_lower or "technical" in audience_lower:
            lens = "showing the implementation-relevant behavior and evidence"
        else:
            lens = ""
        suffix = f", {lens}" if lens else ""
        if len(events) == 1 or index == 0:
            text = f"We {action}{suffix}."
        elif index == len(events) - 1:
            text = f"Finally, we {action}{suffix}."
        else:
            text = f"Next, we {action}{suffix}."
        script.append({"event_id": event.id, "text": text, "facts": event.after})
    return script


def captions_from_duration(
    script: list[dict[str, object]], duration_seconds: float
) -> list[dict[str, object]]:
    """Spread evidence-backed captions across either narration or visible screen time."""
    if not script:
        return []
    step = duration_seconds / len(script)
    return [
        {
            "start": round(index * step, 3),
            "end": round((index + 1) * step, 3),
            "text": item["text"],
            "scene_id": item["event_id"],
            **({"opening": True} if item.get("opening", False) else {}),
        }
        for index, item in enumerate(script)
    ]


def captions_from_audio_duration(
    script: list[dict[str, object]], duration_seconds: float
) -> list[dict[str, object]]:
    """Compatibility alias for callers with a synthesized narration track."""
    return captions_from_duration(script, duration_seconds)


def recommended_caption_duration(
    script: list[dict[str, object]], *, minimum_scene_seconds: float = 2.4
) -> float:
    """Return a readable fallback track length for caption-only delivery.

    Caption timing is normally replaced by verified browser-event timestamps
    during rendering, but local/synthetic traces do not always have a usable
    recorder clock.  A fixed per-event duration made multi-sentence editorial
    copy flash by too quickly.  Allocate from word count at a conservative
    speaking/reading rate, with a minimum dwell for every scene and a small
    transition allowance.  Measured audio remains the source of truth.
    """
    if not script:
        return 0.0
    # Keep this calculation in lockstep with the synchronization gate.  The
    # previous aggregate word-rate estimate could produce a per-caption slice
    # just below the reader dwell requirement after rounding, rejecting an
    # otherwise valid fixture or URL render.  Allocate each line independently
    # and then add a small transition allowance between scenes.
    per_scene_requirements = [
        max(
            float(minimum_scene_seconds),
            len(str(item.get("text", "")).split()) / 3.2 + 0.25,
        )
        for item in script
    ]
    # ``captions_from_duration`` gives every scene an equal slice.  Therefore
    # the total must be based on the longest line, not the sum of independent
    # requirements, otherwise a single long sentence is still under-dwelled.
    scene_floor = max(per_scene_requirements) * len(script)
    transition_seconds = max(0.0, (len(script) - 1) * 0.35)
    return round(max(3.0, scene_floor + transition_seconds), 2)


def captions_from_measured_segments(
    script: list[dict[str, object]],
    durations_seconds: list[float],
    *,
    starts_seconds: list[float] | None = None,
) -> list[dict[str, object]]:
    """Create scene captions from actual synthesized segment durations.

    Character counts and total-track division are only estimates. Each segment
    is measured before concatenation, so the cursor, caption, and future audio
    owner all retain the same stable event/scene identity.
    """
    if len(script) != len(durations_seconds):
        raise ValueError("every narration line requires one measured audio segment")
    if starts_seconds is not None and len(script) != len(starts_seconds):
        raise ValueError("every narration line requires one scene start offset")
    cursor = 0.0
    captions: list[dict[str, object]] = []
    for index, (line, duration) in enumerate(zip(script, durations_seconds, strict=True)):
        if duration <= 0:
            raise ValueError("narration segment duration must be positive")
        if starts_seconds is not None:
            cursor = starts_seconds[index]
        if cursor < 0:
            raise ValueError("narration scene start offset must be non-negative")
        end = cursor + duration
        captions.append(
            {
                "start": round(cursor, 3),
                "end": round(end, 3),
                "text": line["text"],
                "scene_id": line["event_id"],
                **({"opening": True} if line.get("opening", False) else {}),
            }
        )
        cursor = end
    return captions
