"""Editorial storyboard construction and LLM enrichment."""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlsplit

from app.contracts.models import (
    DemoPlan,
    EditorialBrief,
    EditorialNarrationDraft,
    EditorialScene,
    EditorialStoryboard,
    OperationKind,
    ProductContext,
)
from app.observability.logging import redact_prompt_text
from app.presentation.editorial.narrative import *
from app.providers.errors import ProviderError


def build_editorial_storyboard(context: ProductContext, plan: DemoPlan) -> EditorialStoryboard:
    """Build a deterministic story from observed text and the validated plan.

    Model-created prose may improve this later, but this artifact is intentionally
    useful on its own: every statement is traceable to current discovery evidence.
    """
    opening_page = context.page_knowledge[0] if context.page_knowledge else None
    purpose = _clean(context.title or "Product walkthrough")
    objective_subject = _objective_subject(context)
    opening_fact = next(
        (
            prose
            for _, prose in _page_fact_records(opening_page)
            if not _looks_like_label_collection(prose)
            and _safe_editorial_text(prose)
            and not re.search(
                r"\b(?:chevron|notification|dashboard highlights)\b", prose, re.IGNORECASE
            )
        ),
        None,
    )
    # Draft only for the opening enrich pass — LLM owns the shipped welcome.
    opening_candidate = _safe_editorial_text(opening_fact or "") or ""
    opening = (
        opening_candidate
        if opening_candidate and _viewer_ready(opening_candidate, purpose)
        else (
            _interaction_skeleton(
                objective_subject or purpose.split("|", 1)[0].strip() or "product"
            )
        )
    )
    nav = [item.name for item in context.navigation if item.name][:8]
    brief = EditorialBrief(
        title=f"{_concise_title(purpose)} walkthrough",
        product_purpose=purpose,
        opening_message=opening,
        navigation_order=nav,
        facts=_facts(context),
        excluded_areas=["external links", "destructive actions", "unverified claims"],
    )
    scenes: list[EditorialScene] = [
        EditorialScene(
            id="opening",
            title="Opening view",
            purpose="Establish the product before any interaction.",
            # Give every walkthrough a presenter-led opening.  The product
            # name comes from the observed browser title and the following
            # sentence is the evidence-grounded opening message; this avoids
            # dropping viewers into a page with a bare DOM description.
            narration=_fallback_presenter_intro(purpose, opening, objective_subject),
            evidence=[f"page:{context.url}"],
            interaction="opening",
            required_dwell_seconds=5.0,
            completion_criteria=["initial product view is stable", "opening message is readable"],
        )
    ]
    opening_operation = plan.workflow_steps[0].operation if plan.workflow_steps else None
    opening_destination = (
        _page_for_operation(context, opening_operation)
        if opening_operation is not None
        else opening_page
    )
    # The discovery URL is often the authenticated shell, while a focused
    # workflow plan intentionally starts on the requested destination.  The
    # opening chapter must describe the state the viewer actually sees after
    # authentication/navigation, never the stale post-login shell.  Rebind
    # its fact and evidence to the validated plan boundary before enrichment.
    if opening_destination is not None and (
        opening_page is None
        or _canonical_page_url(opening_destination.url) != _canonical_page_url(opening_page.url)
    ):
        destination_fact = next(
            (
                prose
                for _, prose in _page_fact_records(opening_destination)
                if not _looks_like_label_collection(prose)
                and _safe_editorial_text(prose)
                and not re.search(
                    r"\b(?:chevron|notification|dashboard highlights)\b", prose, re.IGNORECASE
                )
            ),
            None,
        )
        destination_summary = _summary_from_intro(
            _safe_editorial_text(destination_fact or " ".join(opening_destination.visible_facts)),
            objective_subject or purpose.split("|", 1)[0].strip() or "product experience",
        )
        destination_opening = (
            destination_summary
            or destination_fact
            or (
                f"The {objective_subject} workspace is ready for a focused walkthrough."
                if objective_subject
                else ""
            )
            or f"The opening view establishes {_clean(opening_destination.purpose or opening_destination.title or purpose)}"
        )
        scenes[0] = scenes[0].model_copy(
            update={
                "narration": _fallback_presenter_intro(
                    purpose, destination_opening, objective_subject
                ),
                "evidence": [f"page:{opening_destination.url}"],
                "page_url": opening_destination.url,
            }
        )
    opening_bridge = _exploration_context_bridge(context, opening_destination)
    if opening_bridge is not None:
        bridge_copy, bridge_evidence = opening_bridge
        scenes[0] = scenes[0].model_copy(
            update={
                # Both fragments are already validated complete sentences. Do not
                # re-run their combined copy through the short fact normalizer: it
                # can clip the final relationship sentence at its length boundary.
                "narration": f"{scenes[0].narration} {bridge_copy}",
                "evidence": [*scenes[0].evidence, bridge_evidence],
            }
        )
    # Authentication is an optional, objective-driven chapter.  The execution
    # layer emits these stable operation IDs only when a login form is actually
    # encountered, so an already-authenticated run cleanly omits the scenes.
    # No credential value is ever included in the storyboard or narration.
    objective_text = str(getattr(context.objective, "raw", "") or plan.objective).casefold()
    if "login" in objective_text or "sign in" in objective_text or "sign-in" in objective_text:
        scenes.extend(
            [
                EditorialScene(
                    id="authentication-username",
                    operation_id="auth:username",
                    title="Email address",
                    purpose="Begin the authenticated workspace session.",
                    narration="We start by signing in so the walkthrough reflects the workspace a real team member would use.",
                    evidence=[f"page:{context.url}", "auth:credential-entry"],
                    interaction="observe",
                    required_dwell_seconds=2.0,
                    completion_criteria=["email field is visibly completed"],
                    story_phase="context",
                    page_url=context.url,
                ),
                EditorialScene(
                    id="authentication-password",
                    operation_id="auth:password",
                    title="Password",
                    purpose="Complete authentication without exposing the secret.",
                    narration="The sign-in form is completed securely before the product workspace opens.",
                    evidence=[f"page:{context.url}", "auth:credential-entry"],
                    interaction="observe",
                    required_dwell_seconds=2.0,
                    completion_criteria=["password field remains redacted"],
                    story_phase="context",
                    page_url=context.url,
                ),
                EditorialScene(
                    id="authentication-submit",
                    operation_id="auth:submit",
                    title="Sign in",
                    purpose="Enter the authenticated product experience.",
                    narration="The workspace is now authenticated, and we can move into the requested product flow.",
                    evidence=[f"page:{context.url}", "auth:submit"],
                    interaction="click",
                    required_dwell_seconds=2.5,
                    completion_criteria=["authenticated workspace is visible"],
                    story_phase="enter",
                    page_url=context.url,
                ),
            ]
        )
    total_steps = len(plan.workflow_steps)
    for index, step in enumerate(plan.workflow_steps, start=1):
        operation = step.operation
        if (
            index == 1
            and operation.kind is OperationKind.NAVIGATE
            and (str(operation.value or "").rstrip("/") == context.url.rstrip("/"))
        ):
            # The opening scene already establishes the initial route; do not
            # create a duplicate generic "next view" chapter for its load.
            continue
        page = _page_for_operation(context, operation)
        if (
            operation.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}
            and operation.target is not None
        ):
            # The execution planner stores the source page on a visible link
            # target.  For narration, however, this scene must describe the
            # destination that the viewer is about to see. Recover the
            # same-origin href from the observed semantic selector and bind to
            # that PageKnowledge record, avoiding source-shell captions such
            # as ``Dashboard brings Lead Management into view``.
            selector = str(operation.target.selector or "")
            href_match = re.search(r"href=['\"]([^'\"]+)['\"]", selector)
            if href_match:
                destination = _canonical_page_url(urljoin(context.url, href_match.group(1)))
                page = next(
                    (
                        candidate
                        for candidate in context.page_knowledge
                        if _canonical_page_url(candidate.url) == destination
                        or urlsplit(_canonical_page_url(candidate.url)).path.rstrip("/")
                        == urlsplit(destination).path.rstrip("/")
                    ),
                    page,
                )
            if page is None or _canonical_page_url(page.url) == _canonical_page_url(context.url):
                expected_destination = next(
                    (
                        str(condition.expected)
                        for condition in operation.postconditions
                        if getattr(condition, "kind", "") == "url" and condition.expected
                    ),
                    "",
                )
                if expected_destination:
                    destination = _canonical_page_url(urljoin(context.url, expected_destination))
                    page = next(
                        (
                            candidate
                            for candidate in context.page_knowledge
                            if _canonical_page_url(candidate.url) == destination
                            or urlsplit(_canonical_page_url(candidate.url)).path.rstrip("/")
                            == urlsplit(destination).path.rstrip("/")
                        ),
                        page,
                    )
        all_page_facts = " ".join(str(item) for item in getattr(page, "visible_facts", []) or [])
        # A route navigation without a DOM target still has a destination
        # page.  Use that observed page identity as the scene subject instead
        # of inventing ``the next view``; the latter is tour mechanics, cannot
        # be grounded, and produces a misleading caption for every product.
        target = (
            operation.target.name
            if operation.target
            else _clean(
                str(getattr(page, "purpose", "") or getattr(page, "title", "") or "this workspace"),
                96,
            )
        )
        # Canvas/editor actions often expose the application shell as their
        # DOM target while the operation intent names the semantic object being
        # created. Follow that observed object in the scene contract so
        # captions and QA describe what the viewer sees, not an implementation
        # surface. The rule is vocabulary-free and applies to any visual
        # editor.
        if operation.kind in {OperationKind.KEY_PRESS, OperationKind.POINTER_SEQUENCE}:
            semantic_match = re.search(
                r"(?:requested|observed)\s+(.+?)\s+label\b",
                operation.intent,
                flags=re.IGNORECASE,
            )
            if semantic_match:
                target = _clean(f"{semantic_match.group(1).strip()} label", 96)
        # Placeholder copy is an implementation hint, not a viewer-facing
        # chapter title. Normalize it to the semantic field being shown so
        # captions never read out example values or ellipses verbatim.
        if re.search(r"\([^)]{2,}\)|\.\.\.$", target):
            placeholder_target = re.sub(r"\s*\([^)]*\)", "", target).strip(" .:-")
            placeholder_target = re.sub(
                r"^(?:type|enter|fill|search|choose)\s+",
                "",
                placeholder_target,
                flags=re.IGNORECASE,
            )
            placeholder_target = re.sub(
                r"^(?:a|an|the)\s+", "", placeholder_target, flags=re.IGNORECASE
            )
            if placeholder_target:
                target = _clean(f"{placeholder_target} input", 96)
        target = target.rstrip(" :.-") or target
        target_words = re.findall(r"[a-z0-9]{4,}", target.lower())
        normalized_target = " ".join(target.split()).lower()
        observed = _observed_narration(context, operation, target)
        # Draft only: never invent domain/tour prose here. LLM enrich owns
        # shipped sentences. Prefer viewer-ready observed fact, else skeleton.
        if re.search(
            r"\b(?:click|press|tap|select)\b.*\b(?:to|then|and)\b", observed, re.IGNORECASE
        ) or not _viewer_ready(observed, target):
            candidate = _target_fact_narration(page, target) if page is not None else ""
            observed = (
                candidate
                if candidate and _viewer_ready(candidate, target)
                else _interaction_skeleton(target)
            )
        target_fact = _target_fact_narration(page, target)
        if target_fact and not _viewer_ready(target_fact, target):
            target_fact = ""
        if (
            target_fact
            and operation.kind is OperationKind.SCROLL_TO
            and target_words
            and not any(word in observed.lower() for word in target_words)
            and _viewer_ready(target_fact, target)
        ):
            observed = target_fact
        if operation.kind is OperationKind.SCROLL_TO:
            narration = observed
            interaction, dwell = "scroll", 5.0
        elif operation.kind in {OperationKind.OPEN_NAVIGATION_ITEM, OperationKind.NAVIGATE}:
            narration = observed
            interaction, dwell = "navigate", 4.5
        elif operation.kind in {
            OperationKind.CLICK,
            OperationKind.OPEN_MODAL,
            OperationKind.CLOSE_MODAL,
        }:
            narration = observed
            interaction, dwell = "click", 4.0
        elif operation.kind in {
            OperationKind.FILL_TEXT,
            OperationKind.FILL_EMAIL,
            OperationKind.FILL_PHONE,
            OperationKind.SELECT_OPTION,
            OperationKind.SELECT_DATE,
            OperationKind.SELECT_DATE_RANGE,
        }:
            narration = observed
            interaction, dwell = "type", 4.25
        elif operation.kind is OperationKind.SUBMIT:
            narration = observed
            interaction, dwell = "submit", 4.75
        else:
            narration = observed
            interaction, dwell = "observe", 3.5
        if not _viewer_ready(narration, target):
            narration = _interaction_skeleton(target)
        fact_id, cited_prose = _scene_fact(context, operation, target)
        if (
            cited_prose
            and operation.kind is OperationKind.SCROLL_TO
            and _viewer_ready(_subject_fact(target, cited_prose), target)
            and len(cited_prose.split()) >= 6
            and not (not re.search(r"[.!?]", cited_prose) and bool(re.search(r"\d", cited_prose)))
        ):
            observed = _target_fact_narration(page, target) or _subject_fact(target, cited_prose)
            if _viewer_ready(observed, target):
                narration = observed
        scene_evidence = [
            f"operation:{operation.id}",
            *operation.evidence_refs,
            f"element:{target}",
        ]
        if page is not None:
            scene_evidence.append(f"page:{page.url}")
        if fact_id:
            scene_evidence.append(fact_id)
        if operation.kind is OperationKind.SCROLL_TO and page is not None:
            grouped_subjects = [
                " ".join(str(item).split())
                for item in (getattr(operation, "covered_content_groups", None) or [])
                if str(item).strip()
            ]
            for subject in [target, *grouped_subjects]:
                local_fact_id, _local_fact = _matching_page_fact(page, subject)
                if local_fact_id:
                    scene_evidence.append(local_fact_id)
        provisional = EditorialScene(
            id=f"scene-{index}",
            operation_id=operation.id,
            title=_clean(target, 120),
            purpose=_clean(operation.intent),
            narration=narration,
            evidence=list(dict.fromkeys(scene_evidence)),
            interaction=interaction,
            required_dwell_seconds=dwell,
            completion_criteria=["target state is visible", "caption evidence is readable"],
            page_url=page.url if page is not None else operation.page_url,
            story_phase=(
                "context"
                if index == 1
                else "close"
                if index == total_steps
                else "enter"
                if interaction == "navigate"
                else "demonstrate"
                if interaction == "click"
                else "explain"
            ),
            action_classification="transitional" if interaction == "navigate" else "essential",
        )
        evidence_words = set(
            re.findall(r"[a-z0-9]{4,}", _scene_source(context, provisional).lower())
        )
        narration_words = set(re.findall(r"[a-z0-9]{4,}", narration.lower()))
        if (
            len(evidence_words) >= 3
            and len(narration_words & evidence_words) < 2
            and not (
                operation.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}
                and _viewer_ready(narration, target)
            )
            and not (
                operation.kind
                in {OperationKind.CLICK, OperationKind.POINTER_SEQUENCE, OperationKind.DRAG}
                and target_words
                and bool(set(target_words) & evidence_words)
                and _viewer_ready(narration, target)
            )
        ):
            if cited_prose and not _looks_like_screen_transcript(cited_prose, cited_prose):
                recovered = _viewer_fact(cited_prose)
            else:
                recovered = _observed_narration(context, operation, target)
            if not _viewer_ready(recovered, target):
                recovered = _interaction_skeleton(target)
            provisional = provisional.model_copy(update={"narration": recovered})
        if (
            target_fact
            and operation.kind is OperationKind.SCROLL_TO
            and target_words
            and not any(word in provisional.narration.lower() for word in target_words)
            and _viewer_ready(target_fact, target)
        ):
            provisional = provisional.model_copy(update={"narration": target_fact})
        scenes.append(provisional)
    # A page may be re-entered after an earlier chapter when the validated
    # workflow proves a meaningful follow-up interaction (for example opening
    # a create form from a list). Reusing the original page summary makes the
    # story sound duplicated and trips the repetition gate. Rephrase the
    # re-entry from its own page evidence while retaining the same scene ID,
    # timing, and provenance.
    seen_narration: list[str] = []
    operation_by_id = {step.operation.id: step.operation for step in plan.workflow_steps}
    diversified: list[EditorialScene] = []
    for scene in scenes:
        normalized = " ".join(scene.narration.split()).casefold()
        same_page_title = [
            prior_scene
            for prior_scene in diversified
            if scene.operation_id is not None
            and prior_scene.operation_id is not None
            and (scene.page_url or "").rstrip("/").casefold()
            == (prior_scene.page_url or "").rstrip("/").casefold()
            and " ".join(scene.title.split()).casefold()
            == " ".join(prior_scene.title.split()).casefold()
        ]
        # The hard editorial gate is intentionally story-wide, not limited to
        # identical titles. A model can repeat the same card copy under two
        # different headings (common on documentation/testimonial pages), so
        # repair any earlier semantic beat on the same product page before QA.
        prior_repeated = next(
            (
                prior_scene
                for prior_scene in diversified
                if prior_scene.interaction != "opening"
                and _narration_repeats(scene.narration, prior_scene.narration)
            ),
            None,
        )
        duplicate = any(
            normalized == " ".join(prior_scene.narration.split()).casefold()
            or (
                len(set(re.findall(r"[a-z0-9]{4,}", normalized))) >= 4
                and len(
                    set(re.findall(r"[a-z0-9]{4,}", normalized))
                    & set(re.findall(r"[a-z0-9]{4,}", prior_scene.narration.casefold()))
                )
                >= 4
            )
            for prior_scene in same_page_title
        )
        if (duplicate or prior_repeated is not None) and scene.operation_id:
            operation = operation_by_id.get(scene.operation_id)
            title = _clean(scene.title, 72) or "this workspace"
            if operation is not None:
                # Re-entry/duplicate-title scenes still need a page-local
                # explanation.  Prefer the deterministic evidence summary for
                # this operation; the old connective sentence described the
                # tour mechanics and was rejected as generic narration.
                previous = (
                    prior_repeated.narration
                    if prior_repeated is not None
                    else (diversified[-1].narration if diversified else "")
                )
                local, fact_id = _distinct_evidence_narration(
                    context,
                    operation,
                    title,
                    previous,
                    interaction=scene.interaction,
                    story_phase=scene.story_phase,
                )
                if local and _viewer_ready(local, title):
                    evidence = list(scene.evidence)
                    if fact_id and fact_id not in evidence:
                        evidence.append(fact_id)
                    scene = scene.model_copy(update={"narration": local, "evidence": evidence})
                else:
                    local = _observed_narration(context, operation, title)
                    if (
                        local
                        and not _narration_repeats(local, previous)
                        and _viewer_ready(local, title)
                    ):
                        scene = scene.model_copy(update={"narration": local})
            normalized = " ".join(scene.narration.split()).casefold()
        seen_narration.append(normalized)
        diversified.append(scene)
    scenes = diversified
    # Run a final story-wide pass after heading/evidence guards. Those guards
    # can legitimately restore a target-specific fact, but that restored line
    # may duplicate a prior scene's editorial beat; repair it before duration
    # allocation and before any provider enrichment.
    scenes = _repair_fragmented_editorial_copy(context, scenes)
    scenes = _repair_repeated_narration(context, scenes)
    requested = min(max(plan.target_duration_seconds, 60), 180)
    # Capture time, rather than render-time slowdown, supplies the editorial
    # duration. Each chapter therefore gets enough reading time to be useful.
    # Allocate enough real reading time to every scene while keeping a typical
    # thorough walkthrough within its requested 2–3 minute editorial window.
    # Capture stays at native speed; this only governs how long the browser
    # holds after each meaningful reveal.
    # Reserve native-speed scroll and navigation time before allocating reading
    # holds. Otherwise a nominal three-minute plan can grow into a five-minute
    # capture simply because every scene also has physical motion.
    scroll_count = sum(scene.interaction == "scroll" for scene in scenes)
    navigation_count = sum(scene.interaction == "navigate" for scene in scenes)
    motion_budget = scroll_count * 2.5 + navigation_count * 2.5 + 5.0
    reading_budget = max(len(scenes) * 2.25, requested - motion_budget)
    # Small stories should not balloon solely because their requested target
    # is large; their minimum is still governed by the actual script.  Once a
    # story has enough scenes to carry a real walkthrough, however, capping
    # every chapter at 4.75 seconds makes the approved captions outrun the
    # browser footage. Allocate the remaining budget as native page reading
    # time, bounded to a deliberate per-scene maximum rather than a renderer
    # stretch or freeze.
    # Full walkthroughs commonly have many short, page-local beats; their
    # requested envelope already accounts for the breadth of coverage and a
    # modest chapter hold keeps the journey from ballooning. Focused stories
    # with several evidence beats need the larger allocation so narration can
    # actually be read over native footage.
    full_walkthrough = bool(
        re.search(r"\b(?:full|complete|entire|every|each)\b", plan.objective or "", re.IGNORECASE)
    )
    if len(scenes) <= 4 or full_walkthrough:
        chapter_dwell = min(4.75, max(2.25, reading_budget / len(scenes)))
    else:
        chapter_dwell = min(12.0, max(2.25, reading_budget / len(scenes)))
    # Keep total native holds inside the objective envelope with headroom for
    # auth, navigation, and form actions. Otherwise cloud captures idle for
    # every chapter dwell and the sync EDL cannot cut enough dead time.
    envelope = float(plan.maximum_duration_seconds or requested)
    action_headroom = 55.0
    max_total_reading = max(
        len(scenes) * 2.25,
        envelope - motion_budget - action_headroom - 2.5,
    )
    chapter_dwell = min(chapter_dwell, max_total_reading / max(len(scenes), 1))
    scenes = [
        scene.model_copy(
            update={
                "required_dwell_seconds": chapter_dwell
                if scene.operation_id
                else max(scene.required_dwell_seconds, chapter_dwell)
            }
        )
        for scene in scenes
    ]
    # The planner's objective minimum is the delivery contract.  Do not
    # replace it with the arithmetic sum of scene holds: render transitions
    # legitimately share a frame boundary, so that sum would reject a valid
    # native-speed edit merely because each scene's dwell overlaps a cut by a
    # few frames.  Per-scene dwell remains enforced by editorial QA.
    return EditorialStoryboard(
        brief=brief,
        scenes=scenes,
        minimum_duration_seconds=max(45, plan.minimum_duration_seconds),
    )


