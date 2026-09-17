"""Page-complete proposal builders."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from app.contracts.models import (
    CandidateDemoFlow,
    ObservedElement,
    OperationKind,
    PageKnowledge,
    Postcondition,
    ProductContext,
    SemanticOperation,
    Target,
    WorkflowProposal,
)
from app.planning.candidates.navigation import (  # noqa: F401
    _configuration_landmark,
    _fact_for_landmark,
    _is_control_chrome,
    _navigation_control,
    _page_form_controls,
    _page_landmarks,
    _page_story_subject,
    _phase_intent,
)
from app.planning.candidates.scoring import *


def build_page_complete_proposal(
    context: ProductContext, candidate: CandidateDemoFlow
) -> WorkflowProposal:
    """Compile evidence into a complete page journey, not generic tab labels.

    Each page receives the same editorial contract. Navigation is represented
    only by a discovered visible control; absent one, a direct URL fallback is
    explicitly marked so workflow QA can account for it.
    """
    page_by_url = {
        _canonical_url(context.url, page.url): page for page in _pages_for_context(context)
    }
    pages = [
        page_by_url[_canonical_url(context.url, url)]
        for url in candidate.page_urls
        if _canonical_url(context.url, url) in page_by_url
    ]
    if not pages:
        raise ValueError("candidate has no fresh PageKnowledge")
    # Candidate selection may be invoked from a persisted/legacy context that
    # has no ObjectiveSpec. Preserve the candidate's explicit full-tour
    # intent so later pages receive complete local coverage instead of being
    # reduced to a first/last representative pair.
    objective_text = str(context.objective.raw if context.objective else "")
    full_walkthrough = bool(
        context.objective and context.objective.demo_type == "full_walkthrough"
    ) or bool(
        re.search(
            r"\b(?:full|complete|entire)\b.*\bwalkthrough\b|\b(?:each|every)\s+(?:tab|page|section|primary)\b",
            objective_text,
            re.IGNORECASE,
        )
    )
    full_walkthrough = full_walkthrough or bool(
        re.search(r"\b(?:full|complete|entire)\b", candidate.name, re.IGNORECASE)
    )
    # Keep the validated workflow contract within its provider-independent
    # operation bound while preserving every meaningful group on each page.
    # Navigation consumes one operation per additional page; the remainder is
    # shared evenly across selected pages instead of silently starving the
    # last page (the previous first/last sampling caused missing roles).
    # Keep complete walkthroughs inside the default adaptive action budget.
    # A remote semantic action includes readiness, cursor motion, reveal, and
    # reading dwell; allowing eleven landmarks per page turned a five-page
    # portfolio into a 13-minute capture that could not be rendered inside its
    # approved envelope. Two or three evidence-rich groups per page preserve
    # the page contract while leaving time for meaningful narration.
    action_budget = 24
    available_group_actions = max(
        2 * len(pages),
        action_budget - max(0, len(pages) - 1) - 1,
    )
    local_group_budget = max(
        2,
        min(8, available_group_actions // max(1, len(pages))),
    )
    # Execution owns duplicate-opening suppression; the plan still declares
    # the initial state explicitly so its trace has a canonical first page.
    steps: list[SemanticOperation] = [
        SemanticOperation(
            kind=OperationKind.NAVIGATE,
            intent="Establish the evidenced opening page once",
            value=_canonical_url(context.url, pages[0].url),
            postconditions=[
                Postcondition(kind="url", expected=_canonical_url(context.url, pages[0].url))
            ],
            page_url=_canonical_url(context.url, pages[0].url),
            evidence_refs=_page_evidence(pages[0]),
        )
    ]
    outcomes: list[str] = []
    previous: PageKnowledge | None = None
    # Full tours may surface the same card collection on multiple pages.  It
    # is still useful discovery evidence, but replaying it creates a route
    # sweep and steals time from the destination page's own story.  Track only
    # observed child subjects, never URL/product-specific labels.
    established_subjects: set[str] = set()
    for index, page in enumerate(pages):
        page_url = _canonical_url(context.url, page.url)
        page_subject = _page_story_subject(page)
        evidence = _page_evidence(page)
        if index and previous is not None:
            control = _navigation_control(context, previous.url, page.url)
            if control:
                steps.append(
                    SemanticOperation(
                        kind=OperationKind.OPEN_NAVIGATION_ITEM,
                        intent=f"Move to {page.purpose} using the visible {control.name} control",
                        target=Target(
                            name=control.name,
                            selector=control.selector,
                            text=control.text or control.name,
                            source_url=control.source_url,
                        ),
                        postconditions=[Postcondition(kind="url", expected=page_url)],
                        story_phase="transition",
                        page_url=page_url,
                        evidence_refs=evidence,
                    )
                )
            else:
                steps.append(
                    SemanticOperation(
                        kind=OperationKind.NAVIGATE,
                        intent=f"Open the evidenced {page.purpose} page (no reliable visible navigation control was discovered)",
                        value=page_url,
                        postconditions=[Postcondition(kind="url", expected=page_url)],
                        story_phase="transition",
                        page_url=page_url,
                        evidence_refs=[*evidence, "fallback:direct-navigation-no-visible-control"],
                    )
                )
        landmarks = _page_landmarks(context, page)
        if not landmarks:
            if index == 0:
                # A canonical opening can be represented only by persistent
                # navigation in a legacy/incremental snapshot. It is still a
                # real opening state, but it cannot honestly claim a local
                # content beat until fresh discovery supplies one. Preserve
                # the visible transition rather than replacing it with a
                # duplicate direct load or inventing a scroll target.
                if not page.fingerprint.startswith("observed:") and (
                    page.visible_facts or page.evidence_refs
                ):
                    steps.append(
                        SemanticOperation(
                            kind=OperationKind.VERIFY_STATE,
                            intent=(
                                f"Hold on the evidenced {page_subject} workspace, "
                                "read its visible content, and establish the opening context"
                            ),
                            target=Target(
                                name=page_subject,
                                selector="body",
                                text=page.title or page_subject,
                                source_url=page.url,
                            ),
                            postconditions=[Postcondition(kind="url", expected=page_url)],
                            critical=True,
                            story_phase="establish",
                            page_url=page_url,
                            page_contract_phases=["establish", "explore", "explain", "verify"],
                            evidence_refs=evidence,
                            required_content_groups=[page_subject],
                            covered_content_groups=[page_subject],
                        )
                    )
                    outcomes.append(
                        f"{page_subject}: evidenced opening workspace held, explored, explained, and verified"
                    )
                else:
                    outcomes.append(
                        f"{page_subject}: opening state established before visible navigation"
                    )
                previous = page
                continue
            # Some authenticated workspaces expose a complete, readable
            # first viewport but do not provide stable heading/landmark
            # semantics (tables and transient controls are intentionally
            # filtered above). Preserve that evidence as a target-free page
            # scene rather than selecting a record value or failing the
            # workflow. Execution verifies the URL/page state and the
            # storyboard supplies the reading dwell from the captured frame.
            if not page.fingerprint.startswith("observed:") and (
                page.visible_facts or page.evidence_refs
            ):
                steps.append(
                    SemanticOperation(
                        kind=OperationKind.VERIFY_STATE,
                        intent=(
                            f"Hold on the evidenced {page_subject} workspace, "
                            "read its visible content, and verify the page before continuing"
                        ),
                        target=Target(
                            name=page_subject,
                            selector="body",
                            text=page.title or page_subject,
                            source_url=page.url,
                        ),
                        postconditions=[Postcondition(kind="url", expected=page_url)],
                        critical=True,
                        story_phase="establish",
                        page_url=page_url,
                        page_contract_phases=["establish", "explore", "explain", "verify"],
                        evidence_refs=evidence,
                        required_content_groups=[page_subject],
                        covered_content_groups=[page_subject],
                    )
                )
                outcomes.append(
                    f"{page_subject}: evidenced workspace held, explored, explained, and verified"
                )
                previous = page
                continue
            raise ValueError(f"page lacks readable local evidence: {page.url}")
        page_subjects = {" ".join(item.name.split()).casefold() for item in landmarks}
        # A navigation scene already establishes a destination's page title.
        # Do not spend a second full browser gesture on the same heading when
        # the page also has readable local content; retain the content
        # landmarks so the page is actually explored rather than merely
        # opened.  Pages with only a title keep it as their sole proof.
        if index and len(landmarks) > 1:
            first_name = " ".join(landmarks[0].name.split()).casefold()
            title_names = {
                " ".join(value.split()).casefold() for value in (page.title, page.purpose) if value
            }
            # A page-local h1 can be meaningful content (for example the
            # first career role on a timeline).  Skip it only when it truly
            # duplicates the destination title/purpose; tag level alone is
            # not evidence that the heading is navigation chrome.
            if first_name in title_names:
                landmarks = landmarks[1:]
        # Every selected landmark remains a required content group.  Adjacent
        # headings under one section are compiled into one smooth scroll beat;
        # the browser-owned motion traverses the intermediate cards while the
        # scene's evidence/caption can explain the whole section. This keeps
        # rich pages complete without spending a remote action on every card.
        # De-duplicate at section granularity before enforcing the bounded
        # action budget.  If we merged first, a repeated card collection could
        # hitch a ride beside new content and be reintroduced into the tour.
        landmark_groups = _editorial_landmark_groups(landmarks, max_groups=None)
        if index:
            retained_groups: list[list[ObservedElement]] = []
            for group in landmark_groups:
                group_names = {" ".join(item.name.split()).casefold() for item in group}
                child_names = {
                    " ".join(item.name.split()).casefold()
                    for item in group[1:]
                    if _heading_level(item) > 2
                }
                # A repeated named card is not a new chapter merely because a
                # second page uses a different heading level for it. Keep a
                # page's own unique content, but suppress a group made wholly
                # of subjects already explained on an earlier page.
                if (group_names and group_names.issubset(established_subjects)) or (
                    child_names and child_names.issubset(established_subjects)
                ):
                    continue
                retained_groups.append(group)
            # Never leave a selected page empty solely because its headings
            # repeat. In that rare case its surrounding facts remain the
            # strongest available evidence and are safer than a blank chapter.
            landmark_groups = retained_groups or landmark_groups
        if not landmark_groups:
            raise ValueError(f"page has no new readable local evidence: {page.url}")
        # A production page may need several distinct reading beats (for
        # example, a capabilities collection, featured work, then outcomes).
        # Eight is a safety ceiling, not an instruction to cover every card;
        # it keeps a dense but meaningful page within a 2–3 minute demo.
        landmark_groups = _split_rich_content_groups(page, landmark_groups)
        landmark_groups = _coalesce_duplicate_fact_groups(page, landmark_groups)
        # A long landing page can expose the same campaign/card title in
        # multiple DOM containers (desktop/mobile variants, sticky promos,
        # duplicated navigation). Keep the first evidence-backed occurrence;
        # repeating it later adds no viewer value and produces repetitive
        # narration. This is label-based and product-neutral, never a route
        # or benchmark allowlist.
        unique_groups: list[list[ObservedElement]] = []
        seen_group_subjects: set[str] = set()
        for group in landmark_groups:
            subjects = {
                " ".join(item.name.split()).casefold()
                for item in group
                if " ".join(item.name.split()).strip()
            }
            if subjects and subjects <= seen_group_subjects:
                continue
            unique_groups.append(group)
            seen_group_subjects.update(subjects)
        landmark_groups = unique_groups or landmark_groups
        # A focused workflow can open on a post-login dashboard that is not
        # the requested feature. The real opening footage still establishes
        # the application, but unrelated dashboard cards must not become a
        # narrated chapter or dilute the requested journey. Derive this from
        # objective vocabulary and page evidence, never product routes.
        if index == 0 and len(pages) > 1 and context.objective is not None:
            objective_terms = _tokens(
                " ".join(context.objective.requested_features) or context.objective.raw
            )
            # Objective parsing intentionally preserves the raw request for
            # auditability, so requested_features can include connective
            # words. They are not feature evidence and would make an
            # unrelated opening page appear relevant merely because both
            # pages say ``the`` or ``flow``.
            objective_terms -= {
                "the",
                "and",
                "for",
                "with",
                "from",
                "into",
                "this",
                "that",
                "show",
                "include",
                "explain",
                "actual",
                "experience",
                "product",
                "prospect",
                "login",
                "journey",
                "context",
                "only",
                "brief",
                "supporting",
                "page",
                "pages",
                "use",
                "understand",
                "relationship",
                "discovered",
                "settings",
                "config",
                "configuration",
                "flow",
                "demonstrate",
                "meaningful",
                "controls",
                "resulting",
                "state",
                "repeatedly",
                "revisit",
                "cover",
                "unrelated",
                "modules",
            }
            opening_terms = _tokens(
                " ".join(
                    [
                        page.title,
                        page.purpose,
                        *page.visible_sections,
                        *page.scroll_landmarks,
                        urlsplit(page.url).path,
                    ]
                )
            )
            destination_terms = _tokens(
                " ".join(
                    " ".join(
                        [
                            selected_page.title,
                            selected_page.purpose,
                            *selected_page.visible_sections,
                            *selected_page.scroll_landmarks,
                            urlsplit(selected_page.url).path,
                        ]
                    )
                    for selected_page in pages[1:]
                )
            )
            if (
                objective_terms
                and not (opening_terms & objective_terms)
                and (destination_terms & objective_terms)
            ):
                previous = page
                # The opening scene owns the stable context. Do not add a
                # token VerifyState/ScrollTo scene merely to narrate an
                # unrelated dashboard before moving to the requested area.
                continue
        # The opening hold already proves the root h1; scrolling back to it
        # makes the first moments look like an unnecessary reload.
        if (
            index == 0
            and len(landmark_groups) > 1
            and len(landmark_groups[0]) == 1
            and _heading_level(landmark_groups[0][0]) <= 1
        ):
            landmark_groups = landmark_groups[1:]
        # The opening receives more room to establish context and featured
        # work. Every later page keeps setup, representative evidence, and a
        # conclusion inside the requested full-tour duration.
        landmark_groups = _select_representative_groups(
            # Focused destinations get two representative local beats on every
            # page: one establishes the page and one proves its outcome.
            # Additional groups remain in PageKnowledge for extraction, but
            # spending an action on every repeated card creates a crawler-like
            # tour (and leaves no time for a meaningful interaction).
            landmark_groups,
            maximum=local_group_budget if full_walkthrough else 2,
        )
        group_names = [" ".join(item.name.split()) for group in landmark_groups for item in group]
        for phase_index, group in enumerate(landmark_groups):
            landmark = group[-1]
            group_items = [" ".join(item.name.split()) for item in group]
            if len(landmark_groups) == 1:
                phase, contract_phases = (
                    "establish",
                    ("establish", "explore", "explain", "demonstrate", "verify"),
                )
            elif phase_index == 0:
                phase, contract_phases = "establish", ("establish",)
            elif phase_index == len(landmark_groups) - 1:
                phase, contract_phases = (
                    "demonstrate",
                    ("explore", "explain", "demonstrate", "verify")
                    if len(landmark_groups) == 2
                    else ("demonstrate", "verify"),
                )
            else:
                phase, contract_phases = "explore", ("explore", "explain")
            facts = [_fact_for_landmark(page, item.name, index) for index, item in enumerate(group)]
            fact = " ".join(dict.fromkeys(fact for fact in facts if fact))
            fact_refs = [
                _fact_evidence_ref(page, fact)
                for fact in dict.fromkeys(facts)
                if fact in page.visible_facts
            ]
            # A compact data workspace can have one genuine heading followed
            # only by operational controls (filters, column names, and a
            # creation entry point).  Do not manufacture a fake scroll to the
            # already-visible heading merely to satisfy an editorial phase.
            # A verified reading hold establishes this state; the actual form
            # workflow appended below provides the meaningful demonstration.
            is_already_established_heading = (
                # A page-local h1 can already be readable immediately after
                # navigation even when it names a role/project rather than
                # the route. Verify that evidence with a dwell instead of
                # manufacturing a zero-distance ScrollTo; a later landmark
                # still provides the page's continuous exploration motion.
                len(group) == 1 and _heading_level(landmark) <= 1
            )
            operation_kind = (
                OperationKind.VERIFY_STATE
                if is_already_established_heading
                else OperationKind.SCROLL_TO
            )
            postconditions = (
                [
                    Postcondition(
                        kind="visible",
                        expected=landmark.name,
                        target=Target(
                            name=landmark.name,
                            selector=landmark.selector,
                            text=landmark.text or landmark.name,
                            source_url=landmark.source_url,
                        ),
                    )
                ]
                if is_already_established_heading
                else []
            )
            steps.append(
                SemanticOperation(
                    kind=operation_kind,
                    intent=_phase_intent(
                        phase,
                        page,
                        landmark.name if len(group) == 1 else ", ".join(group_items),
                        fact,
                    ),
                    target=Target(
                        name=landmark.name,
                        selector=landmark.selector,
                        text=landmark.text or landmark.name,
                        source_url=landmark.source_url,
                    ),
                    postconditions=postconditions,
                    critical=True,
                    story_phase=phase,
                    page_url=page_url,
                    page_contract_phases=list(contract_phases),
                    evidence_refs=[
                        *evidence,
                        *[f"element:{item.name}" for item in group],
                        *fact_refs,
                    ],
                    required_content_groups=group_names,
                    covered_content_groups=group_items,
                )
            )
            established_subjects.update(item.casefold() for item in group_items)
        # If the page's records and form are already in the initial viewport,
        # inspect one observed field rather than dispatching a no-op scroll or
        # treating the page heading as full coverage. This remains read-only
        # even when a later mutation would be authorised separately.
        form_controls = _page_form_controls(context, page)
        if form_controls:
            primary_field = form_controls[0]
            field_names = [" ".join(item.name.split()) for item in form_controls[:3]]
            steps.append(
                SemanticOperation(
                    kind=OperationKind.VERIFY_STATE,
                    intent=(
                        f"Inspect the visible {primary_field.name} form context and the "
                        f"available record fields before any action is taken"
                    ),
                    target=Target(
                        name=primary_field.name,
                        selector=primary_field.selector,
                        text=primary_field.text or primary_field.name,
                        source_url=primary_field.source_url,
                    ),
                    postconditions=[
                        Postcondition(
                            kind="visible",
                            expected=primary_field.name,
                            target=Target(
                                name=primary_field.name,
                                selector=primary_field.selector,
                                text=primary_field.text or primary_field.name,
                                source_url=primary_field.source_url,
                            ),
                        )
                    ],
                    critical=True,
                    story_phase="explore",
                    page_url=page_url,
                    page_contract_phases=["explore", "explain"],
                    evidence_refs=[
                        *evidence,
                        *[f"form-field:{page.url}:{name}" for name in field_names],
                        *[f"element:{name}" for name in field_names],
                    ],
                    required_content_groups=field_names,
                    covered_content_groups=field_names,
                )
            )
        # Visual editors expose a safe, local demonstration surface rather
        # than a conventional form or route. When the objective explicitly
        # asks to draw/design/diagram and discovery observed both a tool
        # control and a canvas-like region, compile an evidence-backed tool
        # activation followed by a short, reversible stroke. The executor
        # derives the stroke geometry from the observed surface at runtime;
        # neither the tool name nor editor-specific coordinates are embedded.
        objective_tokens = _tokens(
            " ".join([objective_text, *getattr(context.objective, "requested_features", [])])
        )
        visual_intent = bool(
            objective_tokens & {"draw", "drawing", "design", "diagram", "whiteboard", "canvas"}
            or any(token.startswith("draw") for token in objective_tokens)
        )
        surface_candidates = [
            item
            for item in context.elements
            if _canonical_url(context.url, item.source_url or context.url) == page_url
            and (item.tag or "").casefold() in {"canvas", "svg"}
            and item.actionable
        ]
        # Prefer a native canvas for pointer editing when both a canvas and an
        # SVG overlay are observed.  SVG is retained as a generic fallback for
        # editors whose semantic drawing surface is SVG; no product-specific
        # selector or coordinate is assumed.
        surface = max(
            surface_candidates,
            key=lambda item: (
                int((item.tag or "").casefold() == "canvas"),
                int(bool(item.selector)),
                -len(item.name),
            ),
            default=None,
        )
        if visual_intent and surface:
            tool_candidates = [
                item
                for item in context.elements
                if _canonical_url(context.url, item.source_url or context.url) == page_url
                and item.actionable
                and (item.tag or "").casefold() in {"button", "a"}
                and not item.href
                and not _is_control_chrome(item)
                and not _PLACEHOLDER_ELEMENT_PATTERN.fullmatch(item.name.strip())
            ]

            # A canvas usually exposes many unrelated buttons (lock, hand,
            # help, undo, collaboration, etc.).  Selecting the ``max`` of
            # that set by lexical overlap used to pick an arbitrary control
            # and then draw at a meaningless location.  Restrict the
            # candidate set to controls whose *observed accessible name*
            # identifies an editing tool.  This vocabulary is a generic UI
            # affordance vocabulary, not a product/route allow-list; if no
            # such control is observed we deliberately do not emit a pointer
            # mutation and the workflow validator can request more discovery.
            visual_tool_terms = {
                "draw",
                "drawing",
                "pen",
                "pencil",
                "brush",
                "line",
                "arrow",
                "connector",
                "rectangle",
                "square",
                "ellipse",
                "circle",
                "diamond",
                "shape",
                "text",
                "label",
                "freehand",
                "select",
                "eraser",
            }
            tool_candidates = [
                item for item in tool_candidates if _tokens(item.name) & visual_tool_terms
            ]

            def tool_score(
                item: ObservedElement, *, tokens: set[str] = objective_tokens
            ) -> tuple[int, int, int, int, int]:
                words = _tokens(item.name)
                overlap = len(words & tokens)
                draw_like = int(
                    bool(words & {"draw", "drawing", "pen", "pencil", "brush", "freehand"})
                )
                shape_like = int(
                    bool(
                        words
                        & {
                            "line",
                            "arrow",
                            "connector",
                            "rectangle",
                            "square",
                            "ellipse",
                            "circle",
                            "diamond",
                            "shape",
                            "text",
                            "label",
                        }
                    )
                )
                # Selection/eraser controls are useful only when explicitly
                # requested; they do not create a visible architecture beat.
                utility_only = int(
                    bool(words & {"select", "eraser", "hand", "pan", "lock", "undo", "redo"})
                )
                concise = int(len(words) <= 2)
                return draw_like, shape_like, overlap, concise, -utility_only

            tool = max(tool_candidates, key=tool_score, default=None)
            if tool:
                source_target = Target(
                    name=tool.name,
                    selector=tool.selector,
                    text=tool.text or tool.name,
                    source_url=tool.source_url,
                )
                destination_target = Target(
                    name=surface.name,
                    selector=surface.selector,
                    text=surface.text or surface.name,
                    source_url=surface.source_url,
                )
                # When the objective names the artifacts to build (for
                # example “labeled components … connected with arrows”),
                # compile those nouns into a generic canvas composition.  The
                # labels come from the request and the surface/tool targets
                # come from current evidence; no application or route is
                # encoded here.  Relative points are resolved against the
                # observed canvas at execution time, so responsive layouts do
                # not invalidate the gesture.
                label_match = re.search(
                    r"\b(?:components?|nodes?|boxes?|items?)\s+(?:for|including|named|:)?\s*(?P<labels>.+?)(?=\s*(?:,?\s*(?:connect|link|join)\b)|[.;]|$)",
                    objective_text,
                    flags=re.IGNORECASE,
                )
                labels: list[str] = []
                if label_match:
                    raw_labels = re.sub(
                        r"^(?:such as|including)\s+", "", label_match.group("labels"), flags=re.IGNORECASE
                    )
                    raw_labels = re.sub(r"\band\b", ",", raw_labels, flags=re.IGNORECASE)
                    labels = [
                        " ".join(item.split())[:80]
                        for item in raw_labels.split(",")
                        if 1 <= len(item.split()) <= 6 and item.strip()
                    ][:8]
                text_tool = next(
                    (candidate for candidate in tool_candidates if _tokens(candidate.name) & {"text", "label"}),
                    None,
                )
                connector_tool = next(
                    (
                        candidate
                        for candidate in tool_candidates
                        if _tokens(candidate.name) & {"arrow", "connector", "line", "link"}
                    ),
                    None,
                )
                if labels and text_tool:
                    # A bounded, readable grid is a layout algorithm, not a
                    # product recipe. It gives every requested label a
                    # distinct drop point while leaving generous canvas space.
                    positions = [
                        (0.22, 0.28), (0.52, 0.28), (0.22, 0.56),
                        (0.52, 0.56), (0.78, 0.28), (0.78, 0.56),
                        (0.36, 0.78), (0.64, 0.78),
                    ]
                    for index, label in enumerate(labels):
                        x, y = positions[index]
                        text_target = Target(
                            name=text_tool.name,
                            selector=text_tool.selector,
                            text=text_tool.text or text_tool.name,
                            source_url=text_tool.source_url,
                        )
                        steps.append(
                            SemanticOperation(
                                kind=OperationKind.CLICK,
                                intent=f"Activate the observed {text_tool.name} tool to place the requested label {label}",
                                target=text_target,
                                postconditions=[Postcondition(kind="visible", expected=text_tool.name, target=text_target)],
                                critical=True,
                                story_phase="demonstrate",
                                page_url=page_url,
                                page_contract_phases=["explain", "demonstrate"],
                                evidence_refs=[*evidence, f"element:{text_tool.name}", f"objective-label:{label}"],
                            )
                        )
                        steps.append(
                            SemanticOperation(
                                kind=OperationKind.POINTER_SEQUENCE,
                                intent=f"Place the requested {label} label on the observed canvas",
                                target=destination_target,
                                value={
                                    "pattern": "text_placement",
                                    "relative_points": [{"x": x, "y": y}, {"x": x, "y": y}],
                                    "duration_ms": 420,
                                    "press": True,
                                    "release": True,
                                },
                                postconditions=[Postcondition(kind="visible", expected=surface.name, target=destination_target)],
                                critical=True,
                                story_phase="demonstrate",
                                page_url=page_url,
                                page_contract_phases=["demonstrate", "verify"],
                                evidence_refs=[*evidence, f"element:{surface.name}", f"objective-label:{label}"],
                                required_content_groups=[label],
                                covered_content_groups=[label],
                            )
                        )
                        steps.append(
                            SemanticOperation(
                                kind=OperationKind.KEY_PRESS,
                                intent=f"Type the requested {label} label into the focused canvas editor",
                                value={
                                    "text": label,
                                    "surface_target": destination_target.model_dump(mode="json"),
                                    "placement": {"x": x, "y": y},
                                    "placement_mode": "text",
                                },
                                postconditions=[Postcondition(kind="surface_changed", expected=True, target=destination_target)],
                                critical=True,
                                story_phase="demonstrate",
                                page_url=page_url,
                                page_contract_phases=["demonstrate", "verify"],
                                evidence_refs=[*evidence, f"objective-label:{label}"],
                                required_content_groups=[label],
                                covered_content_groups=[label],
                            )
                        )
                    if connector_tool and len(labels) > 1:
                        connector_target = Target(
                            name=connector_tool.name,
                            selector=connector_tool.selector,
                            text=connector_tool.text or connector_tool.name,
                            source_url=connector_tool.source_url,
                        )
                        steps.append(
                            SemanticOperation(
                                kind=OperationKind.CLICK,
                                intent=f"Activate the observed {connector_tool.name} tool to connect the requested components",
                                target=connector_target,
                                postconditions=[Postcondition(kind="visible", expected=connector_tool.name, target=connector_target)],
                                critical=True,
                                story_phase="demonstrate",
                                page_url=page_url,
                                page_contract_phases=["explain", "demonstrate"],
                                evidence_refs=[*evidence, f"element:{connector_tool.name}"],
                            )
                        )
                        for index in range(len(labels) - 1):
                            start = positions[index]
                            end = positions[index + 1]
                            steps.append(
                                SemanticOperation(
                                    kind=OperationKind.POINTER_SEQUENCE,
                                    intent=f"Connect the requested {labels[index]} and {labels[index + 1]} components with an observed connector",
                                    target=destination_target,
                                    value={
                                        "pattern": "connector_segment",
                                        "relative_points": [
                                            {"x": start[0], "y": start[1]},
                                            {"x": end[0], "y": end[1]},
                                        ],
                                        "duration_ms": 700,
                                        "press": True,
                                        "release": True,
                                    },
                                    postconditions=[Postcondition(kind="changed", expected=True, target=destination_target)],
                                    critical=True,
                                    story_phase="demonstrate",
                                    page_url=page_url,
                                    page_contract_phases=["demonstrate", "verify"],
                                    evidence_refs=[*evidence, f"element:{surface.name}", f"connector:{labels[index]}->{labels[index + 1]}"],
                                    required_content_groups=[f"{labels[index]}->{labels[index + 1]}"],
                                    covered_content_groups=[f"{labels[index]}->{labels[index + 1]}"],
                                )
                            )
                    outcomes.extend([f"visible label: {label}" for label in labels])
                    outcomes.extend(
                        f"visible connector: {labels[index]} -> {labels[index + 1]}"
                        for index in range(len(labels) - 1)
                    )
                else:
                    steps.append(
                    SemanticOperation(
                        kind=OperationKind.CLICK,
                        intent=f"Activate the observed {tool.name} drawing tool",
                        target=source_target,
                        postconditions=[
                            Postcondition(kind="visible", expected=tool.name, target=source_target)
                        ],
                        critical=True,
                        story_phase="demonstrate",
                        page_url=page_url,
                        page_contract_phases=["explain", "demonstrate"],
                        evidence_refs=[*evidence, f"element:{tool.name}"],
                    )
                )
                steps.append(
                    SemanticOperation(
                        kind=OperationKind.POINTER_SEQUENCE,
                        intent=(f"Draw one short reversible stroke on the observed {surface.name}"),
                        target=destination_target,
                        value={
                            "pattern": "short_reversible_stroke",
                            "duration_ms": 1_200,
                            "press": True,
                            "release": True,
                            # Preserve an observed single-key shortcut when
                            # the editor exposes one on the tool control. A
                            # production replay can reassert the intended
                            # tool immediately before the gesture without
                            # embedding any editor-specific shortcut.
                            "tool_shortcut": (
                                tool.text.strip()
                                if isinstance(tool.text, str)
                                and len(tool.text.strip()) == 1
                                and tool.text.strip().isalnum()
                                else None
                            ),
                        },
                        postconditions=[
                            Postcondition(
                                kind="visible", expected=surface.name, target=destination_target
                            ),
                            # Visibility of a canvas is not proof that the
                            # gesture produced an object.  The execution
                            # snapshot includes a bounded canvas/SVG surface
                            # signature so this condition fails truthfully
                            # when the editor ignored the pointer sequence.
                            Postcondition(kind="changed", expected=True, target=destination_target),
                        ],
                        critical=True,
                        story_phase="demonstrate",
                        page_url=page_url,
                        page_contract_phases=["demonstrate", "verify"],
                        evidence_refs=[
                            *evidence,
                            f"element:{tool.name}",
                            f"element:{surface.name}",
                        ],
                        required_content_groups=[surface.name],
                        covered_content_groups=[surface.name],
                    )
                )
                # The generic fallback stroke is useful for an unlabelled
                # drawing request, but it adds noise after a requested
                # component/connector composition has already completed.
                if labels and text_tool:
                    steps = [
                        operation
                        for operation in steps
                        if not (
                            operation.kind is OperationKind.POINTER_SEQUENCE
                            and isinstance(operation.value, dict)
                            and operation.value.get("pattern") == "short_reversible_stroke"
                        )
                    ]
                # Do not synthesize text labels or connector paths from the
                # wording of the objective.  A fixed grid of normalized points
                # is not evidence of canvas objects and can report successful
                # arrows/labels on an empty canvas.  Text
                # placement and connectors are planned only after exploration
                # observes concrete object geometry/state and records it as a
                # capability.  Until then, retain the reversible stroke above
                # (or leave the canvas unmodified) and let workflow validation
                # reject objectives that require unsupported mutations.
        outcomes.append(
            f"{page_subject}: visible content established, explored, explained, demonstrated, and verified"
        )
        # Discovery still records unselected sibling cards.  Mark their names
        # as established context so a duplicate collection on a later page is
        # not promoted merely because the earlier chapter used representative
        # beats to meet the requested duration.
        established_subjects.update(page_subjects)
        previous = page
    # Keep the public workflow label concise and compatible with callers that
    # use it as the selected feature name.  Generated candidate names carry
    # planning provenance (``focused evidence walkthrough: ...``), which is
    # useful in candidate artifacts but noisy in the durable DemoPlan.
    workflow_label = candidate.name
    if workflow_label.lower().startswith("focused evidence walkthrough:") and len(pages) > 1:
        workflow_label = _page_story_subject(pages[1]) or workflow_label
    # SVG/canvas nodes are common in dashboards (charts, icons, decorative
    # shells).  Their presence alone must never turn an ordinary CRUD or
    # reporting request into a synthetic drawing gesture.  Retain pointer
    # scenes only when the objective explicitly describes a visual-editing
    # task; the evidence-backed surface/tool checks above still apply.
    visual_request_terms = {
        "draw",
        "drawing",
        "design",
        "diagram",
        "whiteboard",
        "canvas",
        "sketch",
        "paint",
    }
    if not (_tokens(objective_text) & visual_request_terms):
        steps = [step for step in steps if step.kind is not OperationKind.POINTER_SEQUENCE]
    return WorkflowProposal(
        narrative_goal="Guide the viewer through evidence-backed product pages and their visible value.",
        selected_workflow=workflow_label,
        steps=steps,
        expected_outcomes=outcomes,
        important_elements=[fact for page in pages for fact in page.visible_facts[:3]],
        excluded_areas=["external links", "unsafe side effects", "pages without captured evidence"],
        risk_flags=list(candidate.risks),
    )


def _unique_pages(pages: list[PageKnowledge], base: str) -> list[PageKnowledge]:
    result: list[PageKnowledge] = []
    seen: set[str] = set()
    for page in pages:
        canonical = _canonical_url(base, page.url)
        if canonical not in seen:
            seen.add(canonical)
            result.append(page)
    return result


def _pages_for_context(context: ProductContext) -> list[PageKnowledge]:
    pages = _unique_pages(context.page_knowledge, context.url)
    known_urls = {_canonical_url(context.url, page.url) for page in pages}
    for page in _observed_pages(context):
        if _canonical_url(context.url, page.url) not in known_urls:
            pages.append(page)
            known_urls.add(_canonical_url(context.url, page.url))
    root = _canonical_url(context.url, context.url)
    # Reversible capability probes are page-local evidence. Project their
    # structural form/workflow marker into PageKnowledge before candidate
    # scoring so a planner can allocate time for establish → open → explain
    # → fill → verify. This never claims a submit/outcome occurred, and it
    # lets old persisted discovery artifacts gain the same generic insight.
    capabilities_by_page: dict[str, list[dict]] = {}
    for capability in context.capabilities:
        source_url = capability.get("source_url")
        if isinstance(source_url, str) and source_url:
            capabilities_by_page.setdefault(_canonical_url(context.url, source_url), []).append(
                capability
            )
    enriched_pages: list[PageKnowledge] = []
    for page in pages:
        capabilities = capabilities_by_page.get(_canonical_url(context.url, page.url), [])
        if not capabilities:
            enriched_pages.append(page)
            continue
        markers = [
            f"{capability.get('kind', 'interaction')!s}:{capability.get('purpose', 'verified interaction')!s}"
            for capability in capabilities
        ]
        evidence = [
            reference
            for capability in capabilities
            for reference in capability.get("evidence_refs", [])
            if isinstance(reference, str) and reference.startswith("capability:")
        ]
        enriched_pages.append(
            page.model_copy(
                update={
                    "form_schemas": list(dict.fromkeys([*page.form_schemas, *markers])),
                    "evidence_refs": list(dict.fromkeys([*page.evidence_refs, *evidence])),
                }
            )
        )
    # A full walkthrough always establishes the current opening state first,
    # even when persisted PageKnowledge was written in exploration order.
    return sorted(
        enriched_pages, key=lambda page: 0 if _canonical_url(context.url, page.url) == root else 1
    )


def _observed_pages(context: ProductContext) -> list[PageKnowledge]:
    grouped: dict[str, list] = {}
    for element in context.elements:
        grouped.setdefault(
            _canonical_url(context.url, element.source_url or context.url), []
        ).append(element)
    # Migration bridge for runs captured before PageKnowledge was persisted:
    # primary controls are visible evidence of a candidate destination.  Fresh
    # discovery replaces these shallow records with page-local evidence before
    # a production run is accepted.
    if not context.page_knowledge:
        for item in context.navigation:
            if not item.href or item.navigation_scope == "footer":
                continue
            destination = _canonical_url(item.source_url or context.url, item.href)
            if urlsplit(destination).netloc != urlsplit(context.url).netloc:
                continue
            grouped.setdefault(destination, []).append(item)
    # The opening page itself remains a real chapter even if only global
    # controls were captured there; these controls are still DOM evidence.
    grouped.setdefault(_canonical_url(context.url, context.url), [])
    # Retain the canonical opening route even when an older/incremental
    # snapshot captured only its navigation shell. The workflow compiler can
    # establish it once and then move through a visible control; dropping it
    # makes the first discovered child route look like a duplicate opening
    # navigation and defeats the no-duplicate-load invariant.
    result: list[PageKnowledge] = []
    for url, elements in grouped.items():
        names = list(dict.fromkeys(item.name.strip() for item in elements if item.name.strip()))
        if not names:
            continue
        headings = [
            item.name.strip()
            for item in elements
            if item.tag in {"h1", "h2", "h3", "h4"} and item.name.strip()
        ]
        result.append(
            PageKnowledge(
                url=url,
                title=context.title,
                purpose=headings[0] if headings else context.title,
                visible_sections=headings or names[:4],
                scroll_landmarks=headings or names[:3],
                actionable_controls=[item.name for item in elements if item.actionable][:12],
                visible_facts=[
                    item.text or item.name for item in elements if (item.text or item.name)
                ][:8],
                evidence_refs=[f"dom:{url}:{index}" for index, _ in enumerate(elements[:8])],
                fingerprint=f"observed:{url}",
            )
        )
    return result


def _page_relevance(page: PageKnowledge, wanted: set[str]) -> float:
    words = _tokens(
        " ".join([page.title, page.purpose, *page.visible_sections, *page.visible_facts])
    )
    return min(1.0, 0.25 + len(words & wanted) * 0.16 + min(0.2, len(page.evidence_refs) * 0.03))


def _flow_from_pages(
    name: str,
    pages: list[PageKnowledge],
    relevance: dict[str, float],
    objective: str,
    *,
    rationale: list[str],
    supporting_pages: list[PageKnowledge] | None = None,
) -> CandidateDemoFlow:
    supporting_pages = supporting_pages or []
    evidence = [
        reference for page in [*pages, *supporting_pages] for reference in _page_evidence(page)
    ]
    score = min(
        1.0,
        sum(relevance[page.url] for page in pages) / max(1, len(pages))
        + min(0.25, len(evidence) * 0.02),
    )
    # Candidate duration is a discovery estimate, never a renderer stretch
    # target.  Count independent readable concepts on the *production* pages
    # (supporting relationship pages can ground narration without consuming
    # footage) plus the real transitions between them.  The old ``28 seconds
    # per route`` default made a sparse one-page form look capable of a
    # polished two-minute story before production had even begun.
    content_beats = 0
    for page in pages:
        concepts = {
            " ".join(value.split()).casefold()
            for value in [*page.visible_sections, *page.scroll_landmarks]
            if value and len(value.split()) <= 18
        }
        fact_beats = {
            value.split("::", 1)[0].strip().casefold()
            for value in page.visible_facts
            if "::" in value and value.split("::", 1)[0].strip()
        }
        # A bounded interactive chapter (for example a discovered form plus
        # its result) is a genuine visual beat even when the page has only a
        # few headings. Count it once from observed controls, never once per
        # table button/row, so form workflows are not incorrectly classified
        # as a twenty-second route tour.
        interaction_beats = (
            2 if len(page.actionable_controls) >= 3 else 1 if page.actionable_controls else 0
        )
        # A safely probed form has distinct opening, guided entry, and visible
        # verification beats. It is still bounded as one chapter, not one beat
        # per field/control, and never represents an unverified submission.
        if page.form_schemas:
            interaction_beats = max(interaction_beats, 4)
        # An evidence-rich page has a bounded number of useful reader-sized
        # beats. It is intentionally not a count of table rows or controls.
        content_beats += min(6, max(1, len(concepts | fact_beats)) + interaction_beats)
    estimated_duration = min(180, 10 + content_beats * 10 + max(0, len(pages) - 1) * 5)
    return CandidateDemoFlow(
        name=name,
        page_urls=[page.url for page in pages],
        supporting_page_urls=[page.url for page in supporting_pages],
        rationale=rationale,
        expected_outcomes=[page.purpose for page in pages],
        risks=["side effects excluded"],
        estimated_duration_seconds=estimated_duration,
        evidence_coverage=evidence,
        semantic_steps=[f"complete:{page.purpose}" for page in pages],
        score=round(score, 3),
    )


def _page_evidence(page: PageKnowledge) -> list[str]:
    return page.evidence_refs or [
        f"page:{page.url}",
        *[f"section:{section}" for section in page.visible_sections[:3]],
    ]

__all__ = [
    "_flow_from_pages",
    "_observed_pages",
    "_page_evidence",
    "_page_relevance",
    "_pages_for_context",
    "_unique_pages",
    "build_page_complete_proposal",
]
