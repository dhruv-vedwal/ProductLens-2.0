"""Human-paced, page-complete scene direction from verified browser evidence.

The execution trace is an audit trail, not a screenplay.  A rendered scene is
therefore given a page-local role and an explicit completion contract so a tab
cannot be considered covered simply because its navigation click succeeded.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from productlens.contracts.models import DemoTrace, EditorialStoryboard, OperationKind, ScenePlan


def _page_key(url: str | None) -> str:
    if not url:
        return "unknown"
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", parts.query, ""))


def build_scene_plan(trace: DemoTrace, *, storyboard: EditorialStoryboard | None = None) -> list[dict]:
    scenes: list[dict] = []
    for event in trace.events:
        if not event.success:
            continue
        kind = event.kind
        page_key = _page_key(event.page_url)
        is_opening = not scenes
        is_navigation = kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}
        if is_opening and kind is not OperationKind.SCROLL_TO:
            phase, page_stage, lead, dwell = "establish", "establish", 0.5, 3.0
        elif kind is OperationKind.SCROLL_TO:
            # ``reveal`` is retained for renderer/API compatibility; the
            # page-local contract carries the richer editorial meaning.
            phase, page_stage, lead, dwell = "reveal", "explore", 0.55, 2.4
        elif is_navigation:
            phase, page_stage, lead, dwell = "transition", "enter", 0.65, 2.6
        elif kind in {OperationKind.FILL_TEXT, OperationKind.FILL_EMAIL, OperationKind.FILL_PHONE}:
            phase, page_stage, lead, dwell = "demonstrate", "demonstrate", 0.45, 2.0
        else:
            phase, page_stage, lead, dwell = "explain", "inspect", 0.4, 1.8
        editorial = next((scene for scene in (storyboard.scenes if storyboard else []) if scene.operation_id == event.operation_id), None)
        if editorial:
            # An editorial scene has stronger semantic information than an
            # event type. Preserve its phase while retaining page-local stage.
            phase = editorial.story_phase
            if phase in {"context", "enter", "explain", "demonstrate", "verify", "transition", "close"}:
                page_stage = {"context": "establish", "enter": "enter", "explain": "explain", "demonstrate": "demonstrate", "verify": "verify", "transition": "transition", "close": "transition"}[phase]
        # Captions are explanatory, never an opaque obstruction. A target in
        # the lower portion of the recorded viewport gets the top safe zone;
        # otherwise the bottom overlay preserves the product's heading/nav.
        caption_safe_zone = editorial.caption_safe_zone if editorial else "bottom"
        if event.target_rect is not None and event.viewport is not None:
            target_center_y = event.target_rect.y + event.target_rect.height / 2
            if target_center_y >= event.viewport.height * 0.58:
                caption_safe_zone = "top"
        scenes.append({
            "event_id": event.id, "scene_id": editorial.id if editorial else f"trace-{event.id}",
            "intent": event.intent, "phase": phase, "page_stage": page_stage, "page_key": page_key,
            "lead_seconds": lead, "dwell_seconds": editorial.required_dwell_seconds if editorial else dwell,
            # Navigation is still a real human click. Hiding the overlay for
            # every transition made tab changes look disconnected from the
            # recorded gesture and masked cursor/target QA.
            "show_cursor": kind in {OperationKind.CLICK, OperationKind.OPEN_NAVIGATION_ITEM, OperationKind.OPEN_MODAL, OperationKind.CLOSE_MODAL, OperationKind.SUBMIT, OperationKind.APPLY_FILTER},
            "caption_safe_zone": caption_safe_zone,
            "editorial_scene_id": editorial.id if editorial else None,
            "completion_criteria": editorial.completion_criteria if editorial else ["verified operation completed"],
            "required_content": editorial.visible_proof if editorial else [event.intent],
            "required_content_groups": list(event.required_content_groups),
            "covered_content_groups": list(event.covered_content_groups),
            "page_contract_phases": list(event.page_contract_phases),
            "page_contract": {
                "page": page_key,
                "stage": page_stage,
                "required": ["establish", "explore", "explain", "demonstrate_or_inspect", "verify", "transition"],
                "complete_only_after": "visible evidence is held for the required dwell and the scene completion criteria are met",
            },
            "camera": {
                # Focus is an editorial exception. A form field being filled
                # is compact, actionable evidence; headings, page transitions
                # and ordinary reading retain the source-faithful full frame.
                "mode": "target-focus" if kind in {
                    OperationKind.FILL_TEXT, OperationKind.FILL_EMAIL, OperationKind.FILL_PHONE,
                    OperationKind.SELECT_OPTION, OperationKind.SELECT_DATE,
                    OperationKind.SELECT_DATE_RANGE,
                } else "full-frame",
                "reason": "compact actionable control is being demonstrated" if kind in {
                    OperationKind.FILL_TEXT, OperationKind.FILL_EMAIL, OperationKind.FILL_PHONE,
                    OperationKind.SELECT_OPTION, OperationKind.SELECT_DATE,
                    OperationKind.SELECT_DATE_RANGE,
                } else "page context is more valuable than target magnification",
                "duration_seconds": 0.45,
                "easing": "out-cubic",
                "safety_bounds": "preserve-browser-frame",
            },
            "cursor": {"mode": "natural-path", "pre_action_pause_seconds": 0.25, "click_at_target_center": True},
            "scroll": {
                "mode": "directed" if kind is OperationKind.SCROLL_TO else "none",
                "continuity": "intermediate-landmarks-and-settle" if kind is OperationKind.SCROLL_TO else "not-applicable",
                "settle_seconds": 0.55 if kind is OperationKind.SCROLL_TO else 0.3,
            },
        })
    return scenes


def inspect_scene_plan(trace: DemoTrace, scenes: list[dict]) -> dict:
    failures: list[str] = []
    event_ids = [event.id for event in trace.events if event.success]
    if [scene.get("event_id") for scene in scenes] != event_ids:
        failures.append("SCENE_TRACE_MISMATCH")
    if any(float(scene.get("dwell_seconds", 0)) < 1.0 for scene in scenes):
        failures.append("INSUFFICIENT_SCENE_DWELL")
    return {"editorial_score": 1.0 if not failures else 0.0, "hard_failures": failures, "scene_count": len(scenes)}


def build_typed_scene_plan(
    trace: DemoTrace, *, storyboard: EditorialStoryboard | None = None
) -> list[ScenePlan]:
    """Return the same directed scenes through the strict cross-layer contract.

    The renderer still accepts the historical dictionary representation for API
    compatibility, while new callers can validate every scene before rendering.
    """
    raw = build_scene_plan(trace, storyboard=storyboard)
    typed: list[ScenePlan] = []
    for index, scene in enumerate(raw):
        event = next(event for event in trace.events if event.id == scene["event_id"])
        editorial = next(
            (item for item in (storyboard.scenes if storyboard else []) if item.id == scene.get("editorial_scene_id")),
            None,
        )
        typed.append(
            ScenePlan(
                id=str(scene.get("editorial_scene_id") or f"scene-{index + 1}"),
                story_phase=(editorial.story_phase if editorial else "explain"),
                page_url=event.page_url or "about:blank",
                evidence_refs=[event.screenshot_path or f"event:{event.id}"],
                operation_ids=[event.operation_id],
                required_content=(editorial.visible_proof if editorial else [event.intent]),
                required_content_groups=list(event.required_content_groups),
                covered_content_groups=list(event.covered_content_groups),
                required_dwell_seconds=float(scene["dwell_seconds"]),
                completion_criteria=list(scene["completion_criteria"]),
                action_classification=(editorial.action_classification if editorial else "essential"),
                caption_intent=(editorial.narration if editorial else event.intent),
            )
        )
    return typed
