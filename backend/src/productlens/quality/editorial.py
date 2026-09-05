"""Quality gates for evidence-grounded editorial walkthroughs."""

from __future__ import annotations

import re

from productlens.contracts.models import (
    DemoPlan,
    DemoTrace,
    EditorialStoryboard,
    OperationKind,
    ProductContext,
)
from productlens.presentation.editorial import _fact_id, _scene_source, _viewer_ready


def inspect_editorial(
    *, context: ProductContext, plan: DemoPlan, trace: DemoTrace, storyboard: EditorialStoryboard | None, script: list[dict],
) -> dict:
    if storyboard is None:
        return {"editorial_score": 1.0, "hard_failures": [], "warnings": ["EDITORIAL_STORYBOARD_UNAVAILABLE"], "scenes": []}
    failures: list[str] = []
    warnings: list[str] = []
    events = {event.operation_id: event for event in trace.events if event.success}
    scenes = []
    opening = storyboard.scenes[0] if storyboard.scenes else None
    opening_words = set(re.findall(r"[a-z0-9]{4,}", (opening.narration if opening else "").lower()))
    if opening is None or opening.interaction != "opening" or opening.required_dwell_seconds < 4:
        failures.append("OPENING_PAGE_NOT_ESTABLISHED")
    if trace.recording_started_at and trace.events:
        opening_seconds = (trace.events[0].occurred_at - trace.recording_started_at).total_seconds()
        if opening_seconds + 0.25 < (opening.required_dwell_seconds if opening else 4):
            failures.append("OPENING_PAGE_ADVANCED_TOO_EARLY")
    returned_home = False
    initial_url = context.url.rstrip("/")
    for index, step in enumerate(plan.workflow_steps):
        op = step.operation
        if index and op.kind is OperationKind.NAVIGATE and str(op.value).rstrip("/") == initial_url:
            returned_home = True
        if index and op.kind is OperationKind.NAVIGATE:
            destination = str(op.value).rstrip("/")
            if any((item.href or "").rstrip("/") and destination.endswith((item.href or "").rstrip("/")) for item in context.navigation):
                failures.append("DIRECT_ROUTE_USED_WHERE_VISIBLE_NAVIGATION_EXISTS")
                break
    if returned_home:
        failures.append("RETURNED_TO_COVERED_OPENING_PAGE")
    # A navigation chapter is not a demonstration until the destination has a
    # page-local reveal or meaningful inspection before the next navigation.
    operations = [step.operation for step in plan.workflow_steps]
    for index, operation in enumerate(operations):
        if operation.kind is not OperationKind.OPEN_NAVIGATION_ITEM:
            continue
        following = operations[index + 1 :]
        chapter = []
        for candidate in following:
            if candidate.kind in {OperationKind.OPEN_NAVIGATION_ITEM, OperationKind.NAVIGATE}:
                break
            chapter.append(candidate)
        exploration_kinds = {OperationKind.SCROLL_TO, OperationKind.CLICK, OperationKind.OPEN_MODAL, OperationKind.READ_VALUE, OperationKind.FILL_TEXT, OperationKind.FILL_EMAIL, OperationKind.FILL_PHONE, OperationKind.SELECT_OPTION, OperationKind.SUBMIT, OperationKind.APPLY_FILTER}
        if not any(candidate.kind in exploration_kinds for candidate in chapter):
            failures.append("NAVIGATED_PAGE_NOT_EXPLORED")
    # Planning local exploration is insufficient on its own. A delivered trace
    # must prove that each navigation produced a later, successful page-local
    # scene before the next navigation began.
    successful_events = [event for event in trace.events if event.success]
    for index, event in enumerate(successful_events):
        if event.kind is not OperationKind.OPEN_NAVIGATION_ITEM:
            continue
        chapter = []
        for following in successful_events[index + 1 :]:
            if following.kind in {OperationKind.OPEN_NAVIGATION_ITEM, OperationKind.NAVIGATE}:
                break
            chapter.append(following)
        if not any(item.kind in exploration_kinds for item in chapter):
            failures.append("TRACE_NAVIGATED_PAGE_NOT_EXPLORED")
    script_by_event = {str(line.get("event_id")): str(line.get("text", "")) for line in script}
    first_executed_operation = next(
        (scene.operation_id for scene in storyboard.scenes if scene.operation_id is not None), None
    )
    for scene in storyboard.scenes:
        if scene.operation_id is None:
            continue
        event = events.get(scene.operation_id)
        if event is None:
            failures.append("EDITORIAL_SCENE_NOT_EXECUTED")
            continue
        # Browser navigation/video instrumentation introduces a small clock
        # boundary around an otherwise completed scene hold. Keep the reading
        # dwell strict while avoiding a false repair for sub-quarter-second
        # scheduler jitter.
        if event.duration_ms + 250 < int(scene.required_dwell_seconds * 1000):
            failures.append("SCENE_ADVANCED_BEFORE_REQUIRED_DWELL")
        text = script_by_event.get(event.id, "")
        if not text:
            failures.append("EDITORIAL_SCENE_MISSING_NARRATION")
        else:
            target_words = set(re.findall(r"[a-z0-9]{4,}", scene.title.lower()))
            caption_words = set(re.findall(r"[a-z0-9]{4,}", text.lower()))
            generic_words = {"explored", "context", "gives", "viewer", "concrete", "evidence", "next", "part", "walkthrough"}
            title_only = bool(target_words) and not (caption_words - target_words - generic_words)
            # The first proved page scene carries the bounded presenter
            # welcome authored from the opening evidence.  It is intentionally
            # connective rather than a route-label sentence, and must not be
            # rejected by a helper designed for later factual scenes.
            presenter_opening = (
                scene.operation_id == first_executed_operation
                and text.lower().startswith("welcome to ")
                and len(text.split()) >= 12
                and bool(target_words & caption_words)
            )
            boilerplate = (
                "is explored in context",
                "gives the viewer concrete evidence",
                "it sets up the details we explore next",
                "walkthrough pauses here",
                "before moving on",
                "comes into focus",
                "visible detail is clear",
                "bespoke command interface",
                "section is now visible",
                "section visibly presents",
                "section brings the visible details together",
                " step is shown on ",
            )
            repeated_opening = (
                bool(opening_words)
                and ("\n" in scene.title or bool(re.search(r"\b\d+\b", scene.title)))
                and len(caption_words & opening_words) / max(len(caption_words), 1) >= 0.72
            )
            if (
                len(text.split()) < 6
                or (not presenter_opening and not _viewer_ready(text, scene.title))
                or "distinct part of the product experience" in text
                or any(phrase in text.lower() for phrase in boilerplate)
                or repeated_opening
                or title_only
            ):
                failures.append("GENERIC_ROUTE_LABEL_CAPTION")
            evidence_text = _scene_source(context, scene)
            evidence_words = set(re.findall(r"[a-z0-9]{4,}", evidence_text.lower()))
            operation = next(
                (step.operation for step in plan.workflow_steps if step.operation.id == scene.operation_id),
                None,
            )
            target_words = set(
                re.findall(r"[a-z0-9]{4,}", operation.target.name.lower())
            ) if operation and operation.target else set()
            fact_words: set[str] = set()
            for page in context.page_knowledge:
                if f"page:{page.url}" not in scene.evidence:
                    continue
                fact_words.update(
                    word
                    for fact in page.visible_facts
                    if _fact_id(page.url, fact) in scene.evidence
                    for word in re.findall(r"[a-z0-9]{4,}", fact.lower())
                )
            # Heading element text is intentionally excluded here: otherwise a
            # scene can cite its own title while narrating a fact from another
            # card on the page.
            target_source_words = fact_words or evidence_words
            if (
                target_words
                and operation is not None
                and operation.kind not in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}
                and not (target_words & target_source_words)
            ):
                failures.append("SCENE_TARGET_EVIDENCE_MISMATCH")
            # A scene can use concise editorial connective language, but its
            # factual sentence must remain recognisably tied to that scene's
            # assigned page/card evidence—not another page in the run.
            if len(evidence_words) >= 3 and len(caption_words & evidence_words) < 2:
                failures.append("UNSUPPORTED_OR_WRONG_SCENE_CAPTION")
        scenes.append({"id": scene.id, "title": scene.title, "duration_ms": event.duration_ms, "evidence": scene.evidence, "interaction": scene.interaction})
    if len(trace.events) < len([scene for scene in storyboard.scenes if scene.operation_id]):
        warnings.append("SOME_PLANNED_SCENES_DID_NOT_PRODUCE_EVIDENCE")
    return {"editorial_score": 1.0 if not failures else 0.0, "hard_failures": list(dict.fromkeys(failures)), "warnings": warnings, "scenes": scenes, "minimum_duration_seconds": storyboard.minimum_duration_seconds}
