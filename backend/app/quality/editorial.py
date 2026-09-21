"""Quality gates for evidence-grounded editorial walkthroughs."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from app.contracts.models import (
    DemoPlan,
    DemoTrace,
    EditorialStoryboard,
    OperationKind,
    ProductContext,
)
from app.presentation.editorial import (
    _SENSITIVE_EDITORIAL_PATTERN,
    _fact_id,
    _looks_like_screen_transcript,
    _scene_source,
    _viewer_ready,
    narrated_storyboard_scenes,
)
from app.urls import canonical_product_url


def _canonical_navigation_url(base: str, value: str) -> str:
    """Normalize a navigation URL for source-page-aware editorial checks."""
    return canonical_product_url(urljoin(base, value))


def _operation_value_leak(text: str, operation: object | None) -> bool:
    """Return whether prose repeats an input/selection literal.

    Editorial narration should explain the role of a field or choice.  Raw
    values are execution evidence (and can contain synthetic PII), not a
    useful presenter line.  Keep this generic by checking the immutable
    semantic operation value, never a product or route name.
    """
    if operation is None or getattr(operation, "kind", None) not in {
        OperationKind.FILL_TEXT,
        OperationKind.FILL_EMAIL,
        OperationKind.FILL_PHONE,
        OperationKind.SELECT_OPTION,
        OperationKind.SELECT_DATE,
        OperationKind.SELECT_DATE_RANGE,
        OperationKind.SEARCH,
    }:
        return False
    value = str(getattr(operation, "value", "") or "").strip()
    if len(value) < 4 or value.startswith("secret://"):
        return False
    normalized = " ".join(value.casefold().split())
    prose = " ".join(text.casefold().split())
    # Avoid flagging a common field label that happens to equal a short value.
    return normalized in prose and not re.fullmatch(r"(?:true|false|none|null)", normalized)


def inspect_editorial_preflight(
    *,
    context: ProductContext,
    plan: DemoPlan,
    storyboard: EditorialStoryboard | None,
) -> dict:
    """Reject a weak story before a production browser is ever opened.

    Trace QA remains the final authority for timing and visual fidelity, but a
    plan already knows enough to reject route-label narration, a missing
    welcome, and an omitted required configuration/feature relationship. This
    prevents spending a cloud recording on an editorial failure that cannot be
    repaired by execution.
    """
    if storyboard is None or not storyboard.scenes:
        return {
            "editorial_score": 0.0,
            "hard_failures": ["EDITORIAL_STORYBOARD_UNAVAILABLE"],
            "warnings": [],
        }
    failures: list[str] = []
    warnings: list[str] = []
    opening = storyboard.scenes[0]
    opening_words = set(re.findall(r"[a-z0-9]{3,}", opening.narration.casefold()))
    if opening.interaction != "opening" or opening.required_dwell_seconds < 4:
        failures.append("OPENING_PAGE_NOT_ESTABLISHED")
    if not opening.narration.casefold().startswith("welcome to"):
        failures.append("OPENING_PRESENTER_WELCOME_MISSING")
    # Fail before Browserbase execution when enrich left a wall of skeletons.
    non_opening = [scene for scene in storyboard.scenes if scene.operation_id is not None]
    skeleton_count = sum(
        1
        for scene in non_opening
        if "control in focus for this step on screen" in scene.narration.casefold()
    )
    if skeleton_count >= 2:
        failures.append("REPETITIVE_EDITORIAL_NARRATION")
    objective = context.objective
    for relation in getattr(objective, "supporting_relationships", []) if objective else []:
        if not relation.required:
            continue
        source = set(re.findall(r"[a-z0-9]{3,}", relation.source.casefold()))
        target = set(re.findall(r"[a-z0-9]{3,}", relation.target.casefold())) - {
            "management",
            "workflow",
            "flow",
            "experience",
        }
        all_story_words = set(
            re.findall(
                r"[a-z0-9]{3,}",
                " ".join(scene.narration for scene in storyboard.scenes).casefold(),
            )
        )
        if source and not (source & all_story_words) or target and not (target & all_story_words):
            failures.append("REQUIRED_CONTEXT_RELATIONSHIP_NOT_EXPLAINED")
    generic_phrases = (
        "quick overview of key metrics",
        "giving you immediate insight",
        "various filters and details",
        "ready for management and action",
        "surrounding controls visible for context",
        "the next part of the walkthrough",
        "hands-on practice",
        "next build",
        "architecture cards",
        "observed details are read before the walkthrough continues",
        "presents its observed controls",
        "groups the product's available capabilities",
        "lets teams ",
        "this leaves the visible state established at the end of the walkthrough",
    )
    # These are the phrases that slipped through the original substring list
    # in live runs.  They describe the mechanics of the tour (a view opened,
    # a workflow being demonstrated) rather than the product value visible in
    # that scene.  Keep this gate generic so it applies to every product.
    generic_route_patterns = (
        r"\bthe\s+next\s+view\b",
        r"\bworkspace\s+establish(?:es|ing)\s+(?:the\s+)?current\s+working\s+view\b",
        r"\bvisible\s+workflow\s+is\s+demonstrated\b",
        r"\bprovides\s+the\s+setup\s+context\b.*\b(?:now\s+)?(?:we\s+)?can\s+see\b",
        r"\b(?:current|working)\s+view\s+before\s+we\s+demonstrate\b",
        r"\b(?:view|page)\s+brings\b.*\binto\s+view\b",
        r"\bshowing\s+how\s+this\s+part\s+of\s+the\s+product\s+is\s+organized\b",
    )
    plan_operations = {step.operation.id: step.operation for step in plan.workflow_steps}
    for scene in storyboard.scenes:
        text = scene.narration.casefold()
        if _SENSITIVE_EDITORIAL_PATTERN.search(scene.narration):
            failures.append("EDITORIAL_SENSITIVE_RECORD_DATA")
        if any(phrase in text for phrase in generic_phrases):
            failures.append("GENERIC_ROUTE_LABEL_CAPTION")
        if scene.operation_id is None:
            continue
        if any(re.search(pattern, text) for pattern in generic_route_patterns):
            failures.append("GENERIC_ROUTE_LABEL_CAPTION")
        operation = plan_operations.get(scene.operation_id)
        if operation is None:
            # Authentication beats are synthetic presentation scenes emitted
            # only when the objective requires login. They have no semantic
            # workflow operation (credentials never enter the plan), but are
            # still valid evidence-bound steps in the approved storyboard.
            if scene.operation_id.startswith("auth:"):
                continue
            failures.append("EDITORIAL_SCENE_NOT_IN_PLAN")
            continue
        # A scroll without a target-specific fact is typically a fabricated
        # coverage gesture. The planner should use a verified reading hold or
        # a real form/action scene instead.
        if operation.kind is OperationKind.SCROLL_TO:
            source = _scene_source(context, scene)
            # Evidence is frequently captured from accessibility/DOM text in
            # title case or all caps (for example a stack category). Normalize
            # before measuring it; the previous lower-case-only expression
            # counted only the tails of capitalized words and rejected valid
            # page-local evidence during live planning.
            readable_source = source.casefold()
            # Some providers persist the page-local proof as section/fact
            # references while omitting the corresponding prose from the
            # compact element inventory.  Treat those explicit references as
            # readable evidence too; otherwise a legitimate scroll scene is
            # rejected merely because its evidence is represented by IDs.
            evidence_tokens = [
                str(ref)
                for ref in scene.evidence
                if str(ref).startswith(("section:", "fact:", "element:", "form-field:"))
            ]
            evidence_rich = len(evidence_tokens) >= 2
            if not _viewer_ready(scene.narration, scene.title) or (
                len(re.findall(r"[a-z0-9]{4,}", readable_source)) < 6 and not evidence_rich
            ):
                failures.append("SCROLL_SCENE_LACKS_READABLE_LOCAL_EVIDENCE")
        if scene.interaction in {"type", "submit"} and operation.kind not in {
            OperationKind.FILL_TEXT,
            OperationKind.FILL_EMAIL,
            OperationKind.FILL_PHONE,
            OperationKind.SELECT_OPTION,
            OperationKind.SELECT_DATE,
            OperationKind.SELECT_DATE_RANGE,
            OperationKind.SUBMIT,
        }:
            failures.append("EDITORIAL_INTERACTION_MISMATCH")
    return {
        "editorial_score": 1.0 if not failures else 0.0,
        "hard_failures": list(dict.fromkeys(failures)),
        "warnings": warnings,
        "opening_words": sorted(opening_words),
    }


def inspect_editorial(
    *,
    context: ProductContext,
    plan: DemoPlan,
    trace: DemoTrace,
    storyboard: EditorialStoryboard | None,
    script: list[dict],
) -> dict:
    if storyboard is None:
        return {
            "editorial_score": 1.0,
            "hard_failures": [],
            "warnings": ["EDITORIAL_STORYBOARD_UNAVAILABLE"],
            "scenes": [],
        }
    failures: list[str] = []
    warnings: list[str] = []
    narrated_texts: list[str] = []
    narrated_operations: list[object | None] = []
    events = {event.operation_id: event for event in trace.events if event.success}
    plan_operations = {step.operation.id: step.operation for step in plan.workflow_steps}
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
    # ``ProductContext.url`` is the last discovery page, not necessarily the
    # first page shown in production.  In authenticated workflows discovery
    # often ends on the requested form, while production intentionally visits
    # a supporting Settings page and then returns to that form.  Derive the
    # opening page from the first non-auth production event so this legitimate
    # continuation is not mistaken for a repair loop.
    initial_event_url = next(
        (
            str(event.page_url).rstrip("/")
            for event in trace.events
            if event.success and event.page_url and not str(event.operation_id).startswith("auth:")
        ),
        None,
    )
    initial_url = (initial_event_url or str(context.url)).rstrip("/")
    active_page = _canonical_navigation_url(context.url, initial_url)
    navigation_evidence = [
        item for item in context.navigation if item.href and not urlsplit(item.href).fragment
    ]
    for index, step in enumerate(plan.workflow_steps):
        op = step.operation
        if index and op.kind is OperationKind.NAVIGATE and str(op.value).rstrip("/") == initial_url:
            returned_home = True
        if op.kind is OperationKind.NAVIGATE:
            destination = _canonical_navigation_url(active_page, str(op.value or ""))
            # The first production load establishes the requested URL; there
            # cannot be a preceding in-product control that should replace
            # that opening navigation.  Applying the visible-control rule to
            # this bootstrap step falsely rejected valid runs when the home
            # page itself exposed a link back to the root.
            if index == 0:
                active_page = destination
                continue
            # A direct route is only invalid when an equivalent visible
            # control was observed on the page that is active immediately
            # before this transition.  The previous global suffix check could
            # mistake an identically named link on another page (or an
            # unrelated footer) for proof that this route had a visible
            # alternative.
            has_visible_control = any(
                _canonical_navigation_url(item.source_url or active_page, item.href or "")
                == destination
                and _canonical_navigation_url(item.source_url or active_page, "") == active_page
                for item in navigation_evidence
            )
            if has_visible_control:
                failures.append("DIRECT_ROUTE_USED_WHERE_VISIBLE_NAVIGATION_EXISTS")
                break
            active_page = destination
        elif op.kind is OperationKind.OPEN_NAVIGATION_ITEM:
            destination = next(
                (
                    str(condition.expected)
                    for condition in op.postconditions
                    if condition.kind == "url"
                ),
                None,
            )
            if destination:
                active_page = _canonical_navigation_url(active_page, destination)
    # A supporting configuration page may be followed by a return to the
    # feature workspace when the validated flow still has an essential
    # mutation/inspection to perform.  That is a planned continuation, not a
    # coverage repair.  Retain the hard failure for returns that only replay
    # already-covered opening content.
    return_has_essential_followup = False
    if returned_home:
        for index, step in enumerate(plan.workflow_steps):
            op = step.operation
            if op.kind is not OperationKind.NAVIGATE or str(op.value).rstrip("/") != initial_url:
                continue
            return_has_essential_followup = any(
                later.operation.kind
                in {
                    OperationKind.OPEN_MODAL,
                    OperationKind.CLICK,
                    OperationKind.FILL_TEXT,
                    OperationKind.FILL_EMAIL,
                    OperationKind.FILL_PHONE,
                    OperationKind.SELECT_OPTION,
                    OperationKind.SELECT_DATE,
                    OperationKind.SELECT_DATE_RANGE,
                    OperationKind.SUBMIT,
                    OperationKind.APPLY_FILTER,
                }
                for later in plan.workflow_steps[index + 1 :]
            )
            if return_has_essential_followup:
                break
    if returned_home and not return_has_essential_followup:
        failures.append("RETURNED_TO_COVERED_OPENING_PAGE")
    # A create-capable objective is not demonstrated by opening a form or
    # leaving its save button visible. Require a submitted operation with an
    # independent result assertion before a delivery can claim the outcome.
    authorized_creation = bool(
        context.objective and "create_isolated_record" in context.objective.permitted_mutations
    )
    submit_operations = [
        step.operation
        for step in plan.workflow_steps
        if step.operation.kind is OperationKind.SUBMIT
    ]
    if authorized_creation and not submit_operations:
        failures.append("AUTHORIZED_CREATION_NOT_DEMONSTRATED")
    for operation in submit_operations:
        independent_proof = any(
            condition.kind in {"url", "text", "test_state"}
            or (
                condition.kind == "visible"
                and condition.target is not None
                and (
                    operation.target is None
                    or condition.target.name.casefold() != operation.target.name.casefold()
                )
            )
            for condition in operation.postconditions
        )
        if not independent_proof:
            failures.append("SUBMIT_WITHOUT_INDEPENDENT_OUTCOME_PROOF")
    # A navigation chapter is not a demonstration until the destination has a
    # page-local reveal or meaningful inspection before the next navigation.
    operations = [step.operation for step in plan.workflow_steps]
    form_targets = {
        (item.selector, " ".join(item.name.split()).casefold())
        for item in context.elements
        if (item.tag or "").casefold() in {"input", "select", "textarea"}
    }

    def is_page_exploration(operation) -> bool:
        explicit = {
            OperationKind.SCROLL_TO,
            OperationKind.CLICK,
            OperationKind.OPEN_MODAL,
            OperationKind.READ_VALUE,
            OperationKind.FILL_TEXT,
            OperationKind.FILL_EMAIL,
            OperationKind.FILL_PHONE,
            OperationKind.SELECT_OPTION,
            OperationKind.SUBMIT,
            OperationKind.APPLY_FILTER,
        }
        if operation.kind in explicit:
            return True
        # A read-only inspection of a page-local form/state is meaningful only
        # when the compiler marks it as an explore/explain beat. A title-only
        # VerifyState remains insufficient and cannot pass this gate.
        phases = set(getattr(operation, "page_contract_phases", []))
        evidence_refs = list(getattr(operation, "evidence_refs", []))
        target = getattr(operation, "target", None)
        target_is_form_control = bool(
            target is not None
            and (target.selector, " ".join(target.name.split()).casefold()) in form_targets
        )
        return (
            operation.kind is OperationKind.VERIFY_STATE
            and bool({"explore", "explain", "demonstrate", "verify"} & phases)
            and (
                # A validated content group is page-local evidence even when
                # the extraction pass represented it as a section/fact rather
                # than an ``element:`` reference.  Requiring a particular
                # evidence prefix made otherwise grounded documentation and
                # dashboard pages fail after successful production inspection.
                bool(getattr(operation, "required_content_groups", []))
                or any(str(ref).startswith(("form-field:", "element:")) for ref in evidence_refs)
                or target_is_form_control
            )
        )

    for index, operation in enumerate(operations):
        if operation.kind is not OperationKind.OPEN_NAVIGATION_ITEM:
            continue
        following = operations[index + 1 :]
        chapter = []
        for candidate in following:
            if candidate.kind in {OperationKind.OPEN_NAVIGATION_ITEM, OperationKind.NAVIGATE}:
                break
            chapter.append(candidate)
        if not any(is_page_exploration(candidate) for candidate in chapter):
            failures.append("NAVIGATED_PAGE_NOT_EXPLORED")
    # Planning local exploration is insufficient on its own. A delivered trace
    # must prove that each navigation produced a later, successful page-local
    # scene before the next navigation began.
    successful_events = [event for event in trace.events if event.success]
    for event in successful_events:
        if event.kind is not OperationKind.SCROLL_TO:
            continue
        motion = event.after.get("scroll_motion") if isinstance(event.after, dict) else None
        if not isinstance(motion, dict):
            # Older/local traces may not expose browser motion telemetry; do
            # not invent a failure from absent evidence. New production traces
            # must include it whenever a directed scroll was dispatched.
            continue
        start = float(motion.get("start_y", event.scroll_path[0]["y"] if event.scroll_path else 0))
        end = float(
            motion.get("target_y", event.scroll_path[-1]["y"] if event.scroll_path else start)
        )
        path = motion.get("path", event.scroll_path)
        if abs(end - start) < 2:
            # A target already visible in the settled viewport is a valid
            # page-local establish/inspect beat; forcing a synthetic scroll
            # would make footage less human, not more continuous. Only reject
            # a no-op when the target is not visibly established.
            rect = event.target_rect
            viewport = event.viewport
            if rect is not None and viewport is not None and 0 <= rect.y <= viewport.height:
                continue
            failures.append("SCROLL_SCENE_HAS_NO_CONTINUOUS_MOTION")
            continue
        if not isinstance(path, list) or len(path) < 2:
            failures.append("SCROLL_SCENE_HAS_NO_CONTINUOUS_MOTION")
    for index, event in enumerate(successful_events):
        if event.kind is not OperationKind.OPEN_NAVIGATION_ITEM:
            continue
        chapter = []
        for following in successful_events[index + 1 :]:
            if following.kind in {OperationKind.OPEN_NAVIGATION_ITEM, OperationKind.NAVIGATE}:
                break
            chapter.append(following)
        if not any(is_page_exploration(item) for item in chapter):
            failures.append("TRACE_NAVIGATED_PAGE_NOT_EXPLORED")
    script_by_event = {str(line.get("event_id")): str(line.get("text", "")) for line in script}
    script_by_moment = {
        str(line.get("moment_id")): str(line.get("text", ""))
        for line in script
        if line.get("moment_id")
    }
    moment_text_by_event = {
        event_id: script_by_moment.get(moment.id, "")
        for moment in trace.moments
        for event_id in moment.event_ids
    }
    narrated_operation_ids = {
        str(scene.operation_id)
        for scene in narrated_storyboard_scenes(storyboard)
        if scene.operation_id is not None
    }
    opening_event_ids = {
        str(line.get("event_id")) for line in script if bool(line.get("opening", False))
    }
    # The first planned operation may be an equivalent bootstrap Navigate that
    # was intentionally skipped because the clean context was already at the
    # canonical URL.  Presenter-opening checks must bind to the first *actual*
    # successful browser event, otherwise a valid opening caption is rejected
    # as generic merely because its skipped navigation has no trace event.
    first_executed_operation = next(
        (event.operation_id for event in successful_events if event.operation_id is not None),
        None,
    )
    for scene in storyboard.scenes:
        if _SENSITIVE_EDITORIAL_PATTERN.search(scene.narration):
            failures.append("EDITORIAL_SENSITIVE_RECORD_DATA")
        if scene.operation_id is None:
            continue
        event = events.get(scene.operation_id)
        if event is None:
            # A clean production context may already be at the canonical
            # opening URL (for example a caller seeded it after a validated
            # login).  The executor intentionally skips an equivalent
            # bootstrap Navigate to avoid a duplicate refresh.  Treat that
            # planned opening operation as satisfied only when the first
            # successful browser event proves the same page; all later
            # missing operations remain hard failures.
            operation = plan_operations.get(scene.operation_id)
            first_plan_operation = plan.workflow_steps[0].operation if plan.workflow_steps else None
            if (
                operation is not None
                and operation.kind is OperationKind.NAVIGATE
                and first_plan_operation is operation
                and any(
                    event_item.success
                    and event_item.page_url
                    and _canonical_navigation_url(context.url, event_item.page_url)
                    == _canonical_navigation_url(context.url, str(operation.value or ""))
                    for event_item in trace.events
                )
            ):
                scenes.append(
                    {
                        "id": scene.id,
                        "title": scene.title,
                        "duration_ms": 0,
                        "evidence": scene.evidence,
                        "interaction": scene.interaction,
                        "narrated": False,
                    }
                )
                continue
            failures.append("EDITORIAL_SCENE_NOT_EXECUTED")
            continue
        # Browser navigation/video instrumentation introduces a small clock
        # boundary around an otherwise completed scene hold. Keep the reading
        # dwell strict while avoiding a false repair for sub-quarter-second
        # scheduler jitter.
        required_dwell_ms = int(scene.required_dwell_seconds * 1000)
        # Authentication beats are synthetic presentation chapters. Their
        # trace events intentionally last five seconds while the generic
        # storyboard enrichment may apply the normal reading dwell (roughly
        # nine seconds) to product scenes. Keep the login contract focused on
        # visible typing/security rather than rejecting a valid run for a
        # product-page reading threshold.
        if scene.operation_id == "auth:username" or scene.operation_id == "auth:password":
            required_dwell_ms = min(required_dwell_ms, 2_000)
        elif scene.operation_id == "auth:submit":
            required_dwell_ms = min(required_dwell_ms, 2_500)
        if event.duration_ms + 250 < required_dwell_ms:
            failures.append("SCENE_ADVANCED_BEFORE_REQUIRED_DWELL")
        # The trace remains exhaustive, but narration intentionally selects
        # reader-sized editorial beats.  Requiring a caption for every low
        # level scroll caused 20-word captions to flash at crawler speed.
        if scene.operation_id not in narrated_operation_ids:
            scenes.append(
                {
                    "id": scene.id,
                    "title": scene.title,
                    "duration_ms": event.duration_ms,
                    "evidence": scene.evidence,
                    "interaction": scene.interaction,
                    "narrated": False,
                }
            )
            continue
        text = script_by_event.get(event.id) or moment_text_by_event.get(event.id, "")
        if not text:
            failures.append("EDITORIAL_SCENE_MISSING_NARRATION")
            scene_failures: list[str] = ["EDITORIAL_SCENE_MISSING_NARRATION"]
        else:
            scene_failures = []
            operation = next(
                (
                    step.operation
                    for step in plan.workflow_steps
                    if step.operation.id == scene.operation_id
                ),
                None,
            )
            narrated_texts.append(text)
            narrated_operations.append(operation)
            target_words = set(re.findall(r"[a-z0-9]{4,}", scene.title.lower()))
            caption_words = set(re.findall(r"[a-z0-9]{4,}", text.lower()))
            generic_words = {
                "explored",
                "context",
                "gives",
                "viewer",
                "concrete",
                "evidence",
                "next",
                "part",
                "walkthrough",
            }
            title_only = bool(target_words) and not (caption_words - target_words - generic_words)
            # The first proved page scene carries the approved presenter
            # introduction authored from opening evidence.  Its wording is
            # intentionally provider/style agnostic; requiring the old
            # literal "Welcome to" prefix made model-authored introductions
            # fail even when they were grounded and useful.
            presenter_opening = (
                scene.operation_id == first_executed_operation
                and len(text.split()) >= 12
                and event.id in opening_event_ids
                and len(opening_words & caption_words) >= 2
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
            route_mechanics = (
                r"\bthe\s+next\s+view\b",
                r"\bworkspace\s+establish(?:es|ing)\s+(?:the\s+)?current\s+working\s+view\b",
                r"\bvisible\s+workflow\s+is\s+demonstrated\b",
                r"\bprovides\s+the\s+setup\s+context\b.*\b(?:now\s+)?(?:we\s+)?can\s+see\b",
                r"\b(?:current|working)\s+view\s+before\s+we\s+demonstrate\b",
            )
            repeated_opening = (
                bool(opening_words)
                and ("\n" in scene.title or bool(re.search(r"\b\d+\b", scene.title)))
                and len(caption_words & opening_words) / max(len(caption_words), 1) >= 0.72
                and not presenter_opening
            )
            generic_reasons = []
            if len(text.split()) < 6:
                generic_reasons.append("too_short")
            if (
                not presenter_opening
                and not str(scene.operation_id or "").startswith("auth:")
                and not _viewer_ready(text, scene.title)
            ):
                generic_reasons.append("not_viewer_ready")
            if "distinct part of the product experience" in text:
                generic_reasons.append("boilerplate")
            if any(phrase in text.lower() for phrase in boilerplate):
                generic_reasons.append("boilerplate")
            if any(re.search(pattern, text.lower()) for pattern in route_mechanics):
                generic_reasons.append("route_mechanics")
            if repeated_opening:
                generic_reasons.append("repeated_opening")
            if title_only:
                generic_reasons.append("title_only")
            if _operation_value_leak(text, operation):
                generic_reasons.append("operation_value_leak")
            if generic_reasons:
                failures.append("GENERIC_ROUTE_LABEL_CAPTION")
                scene_failures.append("GENERIC_ROUTE_LABEL_CAPTION:" + ",".join(generic_reasons))
            evidence_text = _scene_source(context, scene)
            if _looks_like_screen_transcript(scene.narration, evidence_text):
                failures.append("SCREEN_TRANSCRIPT_CAPTION")
            evidence_words = set(re.findall(r"[a-z0-9]{3,}", evidence_text.lower()))
            target_words = (
                set(re.findall(r"[a-z0-9]{4,}", operation.target.name.lower()))
                if operation and operation.target
                else set()
            )
            # In visual editors the DOM target is commonly the canvas shell,
            # while the operation intent carries the semantic label being
            # placed or connected. Use that grounded intent for the target
            # evidence check instead of rejecting a correct caption because it
            # does not repeat the shell's product name.
            if operation is not None and operation.kind in {
                OperationKind.KEY_PRESS,
                OperationKind.POINTER_SEQUENCE,
            }:
                semantic_match = re.search(
                    r"(?:requested|observed)\s+(.+?)\s+label\b",
                    operation.intent,
                    flags=re.IGNORECASE,
                )
                if semantic_match:
                    target_words = set(
                        re.findall(r"[a-z0-9]{4,}", semantic_match.group(1).lower())
                    ) | {"label"}
                else:
                    # Canvas/editor traces often target the generic surface
                    # even though the operation intent names the semantic
                    # component being drawn.  Ground the caption against that
                    # immutable component intent, not the SVG/canvas shell.
                    component_match = re.search(
                        r"(?:requested|observed)\s+(.+?)\s+component\s+node\b",
                        operation.intent,
                        flags=re.IGNORECASE | re.DOTALL,
                    )
                    if component_match:
                        target_words = set(
                            re.findall(r"[a-z0-9]{4,}", component_match.group(1).lower())
                        ) | {"component"}
                    else:
                        connector_match = re.search(
                            r"connect\s+(?:the\s+)?(?:requested|observed)?\s*(.+?)\s+and\s+(.+?)\s+components",
                            operation.intent,
                            flags=re.IGNORECASE,
                        )
                        if connector_match:
                            target_words = set(
                                re.findall(
                                    r"[a-z0-9]{4,}",
                                    f"{connector_match.group(1)} {connector_match.group(2)}",
                                )
                            ) | {"connector"}
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
            # The target label is itself immutable DOM evidence for a control
            # scene.  Dense pages may have no target-headed prose fact (for
            # example a filter chip or search field), so excluding the element
            # label makes valid, concise captions fail despite being grounded.
            target_source_words.update(
                word
                for evidence in scene.evidence
                if evidence.startswith(("element:", "objective-label:"))
                for word in re.findall(r"[a-z0-9]{3,}", evidence.split(":", 1)[1].lower())
            )
            semantic_target_explicit = bool(
                operation is not None
                and operation.kind
                in {OperationKind.KEY_PRESS, OperationKind.POINTER_SEQUENCE}
                and len(target_words & caption_words) >= 2
            )
            if (
                target_words
                and operation is not None
                and operation.kind
                not in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}
                and not (target_words & target_source_words)
                and not semantic_target_explicit
            ):
                failures.append("SCENE_TARGET_EVIDENCE_MISMATCH")
            # A scene can use concise editorial connective language, but its
            # factual sentence must remain recognisably tied to that scene's
            # assigned page/card evidence—not another page in the run.
            # The first proved operation carries the opening presenter line,
            # but its evidence belongs to the operation-less opening scene.
            # Do not compare that welcome against the destination page's
            # local facts; the opening-specific checks above already require
            # it to be grounded in the established opening evidence.
            target_is_explicit = bool(
                operation is not None
                and operation.target is not None
                and any(
                    evidence.casefold() == f"element:{operation.target.name}".casefold()
                    for evidence in scene.evidence
                )
                and bool(target_words & caption_words)
            )
            if (
                not presenter_opening
                and operation is not None
                and operation.kind
                not in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}
                and len(target_source_words) >= 3
                and len(caption_words & target_source_words) < 2
                and not target_is_explicit
                and not semantic_target_explicit
            ):
                failures.append("UNSUPPORTED_OR_WRONG_SCENE_CAPTION")
        scenes.append(
            {
                "id": scene.id,
                "title": scene.title,
                "duration_ms": event.duration_ms,
                "evidence": scene.evidence,
                "interaction": scene.interaction,
                "narrated": True,
                "failures": scene_failures,
            }
        )
    if len(trace.events) < len([scene for scene in storyboard.scenes if scene.operation_id]):
        warnings.append("SOME_PLANNED_SCENES_DID_NOT_PRODUCE_EVIDENCE")
    for index, text in enumerate(narrated_texts):
        if index == 0 or text.casefold().startswith(("welcome to", "today i'll walk")):
            continue
        # Compare semantic payload, not the shared connective grammar used to
        # keep captions conversational.  ``The X view keeps Y close at hand``
        # is intentionally reusable; repetition is only a failure when the
        # meaningful nouns/verbs are also the same.
        stop = {
            "this",
            "the",
            "view",
            "workspace",
            "keeps",
            "close",
            "hand",
            "so",
            "visible",
            "records",
            "can",
            "be",
            "narrowed",
            "and",
            "reviewed",
            "brings",
            "into",
            "current",
            "workflow",
            "with",
            "surrounding",
            "controls",
            "for",
            "context",
            "adds",
            "distinct",
            "beat",
            "walkthrough",
            "section",
            "opens",
            "its",
            "ready",
            "focused",
            "review",
            "observed",
            "information",
            "together",
            "exposes",
            "state",
            "grounding",
            "next",
            "step",
            "held",
            "long",
            "enough",
            "establish",
            "established",
            "before",
            "continues",
            "product",
            "available",
            "checkpoint",
            "shown",
            "within",
            "keeping",
            "details",
            "move",
            # Visual-editor action scaffolding is intentionally shared across
            # adjacent scenes; the semantic label/endpoint is the meaningful
            # payload used to distinguish them.
            "select",
            "selected",
            "text",
            "tool",
            "prepare",
            "prepares",
            "component",
            "label",
            "placing",
            "place",
            "canvas",
            "draw",
            "draws",
            "arrow",
            "data",
            "flow",
            "explicit",
            "field",
            "fields",
            "form",
            "forms",
            "viewer",
            "choice",
            "record",
            "completed",
            "provides",
            "clear",
            "needed",
            "distinguish",
            "recognizable",
            "identity",
            "later",
            "follow",
            "carries",
            "forward",
            "isolated",
            "result",
            "screen",
            "originated",
            "creation",
            "create",
            "entered",
            "reveals",
            "opening",
            "captures",
            "selection",
        }
        words = {w for w in re.findall(r"[a-z0-9]{4,}", text.lower()) if w not in stop}
        for other_index, other in enumerate(narrated_texts[:index]):
            if other.casefold().startswith(("welcome to", "today i'll walk")):
                continue
            prior_operation = narrated_operations[other_index]
            # Low-level visual-editor beats share connective grammar by
            # design. When both operations carry different quoted semantic
            # labels/endpoints, that difference is the evidence-backed
            # payload and the captions are not repetitive merely because they
            # both describe drawing or selecting a tool. Identical labels are
            # still compared and can fail this gate.
            current_operation = narrated_operations[index] if index < len(narrated_operations) else None
            current_quotes = tuple(
                re.findall(r"['\"]([^'\"]+)['\"]", str(getattr(current_operation, "intent", "")))
            )
            prior_quotes = tuple(
                re.findall(r"['\"]([^'\"]+)['\"]", str(getattr(prior_operation, "intent", "")))
            )
            if current_quotes and prior_quotes and current_quotes != prior_quotes:
                continue
            current_intent = str(getattr(current_operation, "intent", "") or "").casefold()
            prior_intent = str(getattr(prior_operation, "intent", "") or "").casefold()
            visual_intent_terms = (
                "canvas",
                "draw",
                "rectangle",
                "component",
                "text tool",
                "text cursor",
                "label",
                "arrow",
                "connector",
            )
            if (
                current_intent != prior_intent
                and any(term in current_intent for term in visual_intent_terms)
                and any(term in prior_intent for term in visual_intent_terms)
            ):
                continue
            other_words = {w for w in re.findall(r"[a-z0-9]{4,}", other.lower()) if w not in stop}
            if not words or not other_words:
                continue
            overlap = len(words & other_words) / max(1, min(len(words), len(other_words)))
            # A shared feature noun (for example, ``bookings``) is expected
            # across adjacent chapters and is not repetition by itself. A
            # repeated editorial beat requires at least two meaningful words
            # in common in addition to the ratio threshold.
            if overlap >= 0.82 and len(words & other_words) >= 2:
                failures.append("REPETITIVE_EDITORIAL_NARRATION")
                break
        if "REPETITIVE_EDITORIAL_NARRATION" in failures:
            break
    return {
        "editorial_score": 1.0 if not failures else 0.0,
        "hard_failures": list(dict.fromkeys(failures)),
        "warnings": warnings,
        "scenes": scenes,
        "minimum_duration_seconds": storyboard.minimum_duration_seconds,
    }