_GENERIC_EDITORIAL_PATTERNS = (
    "walkthrough pauses",
    "before moving on",
    "comes into focus",
    "visible detail is clear",
    "gives the viewer concrete evidence",
    "sets up the details",
    "bespoke command interface",
    "is visibly established in",
    "section is now visible",
    "section visibly presents",
    "section brings the visible details together",
    "quick overview of key metrics",
    "giving you immediate insight",
    "various filters and details",
    "ready for management and action",
    "surrounding controls visible for context",
    "observed details are read before the walkthrough continues",
    "clear next step for continuing the conversation",
)


def _supported(text: str, source: str) -> bool:
    """Require a meaningful phrase overlap before accepting model prose."""
    return _supported_with_overlap(text, source, minimum_overlap=3)


def _supported_with_overlap(text: str, source: str, *, minimum_overlap: int) -> bool:
    words = set(re.findall(r"[a-z0-9]{4,}", text.lower()))
    evidence = set(re.findall(r"[a-z0-9]{4,}", source.lower()))
    lowered = text.lower()
    if any(pattern in lowered for pattern in _GENERIC_EDITORIAL_PATTERNS) or bool(
        re.search(r"\b(?:keeps|brings|puts)\b[^.]{0,80}\bin focus\b", lowered)
    ):
        return False
    if len(re.findall(r"\b\d+\b", text)) >= 3:
        return False
    if len(re.findall(r"\b[A-Z]{2,}\b", text)) >= 5:
        return False
    if not re.search(
        r"\b(?:is|are|was|were|has|have|lets|helps|shows|show|keeps|brings|groups|gathers|contains|connects|supports|organizes|tracks|lists|offers|provides|explains|uses|creates|draws|draw|moves|opens|captures|gives|makes|enables|demonstrates|appears|remains|becomes|causes|caused|prevents|reduces|handles|processes|integrates|improves|requires|can|will|manage|manages|explore|explores|review|reviews|highlights|highlight|enter|enters|type|types|fill|fills|select|selects|submit|submits)\b",
        text,
        flags=re.IGNORECASE,
    ):
        return False
    # Thin scenes (one label) cannot satisfy a high overlap bar — require all
    # available evidence tokens instead of inventing a false failure.
    needed = min(minimum_overlap, max(1, len(evidence)))
    return len(words) >= 6 and len(words & evidence) >= needed


