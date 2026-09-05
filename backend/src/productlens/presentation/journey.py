"""Deterministic trace-to-video journey direction."""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from productlens.contracts.models import DemoTrace, OperationKind

CLICK_KINDS = {OperationKind.CLICK, OperationKind.OPEN_NAVIGATION_ITEM, OperationKind.OPEN_MODAL, OperationKind.CLOSE_MODAL, OperationKind.SUBMIT, OperationKind.APPLY_FILTER}


def _has_directed_scroll_motion(path: list[dict]) -> bool:
    """Distinguish an already-visible reading beat from a real scroll.

    A semantic ``ScrollTo`` is also the planner's safe way to establish that a
    landmark is readable.  If the target is already inside the reading region,
    Playwright correctly leaves scroll position unchanged.  Requiring motion
    in that case would reject valid coverage and encourage artificial
    scroll-away/scroll-back gestures.  Actual movement still requires both
    endpoints and a meaningful browser-owned displacement.
    """
    if len(path) < 2:
        return False
    start, end = path[0], path[-1]
    try:
        return abs(float(end.get("x", 0)) - float(start.get("x", 0))) > 8 or abs(
            float(end.get("y", 0)) - float(start.get("y", 0))
        ) > 8
    except (AttributeError, TypeError, ValueError):
        return False


def _page_key(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/", parsed.query, ""))


def build_journey(trace: DemoTrace, scenes: list[dict]) -> list[dict]:
    """Direct trace scenes into a page-complete editorial journey.

    Navigation is deliberately only an ``enter`` beat.  The final beat of a
    page chapter receives a completion record used by QA and rendering clients,
    making the page-level viewing contract explicit instead of silently treating
    a successful click as coverage.
    """
    by_event = {event.id: event for event in trace.events if event.success}
    directed: list[dict] = []
    for index, scene in enumerate(scenes):
        event = by_event.get(scene.get("event_id"))
        if event is None:
            continue
        if index == 0:
            phase, action_class = "context", "ESSENTIAL"
        elif event.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}:
            phase, action_class = "enter", "TRANSITIONAL"
        elif event.kind is OperationKind.SCROLL_TO:
            phase, action_class = "explore", "ESSENTIAL"
        elif event.kind in CLICK_KINDS:
            phase, action_class = "demonstrate", "ESSENTIAL"
        else:
            phase, action_class = "verify", "ESSENTIAL"
        rect = event.target_rect
        page_key = _page_key(event.page_url)
        existing_camera = scene.get("camera") if isinstance(scene.get("camera"), dict) else {}
        existing_cursor = scene.get("cursor") if isinstance(scene.get("cursor"), dict) else {}
        existing_scroll = scene.get("scroll") if isinstance(scene.get("scroll"), dict) else {}
        scroll_evidence = list(event.scroll_path)
        has_scroll_motion = _has_directed_scroll_motion(scroll_evidence)
        directed.append({
            **scene,
            "story_phase": phase,
            "action_class": action_class,
            "camera": {"zoom": 1.0, "duration_seconds": 0.45, "easing": "out-cubic", "safety": "full-frame", **existing_camera},
            "cursor": {"visible": event.kind in CLICK_KINDS, "hover_seconds": 0.25, "click_ripple": event.kind in CLICK_KINDS, **existing_cursor},
            "scroll": {
                "mode": "gradual" if event.kind is OperationKind.SCROLL_TO and has_scroll_motion else "none",
                "settle_seconds": 0.45,
                "already_visible": event.kind is OperationKind.SCROLL_TO and not has_scroll_motion,
                **existing_scroll,
            },
            "target": rect.model_dump() if rect else None,
            "page_key": page_key,
            "scroll_evidence": scroll_evidence,
            "scroll_continuity_required": (
                event.kind is OperationKind.SCROLL_TO
                and event.action_at is not None
                and has_scroll_motion
            ),
        })
    # Attach the complete, auditable page contract to each chapter's final
    # visible scene. A scroll with a readable dwell counts as inspecting the
    # visible evidence; a navigation event never does.
    for index, scene in enumerate(directed):
        page_key = scene.get("page_key")
        if not page_key:
            continue
        next_page = directed[index + 1].get("page_key") if index + 1 < len(directed) else None
        if page_key == next_page:
            continue
        chapter = [item for item in directed if item.get("page_key") == page_key]
        # ``page_stage`` carries the editorial role authored by the planner
        # (often ``explain`` for every scene).  It must not mask the directed
        # journey phase: an ``enter`` beat is the explicit establishment proof
        # for a newly opened page even when its source scene has page_stage set
        # to ``explain``.
        stages = {
            str(stage)
            for item in chapter
            for stage in (
                item.get("page_stage"), item.get("story_phase"),
                *(item.get("page_contract_phases") or []),
            )
            if stage
        }
        has_visible_exploration = any(
            item.get("story_phase") in {"explore", "demonstrate", "verify"}
            or item.get("page_stage") in {"explore", "inspect", "explain", "demonstrate", "verify"}
            or bool(set(item.get("page_contract_phases") or []) & {"explore", "demonstrate", "verify"})
            for item in chapter
        )
        readable_dwell = sum(float(item.get("dwell_seconds", 0)) for item in chapter if item.get("story_phase") != "enter")
        required_groups = list(dict.fromkeys(
            group
            for item in chapter
            for group in item.get("required_content_groups", [])
            if str(group).strip()
        ))
        covered_groups = list(dict.fromkeys(
            group
            for item in chapter
            for group in item.get("covered_content_groups", [])
            if str(group).strip()
        ))
        scene["page_completion"] = {
            # Entering a page has an explicit readiness/reveal dwell in the
            # scene contract, so it establishes that page before its first
            # local exploration. The opening page uses the context beat.
            "established": "context" in {item.get("story_phase") for item in chapter} or "establish" in stages or "enter" in stages,
            "explored": has_visible_exploration,
            "explained": "explain" in stages or any(item.get("page_stage") in {"inspect", "explain", "demonstrate", "verify"} for item in chapter) or readable_dwell >= 1.5,
            "demonstrated_or_inspected": has_visible_exploration,
            "verified_takeaway": "verify" in stages or any(item.get("story_phase") == "verify" or item.get("page_stage") == "verify" for item in chapter) or readable_dwell >= 1.5,
            "readable_dwell_seconds": round(readable_dwell, 2),
            "required_content_groups": required_groups,
            "covered_content_groups": covered_groups,
            "missing_content_groups": [group for group in required_groups if group not in covered_groups],
            "transition": "end" if index == len(directed) - 1 else "next-page",
        }
    return directed


