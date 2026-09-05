from __future__ import annotations

from productlens.contracts.models import DemoTrace, InteractionEvent, OperationKind


def _sentence(intent: str) -> str:
    return intent.strip().rstrip(".")[:1].lower() + intent.strip().rstrip(".")[1:]


def _target_name(event: InteractionEvent) -> str:
    """Turn stable target evidence into a caption-safe label, not a selector."""
    name = (event.target.name if event.target else event.intent).replace("-", " ").replace("_", " ")
    return " ".join(name.split())


def _narrative_action(event: InteractionEvent) -> str:
    target = _target_name(event)
    value = str(event.after.get("value") or event.before.get("value") or event.target.text if event.target and event.target.text else "").strip()
    if event.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}:
        return f"move into {target}, establishing its visible context before examining the meaningful details"
    if event.kind in {OperationKind.WAIT_FOR_STATE, OperationKind.VERIFY_STATE}:
        return f"start in {target}, establishing the current workspace before following the workflow"
    if event.kind in {OperationKind.FILL_TEXT, OperationKind.FILL_EMAIL, OperationKind.FILL_PHONE, OperationKind.SEARCH}:
        return f"enter the required details in {target} so the next step has the right context"
    if event.kind in {OperationKind.SELECT_OPTION, OperationKind.SELECT_DATE, OperationKind.SELECT_DATE_RANGE, OperationKind.CHOOSE_RADIO}:
        choice = f" {value}" if value else ""
        return f"choose{choice} in {target} to set the intended option"
    if event.kind in {OperationKind.SUBMIT, OperationKind.CREATE_RECORD}:
        return f"submit {target} to complete the workflow"
    if event.kind is OperationKind.SCROLL_TO:
        return f"continue through {target}, pausing on the visible information needed to understand this part of the product"
    if event.kind in {OperationKind.OPEN_MODAL, OperationKind.CLICK, OperationKind.APPLY_FILTER}:
        return f"select {target} to reveal the next part of the product story"
    return _sentence(event.intent)


def script_from_trace(trace: DemoTrace, *, audience: str = "product prospect") -> list[dict[str, object]]:
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
        }
        for index, item in enumerate(script)
    ]


def captions_from_audio_duration(
    script: list[dict[str, object]], duration_seconds: float
) -> list[dict[str, object]]:
    """Compatibility alias for callers with a synthesized narration track."""
    return captions_from_duration(script, duration_seconds)


def captions_from_measured_segments(
    script: list[dict[str, object]], durations_seconds: list[float], *, starts_seconds: list[float] | None = None
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
            }
        )
        cursor = end
    return captions