async def enrich_editorial_brief(
    context: ProductContext, storyboard: EditorialStoryboard, provider: object
) -> EditorialStoryboard:
    """Required first OpenRouter pass: product brief + opening welcome from evidence."""
    structured = getattr(provider, "structured", None)
    if structured is None:
        raise ProviderError(
            "openrouter",
            None,
            "ENRICH_PROVIDER_ERROR: structured provider required for editorial brief",
        )
    source = _editorial_evidence(context)
    # Keep the brief payload compact so a healthy model finishes under timeout.
    compact_source = source[:6_000]
    draft = storyboard.brief.model_dump(mode="json")
    prompt = (
        "Build a product-demo editorial brief from observed website evidence only. "
        "Return ONE JSON object with EXACTLY these keys:\n"
        "title (string, <=7 words, <=64 chars),\n"
        "product_purpose (string),\n"
        "opening_message (string: MUST begin with 'Welcome to <product from evidence>.', then "
        "preview walkthrough value in one more sentence, max 28 words total),\n"
        "navigation_order (array of short visible nav labels),\n"
        "facts (array of {text, evidence[]} where evidence items are page:<url>, element:<name>, "
        "or source:<url> from the evidence list),\n"
        "excluded_areas (array of short strings).\n"
        "Do not copy headings or screen transcripts verbatim. Do not invent unsupported facts. "
        "Ban tour chrome (next view, walkthrough continues, is now visible).\n"
        f"Draft to improve (keep the same keys):\n{json.dumps(draft, ensure_ascii=False)[:2_500]}\n"
        f"Evidence:\n{compact_source}"
    )
    last_error: Exception | None = None
    candidate = None
    for attempt in range(3):
        try:
            candidate = await structured(prompt, EditorialBrief)
            break
        except ProviderError as error:
            last_error = error
            if not getattr(error, "retryable", False) and error.status_code not in {None, 429}:
                raise
        except (ValueError, TypeError, AssertionError, IndexError, KeyError) as error:
            last_error = error
            detail = str(error)[:400]
            prompt = (
                prompt
                + f"\nPrevious attempt failed validation ({type(error).__name__}): {detail}. "
                "Return only valid JSON with the exact keys listed above."
            )
    if candidate is None:
        if isinstance(last_error, ProviderError):
            raise last_error
        raise ProviderError(
            "openrouter",
            None,
            f"ENRICH_PROVIDER_ERROR: brief enrich failed ({type(last_error).__name__}: {str(last_error)[:200]})",
        ) from last_error
    allowed_evidence = {
        f"page:{context.url}",
        *[f"page:{page.url}" for page in context.page_knowledge],
        *[
            _fact_id(page.url, fact)
            for page in context.page_knowledge
            for fact in page.visible_facts
        ],
        *[f"element:{item.name}" for item in context.elements],
        *[f"source:{item.source_url or context.url}" for item in context.elements],
    }
    # Drop ungrounded facts rather than failing the whole brief when most are valid.
    grounded_facts = [
        fact
        for fact in candidate.facts
        if set(fact.evidence).issubset(allowed_evidence) and _supported(fact.text, source)
    ]
    candidate = candidate.model_copy(update={"facts": grounded_facts})

    def _normalize_brief(brief: EditorialBrief) -> EditorialBrief:
        title = " ".join(str(brief.title).split()[:7])[:64].strip() or "Product walkthrough"
        opening = " ".join(str(brief.opening_message).split())
        product = (
            _clean(str(getattr(context, "title", "") or "").split("|", 1)[0], 64)
            or title.replace(" walkthrough", "").strip()
            or "this product"
        )
        if not opening.casefold().startswith("welcome to"):
            remainder = re.sub(
                r"^(?:hello|hi)[,!]?\s*",
                "",
                opening,
                count=1,
                flags=re.IGNORECASE,
            ).strip()
            if _is_skeleton_narration(remainder) or "control in focus" in remainder.casefold():
                purpose_bit = " ".join(str(brief.product_purpose).split())[:120].rstrip(".")
                remainder = purpose_bit or "Today we walk through the observed workflow."
            opening = f"Welcome to {product}. {remainder}".strip()
        if "control in focus" in opening.casefold():
            purpose_bit = " ".join(str(brief.product_purpose).split())[:120].rstrip(".")
            opening = f"Welcome to {product}. {purpose_bit}."
        if len(opening.split()) > 28:
            opening = _sentence_complete(opening, 180)
            opening = " ".join(opening.split()[:28]).rstrip(",;:") + (
                "" if opening.rstrip().endswith((".", "!", "?")) else "."
            )
        purpose = " ".join(str(brief.product_purpose).split())[:420]
        return brief.model_copy(
            update={"title": title, "opening_message": opening, "product_purpose": purpose}
        )

    def _brief_ready(brief: EditorialBrief) -> bool:
        opening = brief.opening_message
        purpose = brief.product_purpose
        if not _readable_fact(opening) or not _readable_fact(purpose):
            return False
        # Openings are greetings; require evidence overlap, not the denser scene verb gate.
        opening_words = set(re.findall(r"[a-z0-9]{4,}", opening.casefold()))
        purpose_words = set(re.findall(r"[a-z0-9]{4,}", purpose.casefold()))
        evidence_words = set(re.findall(r"[a-z0-9]{4,}", source.casefold()))
        if len(opening_words & evidence_words) < 2:
            return False
        if len(purpose_words & evidence_words) < 2 and not _supported(purpose, source):
            return False
        return True

    candidate = _normalize_brief(candidate)
    if not _brief_ready(candidate):
        repair_prompt = (
            prompt
            + "\nTighten opening_message to <=28 words, title <=7 words, and ground "
            "opening_message and product_purpose in evidence words only.\n"
            f"Rejected brief:\n{candidate.model_dump_json()}"
        )
        try:
            repaired = await structured(repair_prompt, EditorialBrief)
            repaired = repaired.model_copy(
                update={
                    "facts": [
                        fact
                        for fact in repaired.facts
                        if set(fact.evidence).issubset(allowed_evidence)
                        and _supported(fact.text, source)
                    ]
                }
            )
            candidate = _normalize_brief(repaired)
        except (ProviderError, ValueError, TypeError, AssertionError, IndexError, KeyError) as error:
            raise ProviderError(
                "openrouter",
                None,
                "ENRICH_UNGROUNDED: brief opening_message failed readiness/grounding checks",
            ) from error
        if not _brief_ready(candidate):
            raise ProviderError(
                "openrouter",
                None,
                "ENRICH_UNGROUNDED: brief opening_message failed readiness/grounding checks",
            )
    scenes = list(storyboard.scenes)
    if scenes and scenes[0].operation_id is None:
        # LLM owns the shipped opening; draft is replaced by opening_message.
        scenes[0] = scenes[0].model_copy(
            update={
                "narration": candidate.opening_message.strip(),
                "evidence": list(
                    dict.fromkeys([*scenes[0].evidence, f"page:{context.url}"])
                ),
            }
        )
    return storyboard.model_copy(update={"brief": candidate, "scenes": scenes})


