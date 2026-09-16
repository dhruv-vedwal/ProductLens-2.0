"""Human-paced, page-complete scene direction from verified browser evidence.

The execution trace is an audit trail, not a screenplay.  A rendered scene is
therefore given a page-local role and an explicit completion contract so a tab
cannot be considered covered simply because its navigation click succeeded.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from productlens.contracts.models import DemoTrace, EditorialStoryboard, OperationKind, ScenePlan


def _page_key(url: str | None) -> str:
    if not url:
        return "unknown"
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", parts.query, "")
    )


def build_scene_plan(
    trace: DemoTrace, *, storyboard: EditorialStoryboard | None = None
) -> list[dict[str, Any]]:
    scenes: list[dict[str, Any]] = []
    # A page that has a directed local scroll is content-dense by definition.
    # Its opening/explanation captions must not sit over the lower rows/cards
    # that the following scroll is meant to reveal. This is derived from the
    # trace's own page-local evidence, never from a product or route name.
    page_has_scroll: set[str] = {
        _page_key(event.page_url)
        for event in trace.events
        if event.success and event.kind is OperationKind.SCROLL_TO
    }
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
        editorial = next(
            (
                scene
                for scene in (storyboard.scenes if storyboard else [])
                if scene.operation_id == event.operation_id
            ),
            None,
        )
        if editorial:
            # An editorial scene has stronger semantic information than an
            # event type. Preserve its phase while retaining page-local stage.
            phase = editorial.story_phase
            if phase in {
                "context",
                "enter",
                "explain",
                "demonstrate",
                "verify",
                "transition",
                "close",
            }:
                page_stage = {
                    "context": "establish",
                    "enter": "enter",
                    "explain": "explain",
                    "demonstrate": "demonstrate",
                    "verify": "verify",
                    "transition": "transition",
                    "close": "transition",
                }[phase]
        # Captions are explanatory, never an opaque obstruction. A target in
        # the lower portion of the recorded viewport gets the top safe zone;
        # otherwise the bottom overlay preserves the product's heading/nav.
        caption_safe_zone = editorial.caption_safe_zone if editorial else "bottom"
        # Navigation captions describe the newly loaded page after the click,
        # not the tiny tab that was clicked.  Keep that explanation above the
        # page body so a dense result/list below is never hidden while the
        # route transition is being introduced.  This is deliberately based
        # on semantic operation role rather than a product route or label.
        if is_navigation and page_stage == "enter":
            caption_safe_zone = "top"
        if page_key in page_has_scroll and page_stage in {"establish", "explain", "inspect"}:
            caption_safe_zone = "top"
        if event.target_rect is not None and event.viewport is not None:
            target_center_y = event.target_rect.y + event.target_rect.height / 2
            # Form controls and centered dialogs often sit just below the
            # midpoint. A bottom caption then obscures the submit/result area
            # even though it does not overlap the active field itself. Use the
            # opposite half of the frame with a small safety margin so the
            # surrounding workflow remains readable.
            # Use a conservative upper-third boundary for scroll/reveal
            # targets. A heading near 30–40% of the viewport usually anchors
            # a long card/list whose meaningful rows continue below it; a
            # bottom caption would cover that evidence even though the
            # heading's midpoint is above the geometric half of the frame.
            if target_center_y >= event.viewport.height * 0.30:
                caption_safe_zone = "top"
            # A page heading/filter near the upper edge usually introduces a
            # long list or table below it.  Keeping the caption at the bottom
            # in that case hides the very evidence the scene is explaining
            # (the common failure mode on dense pages whose first reveal is
            # already visible).  Use geometry and scene role only; no route or
            # product-specific assumptions are needed.
            if (
                page_stage in {"explain", "inspect"}
                and event.target_rect.y <= event.viewport.height * 0.28
                and (
                    event.target_rect.width >= event.viewport.width * 0.22
                    or bool(event.required_content_groups)
                )
            ):
                caption_safe_zone = "top"
        # A directed scroll often reveals a compact card or capability whose
        # text is otherwise needlessly small inside a wide, source-faithful
        # desktop capture. It earns a modest editorial reframe only when the
        # recorded geometry proves that it is a bounded, visible reading
        # target. Page navigation, broad sections, and off-canvas geometry
        # deliberately retain the complete frame.
        compact_reading_target = bool(
            kind is OperationKind.SCROLL_TO
            and event.target_rect is not None
            and event.viewport is not None
            and 96 <= event.target_rect.width <= event.viewport.width * 0.55
            and event.target_rect.height <= min(112, event.viewport.height * 0.16)
            and event.target_rect.x >= 0
            and event.target_rect.y >= 0
            and event.target_rect.x + event.target_rect.width <= event.viewport.width
            and event.target_rect.y + event.target_rect.height <= event.viewport.height
        )
        scenes.append(
            {
                "event_id": event.id,
                "scene_id": editorial.id if editorial else f"trace-{event.id}",
                "intent": event.intent,
                "phase": phase,
                "page_stage": page_stage,
                "page_key": page_key,
                "lead_seconds": lead,
                "dwell_seconds": editorial.required_dwell_seconds if editorial else dwell,
                # Navigation is still a real human click. Hiding the overlay for
                # every transition made tab changes look disconnected from the
                # recorded gesture and masked cursor/target QA.
                "show_cursor": kind
                in {
                    OperationKind.CLICK,
                    OperationKind.OPEN_NAVIGATION_ITEM,
                    OperationKind.OPEN_MODAL,
                    OperationKind.CLOSE_MODAL,
                    OperationKind.SUBMIT,
                    OperationKind.APPLY_FILTER,
                    OperationKind.DRAG,
                    OperationKind.POINTER_SEQUENCE,
                },
                "caption_safe_zone": caption_safe_zone,
                "editorial_scene_id": editorial.id if editorial else None,
                "completion_criteria": editorial.completion_criteria
                if editorial
                else ["verified operation completed"],
                "required_content": editorial.visible_proof if editorial else [event.intent],
                "required_content_groups": list(event.required_content_groups),
                "covered_content_groups": list(event.covered_content_groups),
                "page_contract_phases": list(event.page_contract_phases),
                "page_contract": {
                    "page": page_key,
                    "stage": page_stage,
                    "required": [
                        "establish",
                        "explore",
                        "explain",
                        "demonstrate_or_inspect",
                        "verify",
                        "transition",
                    ],
                    "complete_only_after": "visible evidence is held for the required dwell and the scene completion criteria are met",
                },
                "camera": {
                    # Focus is an editorial exception. A form field being filled
                    # is compact, actionable evidence; headings, page transitions
                    # and ordinary reading retain the source-faithful full frame.
                    "mode": "target-focus"
                    if compact_reading_target
                    or kind
                    in {
                        OperationKind.FILL_TEXT,
                        OperationKind.FILL_EMAIL,
                        OperationKind.FILL_PHONE,
                        OperationKind.SELECT_OPTION,
                        OperationKind.SELECT_DATE,
                        OperationKind.SELECT_DATE_RANGE,
                    }
                    else "full-frame",
                    "reason": "compact evidence region is being explained with surrounding context preserved"
                    if compact_reading_target
                    else "compact actionable control is being demonstrated"
                    if kind
                    in {
                        OperationKind.FILL_TEXT,
                        OperationKind.FILL_EMAIL,
                        OperationKind.FILL_PHONE,
                        OperationKind.SELECT_OPTION,
                        OperationKind.SELECT_DATE,
                        OperationKind.SELECT_DATE_RANGE,
                    }
                    else "page context is more valuable than target magnification",
                    "duration_seconds": 0.45,
                    # A form scene is the one place where the viewer needs to
                    # follow a value being entered. Give it a meaningful but
                    # bounded reframe; broad page reading retains the full frame.
                    "zoom": 1.14
                    if kind
                    in {
                        OperationKind.FILL_TEXT,
                        OperationKind.FILL_EMAIL,
                        OperationKind.FILL_PHONE,
                        OperationKind.SELECT_OPTION,
                        OperationKind.SELECT_DATE,
                        OperationKind.SELECT_DATE_RANGE,
                    }
                    else 1.10
                    if compact_reading_target
                    else 1.0,
                    "easing": "out-cubic",
                    "safety_bounds": "preserve-browser-frame",
                },
                "cursor": {
                    "mode": "natural-path",
                    "pre_action_pause_seconds": 0.25,
                    "click_at_target_center": True,
                },
                "scroll": {
                    "mode": "directed" if kind is OperationKind.SCROLL_TO else "none",
                    "continuity": "intermediate-landmarks-and-settle"
                    if kind is OperationKind.SCROLL_TO
                    else "not-applicable",
                    "settle_seconds": 0.55 if kind is OperationKind.SCROLL_TO else 0.3,
                },
            }
        )
    return scenes


def inspect_scene_plan(trace: DemoTrace, scenes: list[dict[str, Any]]) -> dict[str, Any]:
    failures: list[str] = []
    event_ids = [event.id for event in trace.events if event.success]
    if [scene.get("event_id") for scene in scenes] != event_ids:
        failures.append("SCENE_TRACE_MISMATCH")
    if any(float(scene.get("dwell_seconds", 0)) < 1.0 for scene in scenes):
        failures.append("INSUFFICIENT_SCENE_DWELL")
    return {
        "editorial_score": 1.0 if not failures else 0.0,
        "hard_failures": failures,
        "scene_count": len(scenes),
    }


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
            (
                item
                for item in (storyboard.scenes if storyboard else [])
                if item.id == scene.get("editorial_scene_id")
            ),
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
                action_classification=(
                    editorial.action_classification if editorial else "essential"
                ),
                caption_intent=(editorial.narration if editorial else event.intent),
                transition=(
                    editorial.transition
                    if editorial
                    and editorial.transition in {"cut", "dissolve", "match_scroll", "hold"}
                    else "match_scroll"
                    if event.kind is OperationKind.SCROLL_TO
                    else "cut"
                ),
            )
        )
    return typed