def inspect_journey(scenes: list[dict]) -> dict:
    failures: list[str] = []
    if not scenes or scenes[0].get("story_phase") != "context":
        failures.append("JOURNEY_MISSING_CONTEXT")
    if any(scene.get("action_class") == "DEAD_TIME" for scene in scenes):
        failures.append("UNCOMPRESSED_DEAD_TIME")
    # The opening page is already a completed chapter once the first context
    # beat has been established. A later navigation back to it is a coverage
    # repair, not a new story chapter.
    completed_pages: set[str] = {
        str(scenes[0]["page_key"])
    } if scenes and scenes[0].get("page_key") else set()
    for index, scene in enumerate(scenes):
        if scene.get("story_phase") != "enter":
            continue
        chapter = []
        for following in scenes[index + 1 :]:
            if following.get("story_phase") == "enter":
                break
            chapter.append(following)
        if not any(item.get("story_phase") in {"explore", "demonstrate", "verify"} for item in chapter):
            failures.append("JOURNEY_HAS_NAVIGATION_WITHOUT_EXPLORATION")
            continue
        terminal = next((item for item in reversed(chapter) if item.get("page_completion")), None)
        completion = terminal.get("page_completion", {}) if terminal else {}
        if completion and not all(bool(completion.get(key)) for key in ("established", "explored", "explained", "demonstrated_or_inspected", "verified_takeaway")):
            failures.append("JOURNEY_INCOMPLETE_PAGE_CONTRACT")
        if completion.get("missing_content_groups"):
            failures.append("JOURNEY_REQUIRED_CONTENT_GROUP_NOT_SHOWN")
        page_key = scene.get("page_key")
        if page_key and page_key in completed_pages:
            failures.append("JOURNEY_REVISITS_COMPLETED_PAGE")
            continue
        if page_key:
            completed_pages.add(page_key)
    # A ScrollTo scene must carry browser-owned path evidence. The renderer is
    # allowed to cut remote dead time, never the movement interval itself.
    for scene in scenes:
        if scene.get("scroll_continuity_required"):
            path = scene.get("scroll_evidence") or []
            if len(path) < 2 or path[0] == path[-1]:
                failures.append("JOURNEY_SCROLL_WITHOUT_CONTINUITY_EVIDENCE")
    return {"journey_score": 1.0 if not failures else 0.0, "hard_failures": failures, "scene_count": len(scenes)}