def _is_route_mechanics_copy(text: str) -> bool:
    lowered = text.casefold()
    return (
        "next view" in lowered
        or "next part of the walkthrough" in lowered
        or "is now visible" in lowered
        or "visible context needed" in lowered
        or "distinguish this record" in lowered
        or "traceable example" in lowered
        or "recognizable identity" in lowered
        or bool(
            re.search(
                r"\b(?:view|page)\s+brings\b.*\binto\s+view\b|\bshowing\s+how\s+this\s+part\s+of\s+the\s+product\s+is\s+organized\b",
                lowered,
            )
        )
        or "observed details are read" in lowered
        or "before the walkthrough continues" in lowered
        or bool(re.search(r"\b(?:current|working)\s+view\b.*\b(?:before|then)\b", lowered))
        or bool(re.search(r"\b(?:click|press|tap|select|open)\b.{0,80}\b(?:to|then|and)\b", lowered))
    )


def _is_skeleton_narration(text: str) -> bool:
    lowered = text.casefold()
    return (
        "control in focus for this step on screen" in lowered
        or "uses the observed" in lowered and "selection on screen" in lowered
    )


def _reject_reason(
    context: ProductContext, scene: EditorialScene, text: str, *, opening_words: set[str]
) -> str | None:
    source = _scene_source(context, scene)
    if _is_skeleton_narration(text):
        return "skeleton template is not final narration; write a grounded product sentence"
    if not _supported_with_overlap(text, source, minimum_overlap=2):
        evidence_tokens = sorted(set(re.findall(r"[a-z0-9]{4,}", source.casefold())))[:8]
        return (
            "need >=2 meaningful words overlapping scene evidence; "
            f"reuse labels like {evidence_tokens}"
        )
    if not _viewer_ready(text, scene.title):
        return "narration not viewer-ready"
    if not _mentions_scene_element(scene, text):
        return f"must mention scene element/label from evidence ({scene.title})"
    inventory_title = "\n" in scene.title or bool(re.search(r"\b\d+\b", scene.title))
    words = set(re.findall(r"[a-z0-9]{4,}", text.lower()))
    if (
        inventory_title
        and opening_words
        and words
        and len(words & opening_words) / max(len(words), 1) >= 0.72
    ):
        return "repeats opening too closely"
    if _is_route_mechanics_copy(text):
        return "tour chrome / click instructions banned"
    if _looks_like_screen_transcript(text, source):
        return "looks like screen transcript dump"
    return None


async def enrich_editorial_storyboard(
    context: ProductContext, storyboard: EditorialStoryboard, provider: object
) -> EditorialStoryboard:
    """Required second pass: scene narration from evidence. Retries soft rejects."""
    structured = getattr(provider, "structured", None)
    if structured is None:
        raise ProviderError(
            "openrouter",
            None,
            "ENRICH_PROVIDER_ERROR: structured provider required for editorial narration",
        )
    original_by_id = {
        scene.id: scene for scene in storyboard.scenes if scene.operation_id is not None
    }
    if not original_by_id:
        return storyboard
    objective = str(
        getattr(getattr(context, "objective", None), "raw", "") or storyboard.brief.product_purpose
    )
    objective_spec = getattr(context, "objective", None)
    audience = str(getattr(objective_spec, "audience", "product prospect"))
    audience_profile = getattr(objective_spec, "audience_profile", None)
    video_type = str(getattr(objective_spec, "video_type", "feature_walkthrough"))
    purpose = str(getattr(objective_spec, "purpose", "") or "")
    tone = str(getattr(objective_spec, "tone", "conversational"))
    opening_words = set(re.findall(r"[a-z0-9]{4,}", storyboard.scenes[0].narration.lower()))
    pending_ids = set(original_by_id)
    narration_by_id: dict[str, str] = {}
    reject_map: dict[str, str] = {}
    last_provider_error: ProviderError | None = None

    for attempt in range(3):
        if not pending_ids:
            break
        scene_payload = {
            scene_id: {
                "allowed_evidence": original_by_id[scene_id].evidence,
                "observed_text": _scene_source(context, original_by_id[scene_id])[:1_800],
                "must_include_words": sorted(
                    {
                        word
                        for word in re.findall(
                            r"[a-z0-9]{4,}",
                            " ".join(
                                [
                                    original_by_id[scene_id].title,
                                    *[
                                        ref.split(":", 1)[1]
                                        for ref in original_by_id[scene_id].evidence
                                        if ref.startswith(("element:", "section:"))
                                    ],
                                ]
                            ).casefold(),
                        )
                        if word
                        not in {
                            "this",
                            "that",
                            "with",
                            "from",
                            "page",
                            "view",
                            "section",
                            "button",
                            "input",
                        }
                    }
                )[:8],
                "scene_title": original_by_id[scene_id].title,
                "scene_purpose": original_by_id[scene_id].purpose,
                "story_phase": original_by_id[scene_id].story_phase,
                "approved_fallback": original_by_id[scene_id].narration,
            }
            for scene_id in sorted(pending_ids)
        }
        retry_note = ""
        if reject_map:
            retry_note = (
                "\nRejected lines from prior attempt — rewrite only these ids with the reason:\n"
                + json.dumps(reject_map, ensure_ascii=False)
            )
        prompt = (
            "You are the product-demo narrator. Write polished narration for each immutable scene "
            "from THAT scene's evidence only. Return only {lines:[{id,narration}]}. One line per "
            "supplied scene id. No brief, timing, titles, or extra fields. "
            "Each line: 1–2 conversational sentences, 12–28 words. Name the visible control or page "
            "subject from allowed_evidence / observed_text, say what changed, and why it matters — "
            "using only words grounded in that scene. "
            "approved_fallback is a last-resort skeleton; improve it when evidence supports richer "
            "copy, otherwise keep it grounded. "
            "Never invent fields, domains, or outcomes not present in the scene evidence. "
            "Never invent typed secret values. Vary wording across adjacent scenes. "
            "Ban tour chrome: next view, working view, is now visible, walkthrough continues, "
            "highlights, showcases, details are shown, control in focus for this step. "
            "Do not recite screen copy verbatim, start with 'The ... section', or give click "
            "instructions. "
            "Good: grounded in the supplied control/page words for that scene. "
            "Bad: generic filler that could describe any form on any product. "
            "Every narration must share at least two meaningful words with ITS OWN scene evidence "
            "and should reuse must_include_words when provided.\n"
            f"Objective: {redact_prompt_text(objective)}\n"
            f"Video mode: {video_type}\n"
            f"Purpose: {redact_prompt_text(purpose) or 'explain the observed product clearly'}\n"
            f"Tone: {tone}\nAudience: {audience}\n"
            f"Audience profile: {json.dumps(audience_profile.model_dump(mode='json') if audience_profile is not None else {}, ensure_ascii=False)}\n"
            f"Scenes: {redact_prompt_text(json.dumps(scene_payload, ensure_ascii=False))}"
            f"{retry_note}"
        )
        try:
            candidate = await structured(prompt, EditorialNarrationDraft)
        except ProviderError as error:
            last_provider_error = error
            if not getattr(error, "retryable", True) and error.status_code not in {None, 429}:
                raise
            continue
        except (ValueError, TypeError, AssertionError, IndexError, KeyError) as error:
            reject_map = {scene_id: f"schema/parse error: {type(error).__name__}" for scene_id in pending_ids}
            continue

        proposed_lines = (
            [(line.id, line.narration) for line in candidate.lines]
            if isinstance(candidate, EditorialNarrationDraft)
            else [
                (scene.id, scene.narration)
                for scene in getattr(candidate, "scenes", [])
                if scene.operation_id is not None
            ]
        )
        proposed_by_id = dict(proposed_lines)
        if not set(pending_ids).issubset(set(proposed_by_id)):
            missing = sorted(set(pending_ids) - set(proposed_by_id))
            reject_map = {scene_id: "missing line for scene id" for scene_id in missing}
            # Keep any valid extra? No — only evaluate pending.
            for scene_id in list(pending_ids):
                if scene_id not in proposed_by_id:
                    continue
                reason = _reject_reason(
                    context,
                    original_by_id[scene_id],
                    proposed_by_id[scene_id],
                    opening_words=opening_words,
                )
                if reason is None:
                    narration_by_id[scene_id] = proposed_by_id[scene_id]
                    pending_ids.discard(scene_id)
                    reject_map.pop(scene_id, None)
                else:
                    reject_map[scene_id] = reason
            continue

        reject_map = {}
        for scene_id in list(pending_ids):
            reason = _reject_reason(
                context,
                original_by_id[scene_id],
                proposed_by_id[scene_id],
                opening_words=opening_words,
            )
            if reason is None:
                narration_by_id[scene_id] = proposed_by_id[scene_id]
                pending_ids.discard(scene_id)
            else:
                reject_map[scene_id] = reason

    if pending_ids:
        # Keep accepted enrich lines. Allow at most one skeleton draft as a
        # last resort; a wall of "control in focus" lines fails editorial QA
        # after Browserbase execution and wastes provider credits.
        unresolved: dict[str, str] = {}
        skeleton_used = sum(
            1 for text in narration_by_id.values() if _is_skeleton_narration(text)
        )
        for scene_id in sorted(pending_ids):
            fallback = original_by_id[scene_id].narration
            title = original_by_id[scene_id].title
            if (
                _viewer_ready(fallback, title)
                and not _is_skeleton_narration(fallback)
                and not any(
                    _narration_repeats(fallback, prior) for prior in narration_by_id.values()
                )
            ):
                narration_by_id[scene_id] = fallback
                continue
            if skeleton_used == 0:
                skeleton = _interaction_skeleton(title)
                if _viewer_ready(skeleton, title):
                    narration_by_id[scene_id] = skeleton
                    skeleton_used = 1
                    continue
            unresolved[scene_id] = reject_map.get(
                scene_id, "enrich returned no unique grounded line"
            )
        if unresolved:
            if last_provider_error is not None and not narration_by_id:
                raise last_provider_error
            raise ProviderError(
                "openrouter",
                None,
                "ENRICH_UNGROUNDED: " + json.dumps(unresolved, ensure_ascii=False),
            )
        pending_ids.clear()

    # Fail closed before execution if enrich still left a repetitive skeleton wall.
    merged_preview = [
        narration_by_id.get(scene.id, scene.narration) for scene in storyboard.scenes
    ]
    skeleton_count = sum(1 for text in merged_preview if _is_skeleton_narration(text))
    if skeleton_count >= 2:
        raise ProviderError(
            "openrouter",
            None,
            "ENRICH_UNGROUNDED: too many skeleton fallback lines "
            f"({skeleton_count}); refuse to spend execution credits on repetitive narration",
        )

    scenes = [
        scene.model_copy(update={"narration": narration_by_id[scene.id]})
        if scene.id in narration_by_id
        else scene
        for scene in storyboard.scenes
    ]
    scenes = _repair_fragmented_editorial_copy(context, scenes)
    scenes = _repair_repeated_narration(context, scenes)
    scenes = [
        scene.model_copy(
            update={
                "narration": (
                    scene.narration[:1].upper() + scene.narration[1:]
                    if scene.narration and scene.narration[0].islower()
                    else scene.narration
                )
            }
        )
        for scene in scenes
    ]
    return storyboard.model_copy(update={"scenes": scenes})



def _mentions_scene_element(scene: EditorialScene, text: str) -> bool:
    """Keep model prose tied to the scene's named subject, not just its page.

    Page evidence often contains many facts. Requiring at least one observed
    element label in a content scene prevents a contribution sentence from
    silently losing the company/project/feature it is meant to explain.
    """
    labels = [
        value.split(":", 1)[1]
        for value in scene.evidence
        if value.startswith("element:") and value.split(":", 1)[1].strip()
    ]
    if not labels:
        return True
    text_words = set(re.findall(r"[a-z0-9]{4,}", text.casefold()))
    ignored = {"toggle", "theme", "button", "control", "section", "page", "home"}
    for label in labels:
        label_words = {
            word for word in re.findall(r"[a-z0-9]{4,}", label.casefold()) if word not in ignored
        }
        if label_words and label_words & text_words:
            return True
    return False


def _mentions_scene_subject(scene: EditorialScene, text: str) -> bool:
    """Require editorial prose to retain the scene's own named subject."""
    title_words = {
        word
        for word in re.findall(r"[a-z0-9]{4,}", scene.title.casefold())
        if word not in {"this", "that", "view", "page", "section", "current", "visible"}
    }
    if not title_words:
        return True
    return bool(title_words & set(re.findall(r"[a-z0-9]{4,}", text.casefold())))


def _scene_source(context: ProductContext, scene: EditorialScene) -> str:
    """Return only the observed text explicitly assigned to a scene."""
    allowed = set(scene.evidence)
    chunks: list[str] = []
    # The planner may ground a semantic control from an accessibility or
    # Stagehand observation without adding it to the page's compact element
    # inventory.  Its explicit ``element:`` evidence is still authoritative;
    # retain the label so editorial validation can distinguish a target-bound
    # state change from a noisy page-wide inventory.
    chunks.extend(
        value.removeprefix("element:")
        for value in scene.evidence
        if value.startswith("element:") and value.removeprefix("element:").strip()
    )
    for page in context.page_knowledge:
        if f"page:{page.url}" not in allowed:
            continue
        chunks.extend(fact for fact in page.visible_facts if _fact_id(page.url, fact) in allowed)
        if not chunks:
            chunks.extend(page.visible_sections[:4])
    for element in context.elements:
        if f"element:{element.name}" in allowed:
            chunks.append(element.text or element.name)
    return " ".join(chunks)


def bind_storyboard_events(
    storyboard: EditorialStoryboard, trace_event_ids: set[str]
) -> EditorialStoryboard:
    """Discard only planned scenes that never produced visible execution evidence."""
    scenes = [
        scene
        for scene in storyboard.scenes
        if scene.operation_id is None or scene.operation_id in trace_event_ids
    ]
    return storyboard.model_copy(update={"scenes": scenes})

__all__ = [
    "_GENERIC_EDITORIAL_PATTERNS",
    "_mentions_scene_element",
    "_mentions_scene_subject",
    "_scene_source",
    "_supported",
    "bind_storyboard_events",
    "build_editorial_storyboard",
    "enrich_editorial_brief",
    "enrich_editorial_storyboard",
]
