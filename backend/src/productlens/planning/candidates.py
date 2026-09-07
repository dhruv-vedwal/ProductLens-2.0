"""Evidence-backed candidate-flow selection and scope validation."""

from __future__ import annotations

import re
from hashlib import sha256
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from productlens.contracts.models import (
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


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", value.lower()))


def _canonical_url(base: str, value: str) -> str:
    """Compare routes by browser-visible state, not URI escaping spelling."""
    parsed = urlsplit(urljoin(base, value))
    return urlunsplit((
        parsed.scheme.lower(), parsed.netloc.lower(), unquote(parsed.path).rstrip("/") or "/",
        parsed.query, "",
    ))


def _fact_evidence_ref(page: PageKnowledge, fact: str) -> str:
    """Create the same stable fact reference used by editorial validation.

    Planning owns the association between a scroll target and its exact
    observed copy.  Passing only the page reference let the writer select an
    unrelated heading from a dense page, which is how a project scene could
    narrate an adjacent card rather than the card currently on screen.
    """
    digest = sha256((page.url + "\n" + fact).encode()).hexdigest()[:16]
    return f"fact:{digest}"


def select_candidate_flow(context: ProductContext, objective: str) -> CandidateDemoFlow | None:
    """Choose a discovered flow by relevance, not route order.

    Full walkthroughs prefer evidence breadth. Narrow requests prefer concrete
    outcome/fact overlap and keep the opening page as the only context page.
    """
    candidates = candidate_flows_from_evidence(context, objective)
    if not candidates:
        return None
    wanted = _tokens(objective)
    full = bool(context.objective and context.objective.demo_type == "full_walkthrough") or bool(
        re.search(r"\b(?:full|complete|thorough|entire)\b.*\bwalkthrough\b|\beach tab\b|\bevery safe primary\b", objective.lower())
    )
    scored: list[tuple[float, CandidateDemoFlow]] = []
    for candidate in candidates:
        words = _tokens(" ".join([candidate.name, *candidate.rationale, *candidate.expected_outcomes, *candidate.evidence_coverage]))
        overlap = len(words & wanted)
        breadth = len(candidate.page_urls)
        score = candidate.score + overlap * 0.08
        if full:
            score += min(0.5, breadth * 0.08)
        else:
            # A narrow feature should not win just because a generic "full"
            # candidate contains many URLs.
            score -= max(0, breadth - 2) * 0.18
        scored.append((score, candidate))
    return max(scored, key=lambda item: item[0])[1]


def candidate_flows_from_evidence(context: ProductContext, objective: str) -> list[CandidateDemoFlow]:
    """Return several auditable alternatives, never a route-order tour.

    Discovery may provide model-ranked candidates, but they are only useful
    when every included page has fresh, visible page knowledge.  The generated
    alternatives make that invariant explicit and give planning a fallback
    that stays grounded when a provider response is unavailable.
    """
    pages = _pages_for_context(context)
    # Older discovery records may contain DOM evidence but not yet have been
    # materialised as PageKnowledge.  Promote that evidence here instead of
    # falling back to a route tour.  New discovery normally writes these
    # records directly; this bridge preserves resumable runs during migration.
    known = list(context.candidate_demo_flows)
    if not pages:
        return known
    wanted = _tokens(objective)
    full = bool(context.objective and context.objective.demo_type == "full_walkthrough") or bool(
        re.search(r"\b(?:full|complete|thorough|entire)\b.*\bwalkthrough\b|\beach tab\b|\bevery safe primary\b", objective.lower())
    )
    relevance = {page.url: _page_relevance(page, wanted) for page in pages}
    ordered = sorted(pages, key=lambda page: (-relevance[page.url], pages.index(page)))
    opening = next((page for page in pages if _canonical_url(context.url, page.url) == _canonical_url(context.url, context.url)), pages[0])
    generated: list[CandidateDemoFlow] = []
    if full:
        # A complete walkthrough covers safe *primary* sections and their
        # representative visible content.  Discovery may have inspected a
        # deep article/card to understand a section, but silently promoting
        # every such probe into a separate route chapter creates exactly the
        # route sweep we are trying to prevent. Keep a deep route only when
        # the request explicitly names it or it is the sole available detail.
        primary_pages = [
            page for page in pages
            if _is_primary_page(context, page) or _deep_page_is_explicitly_requested(page, wanted)
        ]
        primary_pages = _order_pages_by_visible_navigation(context, primary_pages)
        # A whole-product tour needs one representative detail when a primary
        # collection visibly leads there; otherwise it can establish a tab but
        # never demonstrate what that tab contains. Keep this bounded to one
        # discovered child per parent section. This is derived from fresh,
        # visible same-origin controls rather than product-specific paths or
        # a blanket crawl of every card/article.
        primary_pages = _insert_representative_detail_pages(context, primary_pages, pages)
        # Preserve discovery/navigation order for a whole-product journey; it
        # creates continuity and avoids returning to Home to patch coverage.
        generated.append(_flow_from_pages(
            "complete evidence walkthrough", primary_pages, relevance, objective,
            rationale=["every selected primary page has captured local evidence", "opening page is completed before transition"],
        ))
    else:
        for page in ordered:
            selection = [opening] if page.url == opening.url else [opening, page]
            generated.append(_flow_from_pages(
                f"focused evidence walkthrough: {page.purpose}", selection, relevance, objective,
                rationale=["requested feature overlap", "only supporting context retained"],
            ))
    # Keep independently discovered alternatives only when they refer to
    # known pages.  This rejects stale/deep route candidates rather than
    # letting a successful click silently broaden a story.
    page_urls = {_canonical_url(context.url, page.url) for page in pages}
    # Full walkthroughs are compiled from the freshly ordered, page-complete
    # evidence above. Reusing an older model candidate here can reintroduce a
    # detail-before-parent order (or stale route sweep) even when discovery
    # has already re-grounded the page knowledge. Persisted candidates remain
    # useful for narrow requests, where their objective-specific selection is
    # explicitly validated below.
    if not full:
        for candidate in known:
            if candidate.page_urls and all(_canonical_url(context.url, url) in page_urls for url in candidate.page_urls):
                generated.append(candidate)
    deduped: dict[tuple[str, ...], CandidateDemoFlow] = {}
    for candidate in generated:
        key = tuple(_canonical_url(context.url, url) for url in candidate.page_urls)
        existing = deduped.get(key)
        if existing is None or candidate.score > existing.score:
            deduped[key] = candidate
    return sorted(deduped.values(), key=lambda candidate: candidate.score, reverse=True)


def _is_primary_page(context: ProductContext, page: PageKnowledge) -> bool:
    """Whether a page is an opening or top-level same-origin destination."""
    page_url = _canonical_url(context.url, page.url)
    if page_url == _canonical_url(context.url, context.url):
        return True
    path = [segment for segment in urlsplit(page_url).path.split("/") if segment]
    return len(path) <= 1


def _order_pages_by_visible_navigation(
    context: ProductContext, pages: list[PageKnowledge]
) -> list[PageKnowledge]:
    """Keep the opening first, then follow observed primary navigation order."""
    by_url = {_canonical_url(context.url, page.url): page for page in pages}
    ordered: list[PageKnowledge] = []
    opening = by_url.get(_canonical_url(context.url, context.url))
    if opening is not None:
        ordered.append(opening)
    for control in [*context.navigation, *context.elements]:
        if not control.href or control.navigation_scope == "footer":
            continue
        target = _canonical_url(control.source_url or context.url, control.href)
        page = by_url.get(target)
        if page is not None and page not in ordered:
            ordered.append(page)
    for page in pages:
        if page not in ordered:
            ordered.append(page)
    # A discovered detail route can be highly relevant and appear before its
    # parent in the accessibility/URL evidence.  A human journey must still
    # establish the collection before entering its detail; otherwise it has to
    # return to the parent later, creating the same route-sweep pattern we
    # explicitly reject.  Keep visible-navigation order among equal depths.
    order_index = {id(page): index for index, page in enumerate(ordered)}
    return sorted(
        ordered,
        key=lambda page: (
            len([segment for segment in urlsplit(_canonical_url(context.url, page.url)).path.split("/") if segment]),
            order_index[id(page)],
        ),
    )


def _insert_representative_detail_pages(
    context: ProductContext,
    primary_pages: list[PageKnowledge],
    all_pages: list[PageKnowledge],
) -> list[PageKnowledge]:
    by_url = {_canonical_url(context.url, page.url): page for page in all_pages}
    primary_urls = {_canonical_url(context.url, page.url) for page in primary_pages}
    detail_by_source: dict[str, PageKnowledge] = {}
    selected_detail_urls: set[str] = set()
    for control in [*context.navigation, *context.elements]:
        if not control.href or not control.actionable or control.navigation_scope == "footer":
            continue
        target = _canonical_url(control.source_url or context.url, control.href)
        page = by_url.get(target)
        if page is None or _is_primary_page(context, page):
            continue
        if target in selected_detail_urls:
            continue
        path = [segment for segment in urlsplit(target).path.split("/") if segment]
        if len(path) < 2:
            continue
        parent = _canonical_url(context.url, "/" + path[0])
        source = _canonical_url(context.url, control.source_url or context.url)
        # Prefer the page where the semantic control was actually observed.
        # A dashboard often exposes a representative detail card before its
        # parent collection route; clicking it while it is visibly available
        # is more faithful than navigating away and later falling back to a
        # stale global selector.
        # A deep card may be visible on the opening dashboard, but its story
        # belongs after the owning collection page.  Otherwise inserting it
        # after the root causes the journey to enter detail first and later
        # return to the parent collection.
        insertion_page = parent if parent in primary_urls else (source if source in primary_urls else parent)
        if insertion_page in primary_urls:
            detail_by_source.setdefault(insertion_page, page)
            selected_detail_urls.add(target)
    ordered: list[PageKnowledge] = []
    for page in primary_pages:
        ordered.append(page)
        detail = detail_by_source.get(_canonical_url(context.url, page.url))
        if detail is not None:
            ordered.append(detail)
    return ordered


def _deep_page_is_explicitly_requested(page: PageKnowledge, objective_tokens: set[str]) -> bool:
    """Allow a deep chapter only when the request names its observed subject.

    Full-walkthrough language must not promote a discovery probe/article into a
    separate chapter.  Conversely, a request for a named deep feature should
    retain it. Generic product-tour words deliberately carry no such weight.
    """
    generic = {
        "full", "complete", "thorough", "walkthrough", "demo", "video", "product",
        "website", "application", "pages", "page", "tabs", "tab", "show", "tour",
    }
    path = [segment for segment in urlsplit(page.url).path.split("/") if segment]
    # Use the leaf route subject only. A request for the parent ``Engineering
    # Notes`` section must not accidentally request every article below it.
    path_subject = path[-1].replace("-", " ") if path else ""
    subject_tokens = _tokens(path_subject) - generic
    return bool(subject_tokens & (objective_tokens - generic))


def _heading_level(item: ObservedElement) -> int:
    """Return a conservative document-heading level for grouping evidence."""
    match = re.fullmatch(r"h([1-6])", (item.tag or "").lower())
    if match:
        return int(match.group(1))
    return 6 if item.role == "heading" else 7


def _editorial_landmark_groups(
    landmarks: list[ObservedElement], *, max_groups: int | None = 6
) -> list[list[ObservedElement]]:
    """Group adjacent content under the same section into readable scroll beats.

    Discovery keeps every meaningful heading so narration can explain the
    actual page. Production should not issue a remote browser command for every
    card, however: a single smooth scroll through a section visibly traverses
    its intermediate cards and gives the presenter one coherent thought. A
    new h1/h2 starts a chapter; lower-level headings remain in that chapter.
    This is derived entirely from DOM heading hierarchy and works for
    portfolios, dashboards, articles, and arbitrary products.
    """
    if not landmarks:
        return []
    groups: list[list[ObservedElement]] = []
    current: list[ObservedElement] = []
    for index, item in enumerate(landmarks):
        level = _heading_level(item)
        # The page title establishes the chapter on its own. Lower-level cards
        # that follow it are the page's actual content and must not be hidden
        # inside the title's target rectangle.
        if index == 0 and level <= 1 and len(landmarks) > 1:
            groups.append([item])
            continue
        if current and level <= 2:
            groups.append(current)
            current = []
        current.append(item)
    if current:
        groups.append(current)
    if (
        len(groups) == 2
        and len(groups[0]) == 1
        and _heading_level(groups[0][0]) <= 1
        and len(groups[1]) <= 6
        and all(_heading_level(item) > 2 for item in groups[1])
    ):
        # Article/card collections generally use h3 headings and can be
        # traversed in two or three-card reading beats. h4 component cards,
        # by contrast, are commonly independent selectable designs; retain
        # each target so the planner can prove every observed component.
        if all(_heading_level(item) == 3 for item in groups[1]):
            groups = [groups[0], *[groups[1][index:index + 3] for index in range(0, len(groups[1]), 3)]]
        else:
            groups = [groups[0], *[[item] for item in groups[1]]]
    # Sparse pages without section hierarchy (for example a list of article
    # cards) still need several visible beats. Keep at most three adjacent
    # cards per beat so every card is traversed and explained, without turning
    # a long collection into a route-like slideshow.
    if len(groups) == 1 and len(groups[0]) > 1 and all(_heading_level(item) > 2 for item in groups[0]):
        items = groups[0]
        groups = [items[index:index + 3] for index in range(0, len(items), 3)] if len(items) > 6 else [[item] for item in items]
    # A dense h2 section often contains a card collection (featured work,
    # roles, designs, or metrics).  Keep its section heading with the first
    # cards, then continue in three-card beats.  This preserves continuous
    # document order and lets the narration identify what each visible card
    # contributes, instead of asking one caption to read a whole collection.
    expanded: list[list[ObservedElement]] = []
    for group in groups:
        parent = [group[0]] if group and _heading_level(group[0]) <= 2 else []
        children = group[len(parent):]
        if len(children) > 3 and all(_heading_level(item) >= 3 for item in children):
            expanded.append([*parent, *children[:3]])
            expanded.extend(children[index:index + 3] for index in range(3, len(children), 3))
        else:
            expanded.append(group)
    groups = expanded
    # Keep the workflow contract bounded even when an application exposes a
    # very large number of top-level sections. Merging adjacent section groups
    # preserves every required content name and the browser's continuous
    # motion, while avoiding an unbounded remote action list.
    return _limit_editorial_groups(groups, maximum=max_groups)


def _limit_editorial_groups(
    groups: list[list[ObservedElement]], *, maximum: int | None
) -> list[list[ObservedElement]]:
    """Bound a page plan without merging a whole card collection at once."""
    if maximum is None:
        return groups
    groups = [list(group) for group in groups]
    while len(groups) > maximum:
        # Merge the smallest adjacent reading beats.  This normally folds an
        # opening h1 into its immediately visible context, rather than joining
        # two project/card beats and forcing one caption to narrate six items.
        index = min(
            range(len(groups) - 1),
            key=lambda item: (len(groups[item]) + len(groups[item + 1]), item),
        )
        groups[index:index + 2] = [[*groups[index], *groups[index + 1]]]
    return groups


def _split_rich_content_groups(
    page: PageKnowledge, groups: list[list[ObservedElement]]
) -> list[list[ObservedElement]]:
    """Give independently described cards their own readable reveal beat.

    DOM hierarchy alone cannot distinguish a compact label list from a set of
    projects, roles, designs, or articles.  The captured page facts can: when
    a lower-level landmark has an explanatory body, it deserves a separate
    pause instead of being swept past in a single long scroll.  This remains
    generic and preserves adjacent non-rich labels with their nearest card.
    """
    expanded: list[list[ObservedElement]] = []
    for group in groups:
        if len(group) < 3:
            expanded.append(group)
            continue
        parent = [group[0]] if _heading_level(group[0]) <= 2 else []
        children = group[len(parent):]
        rich = {
            index
            for index, item in enumerate(children)
            if _heading_level(item) >= 3
            and len(_tokens(_fact_for_landmark(page, item.name, index))) >= 10
        }
        if len(rich) < 2:
            expanded.append(group)
            continue
        current = list(parent)
        saw_rich = False
        seen_signatures: set[str] = set()
        for index, item in enumerate(children):
            if index in rich:
                raw_fact = _fact_for_landmark(page, item.name, index)
                body = raw_fact.split("::", 1)[-1]
                start = re.search(
                    r"\b(?:I\s+(?:engineer|build|design|focus)|This\s+|An?\s+|"
                    r"Architecting\s+|Building\s+|Designing\s+|Integrating\s+|"
                    r"Engineered\s+|Developed\s+|Optimized\s+)",
                    body,
                    flags=re.IGNORECASE,
                )
                signature = " ".join(sorted(_tokens((body[start.start():] if start else body)[:260])))
                if saw_rich and signature not in seen_signatures:
                    expanded.append(current)
                    current = []
                if signature:
                    seen_signatures.add(signature)
            current.append(item)
            saw_rich = saw_rich or index in rich
        if current:
            expanded.append(current)
    return expanded


def _group_fact_signature(page: PageKnowledge, group: list[ObservedElement]) -> str:
    """Identify adjacent headings that expose the same observed description."""
    for index, item in enumerate(reversed(group)):
        fact = _fact_for_landmark(page, item.name, index)
        body = fact.split("::", 1)[-1]
        start = re.search(
            r"\b(?:I\s+(?:engineer|build|design|focus)|This\s+|An?\s+|"
            r"Architecting\s+|Building\s+|Designing\s+|Integrating\s+|"
            r"Engineered\s+|Developed\s+|Optimized\s+)",
            body,
            flags=re.IGNORECASE,
        )
        # A label inventory is evidence that cards exist, not proof that two
        # cards mean the same thing. Only coalesce when both headings share a
        # real explanatory sentence (company/role pairs are the common case).
        if start is None:
            continue
        signature = " ".join(sorted(_tokens(body[start.start():][:260])))
        if signature:
            return signature
    return ""


def _coalesce_duplicate_fact_groups(
    page: PageKnowledge, groups: list[list[ObservedElement]]
) -> list[list[ObservedElement]]:
    """Keep a company/title or card/subtitle pair in one scene when identical."""
    merged: list[list[ObservedElement]] = []
    previous_signature = ""
    for group in groups:
        signature = _group_fact_signature(page, group)
        if merged and signature and signature == previous_signature:
            merged[-1].extend(group)
        else:
            merged.append(list(group))
        previous_signature = signature or previous_signature
    return merged


def _select_representative_groups(
    groups: list[list[ObservedElement]], *, maximum: int
) -> list[list[ObservedElement]]:
    """Keep an ordered, representative page story within the duration budget.

    A complete walkthrough means complete *meaningful* coverage, not an
    unwatchable command for every heading on a large page.  Preserve the first
    three content beats (where identity, setup, and the first concrete example
    usually live), then distribute the remaining budget through the page and
    retain its conclusion.  The result is generic, deterministic, and never
    reorders a page or fabricates unseen content.
    """
    if len(groups) <= maximum:
        return groups
    front = min(3, max(1, maximum - 2))
    chosen = list(range(front))
    remaining = maximum - front
    tail_start = front
    tail_end = len(groups) - 1
    if remaining == 1:
        chosen.append(tail_end)
    else:
        chosen.extend(
            round(tail_start + index * (tail_end - tail_start) / (remaining - 1))
            for index in range(remaining)
        )
    return [groups[index] for index in sorted(dict.fromkeys(chosen))]


def build_page_complete_proposal(context: ProductContext, candidate: CandidateDemoFlow) -> WorkflowProposal:
    """Compile evidence into a complete page journey, not generic tab labels.

    Each page receives the same editorial contract. Navigation is represented
    only by a discovered visible control; absent one, a direct URL fallback is
    explicitly marked so workflow QA can account for it.
    """
    page_by_url = {_canonical_url(context.url, page.url): page for page in _pages_for_context(context)}
    pages = [page_by_url[_canonical_url(context.url, url)] for url in candidate.page_urls if _canonical_url(context.url, url) in page_by_url]
    if not pages:
        raise ValueError("candidate has no fresh PageKnowledge")
    # Execution owns duplicate-opening suppression; the plan still declares
    # the initial state explicitly so its trace has a canonical first page.
    steps: list[SemanticOperation] = [SemanticOperation(
        kind=OperationKind.NAVIGATE, intent="Establish the evidenced opening page once",
        value=_canonical_url(context.url, pages[0].url),
        postconditions=[Postcondition(kind="url", expected=_canonical_url(context.url, pages[0].url))],
        page_url=_canonical_url(context.url, pages[0].url), evidence_refs=_page_evidence(pages[0]),
    )]
    outcomes: list[str] = []
    previous: PageKnowledge | None = None
    # Full tours may surface the same card collection on multiple pages.  It
    # is still useful discovery evidence, but replaying it creates a route
    # sweep and steals time from the destination page's own story.  Track only
    # observed child subjects, never URL/product-specific labels.
    established_subjects: set[str] = set()
    for index, page in enumerate(pages):
        page_url = _canonical_url(context.url, page.url)
        evidence = _page_evidence(page)
        if index and previous is not None:
            control = _navigation_control(context, previous.url, page.url)
            if control:
                steps.append(SemanticOperation(
                    kind=OperationKind.OPEN_NAVIGATION_ITEM,
                    intent=f"Move to {page.purpose} using the visible {control.name} control",
                    target=Target(name=control.name, selector=control.selector, text=control.text or control.name, source_url=control.source_url),
                    postconditions=[Postcondition(kind="url", expected=page_url)],
                    story_phase="transition", page_url=page_url, evidence_refs=evidence,
                ))
            else:
                steps.append(SemanticOperation(
                    kind=OperationKind.NAVIGATE,
                    intent=f"Open the evidenced {page.purpose} page (no reliable visible navigation control was discovered)",
                    value=page_url, postconditions=[Postcondition(kind="url", expected=page_url)],
                    story_phase="transition", page_url=page_url, evidence_refs=[*evidence, "fallback:direct-navigation-no-visible-control"],
                ))
        landmarks = _page_landmarks(context, page)
        if not landmarks:
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
                " ".join(value.split()).casefold()
                for value in (page.title, page.purpose)
                if value
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
                group_names = {
                    " ".join(item.name.split()).casefold()
                    for item in group
                }
                child_names = {
                    " ".join(item.name.split()).casefold()
                    for item in group[1:]
                    if _heading_level(item) > 2
                }
                # A repeated named card is not a new chapter merely because a
                # second page uses a different heading level for it. Keep a
                # page's own unique content, but suppress a group made wholly
                # of subjects already explained on an earlier page.
                if (
                    group_names and group_names.issubset(established_subjects)
                ) or (
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
        # The opening hold already proves the root h1; scrolling back to it
        # makes the first moments look like an unnecessary reload.
        if (
            index == 0 and len(landmark_groups) > 1
            and len(landmark_groups[0]) == 1
            and _heading_level(landmark_groups[0][0]) <= 1
        ):
            landmark_groups = landmark_groups[1:]
        # The opening receives more room to establish context and featured
        # work. Every later page keeps setup, representative evidence, and a
        # conclusion inside the requested full-tour duration.
        landmark_groups = _select_representative_groups(
            landmark_groups, maximum=10 if index == 0 else 6
        )
        group_names = [
            " ".join(item.name.split())
            for group in landmark_groups for item in group
        ]
        for phase_index, group in enumerate(landmark_groups):
            landmark = group[-1]
            group_items = [" ".join(item.name.split()) for item in group]
            if len(landmark_groups) == 1:
                phase, contract_phases = "establish", ("establish", "explore", "explain", "demonstrate", "verify")
            elif phase_index == 0:
                phase, contract_phases = "establish", ("establish",)
            elif phase_index == len(landmark_groups) - 1:
                phase, contract_phases = (
                    "demonstrate",
                    ("explore", "explain", "demonstrate", "verify") if len(landmark_groups) == 2 else ("demonstrate", "verify"),
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
            steps.append(SemanticOperation(
                kind=OperationKind.SCROLL_TO,
                intent=_phase_intent(
                    phase,
                    page,
                    landmark.name if len(group) == 1 else ", ".join(group_items),
                    fact,
                ),
                target=Target(name=landmark.name, selector=landmark.selector, text=landmark.text or landmark.name, source_url=landmark.source_url),
                critical=True, story_phase=phase, page_url=page_url,
                page_contract_phases=list(contract_phases),
                evidence_refs=[
                    *evidence,
                    *[f"element:{item.name}" for item in group],
                    *fact_refs,
                ],
                required_content_groups=group_names,
                covered_content_groups=group_items,
            ))
            established_subjects.update(item.casefold() for item in group_items)
        outcomes.append(f"{page.purpose}: visible content established, explored, explained, demonstrated, and verified")
        # Discovery still records unselected sibling cards.  Mark their names
        # as established context so a duplicate collection on a later page is
        # not promoted merely because the earlier chapter used representative
        # beats to meet the requested duration.
        established_subjects.update(page_subjects)
        previous = page
    return WorkflowProposal(
        narrative_goal="Guide the viewer through evidence-backed product pages and their visible value.",
        selected_workflow=candidate.name,
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
    # A full walkthrough always establishes the current opening state first,
    # even when persisted PageKnowledge was written in exploration order.
    return sorted(pages, key=lambda page: 0 if _canonical_url(context.url, page.url) == root else 1)


def _observed_pages(context: ProductContext) -> list[PageKnowledge]:
    grouped: dict[str, list] = {}
    for element in context.elements:
        grouped.setdefault(_canonical_url(context.url, element.source_url or context.url), []).append(element)
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
    root = _canonical_url(context.url, context.url)
    root_items = grouped.get(root, [])
    has_root_content = any(not item.href for item in root_items)
    has_local_content_elsewhere = any(
        route != root and any(not item.href for item in items)
        for route, items in grouped.items()
    )
    if root_items and not has_root_content and has_local_content_elsewhere:
        grouped.pop(root, None)
    result: list[PageKnowledge] = []
    for url, elements in grouped.items():
        names = list(dict.fromkeys(item.name.strip() for item in elements if item.name.strip()))
        if not names:
            continue
        headings = [item.name.strip() for item in elements if item.tag in {"h1", "h2", "h3", "h4"} and item.name.strip()]
        result.append(PageKnowledge(
            url=url, title=context.title, purpose=headings[0] if headings else context.title,
            visible_sections=headings or names[:4], scroll_landmarks=headings or names[:3],
            actionable_controls=[item.name for item in elements if item.actionable][:12],
            visible_facts=[item.text or item.name for item in elements if (item.text or item.name)][:8],
            evidence_refs=[f"dom:{url}:{index}" for index, _ in enumerate(elements[:8])],
            fingerprint=f"observed:{url}",
        ))
    return result


def _page_relevance(page: PageKnowledge, wanted: set[str]) -> float:
    words = _tokens(" ".join([page.title, page.purpose, *page.visible_sections, *page.visible_facts]))
    return min(1.0, 0.25 + len(words & wanted) * 0.16 + min(0.2, len(page.evidence_refs) * 0.03))


def _flow_from_pages(name: str, pages: list[PageKnowledge], relevance: dict[str, float], objective: str, *, rationale: list[str]) -> CandidateDemoFlow:
    evidence = [reference for page in pages for reference in _page_evidence(page)]
    score = min(1.0, sum(relevance[page.url] for page in pages) / max(1, len(pages)) + min(0.25, len(evidence) * 0.02))
    return CandidateDemoFlow(
        name=name, page_urls=[page.url for page in pages], rationale=rationale,
        expected_outcomes=[page.purpose for page in pages], risks=["side effects excluded"],
        estimated_duration_seconds=max(60, min(180, len(pages) * 28)), evidence_coverage=evidence,
        semantic_steps=[f"complete:{page.purpose}" for page in pages], score=round(score, 3),
    )


def _page_evidence(page: PageKnowledge) -> list[str]:
    return page.evidence_refs or [f"page:{page.url}", *[f"section:{section}" for section in page.visible_sections[:3]]]


def _navigation_control(context: ProductContext, source_url: str, destination: str):
    source = _canonical_url(context.url, source_url)
    target = _canonical_url(context.url, destination)
    controls = [*context.navigation, *context.elements]
    exact = next((item for item in controls if item.href and item.actionable
                 and _canonical_url(item.source_url or context.url, item.source_url or context.url) == source
                 and _canonical_url(item.source_url or context.url, item.href) == target), None)
    if exact is not None:
        return exact
    # Persistent global navigation is sometimes captured against its opening
    # URL during migration. Only a control explicitly observed in primary
    # navigation may be re-grounded on another page. Reusing arbitrary card
    # links here can send the run back into a representative detail route
    # merely because its label exists in stale discovery evidence.
    persistent_controls = [
        *context.navigation,
        *(item for item in context.elements if item.navigation_scope == "primary"),
    ]
    return next((item for item in persistent_controls if item.href and item.actionable
                 and _canonical_url(item.source_url or context.url, item.href) == target), None)


def _page_landmarks(context: ProductContext, page: PageKnowledge):
    canonical = _canonical_url(context.url, page.url)
    candidates = [item for item in context.elements if item.name.strip()
                  and item.navigation_scope != "footer"
                  and _canonical_url(context.url, item.source_url or context.url) == canonical]
    if not candidates and page.fingerprint.startswith("observed:"):
        candidates = [item for item in context.navigation if item.href and _canonical_url(item.source_url or context.url, item.href) == canonical]
    # A reading landmark is page content, not another navigation affordance.
    # In particular, a short Contact page often exposes a footer full of links;
    # treating those links as evidence would turn the last chapter into an
    # accidental route sweep. Prefer headings, then other non-actionable local
    # content. Actionable controls are a last-resort fallback for pages which
    # genuinely have no readable landmarks.
    headings = [item for item in candidates if item.tag in {"h1", "h2", "h3", "h4"} or item.role == "heading"]
    passive = [item for item in candidates if not item.actionable and item not in headings]
    actionable = [item for item in candidates if item.actionable and not item.href and item.navigation_scope != "primary"]
    reading_candidates = headings or passive or actionable
    wanted = {" ".join(value.split()).lower() for value in [*page.scroll_landmarks, *page.visible_sections]}
    reading_candidates.sort(key=lambda item: (0 if " ".join(item.name.split()).lower() in wanted else 1, 0 if item.tag in {"h1", "h2", "h3", "h4"} else 1))
    unique, names = [], set()
    for item in reading_candidates:
        name = " ".join(item.name.split()).lower()
        if name and name not in names and len(name) <= 180:
            names.add(name); unique.append(item)
    # A featured-work collection belongs to the opening portfolio story. On a
    # later page, a visually repeated project strip/footer is supporting
    # content, not permission to replace that page's own career/design/note
    # evidence. Preserve the page's inspected landmark order there.
    # Keep all discovered local landmarks in the editorial evidence set.  The
    # production compiler groups them by section below, so retaining the full
    # set does not create one crawler gesture per heading; it prevents later
    # career roles, projects, metrics, or article sections from disappearing
    # merely because they appeared after an arbitrary first-N cutoff.
    if canonical == _canonical_url(context.url, context.url):
        return _collapse_repeated_series(_prioritize_story_landmarks(unique))[:32]
    return _collapse_repeated_series(unique)[:32]


def _collapse_repeated_series(landmarks: list) -> list:
    """Keep one readable example from a repeated numbered collection.

    Discovery can correctly capture every visible sibling (Week 1..N, Day
    1..N, etc.), but promoting all siblings to production scenes creates a
    crawler-like story and exhausts the editorial budget before the viewer
    sees the result.  The collection heading plus the first representative is
    sufficient; detail routes remain available when explicitly requested.
    """
    seen_series: set[str] = set()
    result: list = []
    for item in landmarks:
        label = " ".join(item.name.split())
        key = label.casefold()
        series = re.sub(r"\b\d+\b", "#", key)
        repeated = bool(re.search(r"\b(?:week|day|module|lesson|chapter|step)\s+#(?:\s|$)", series))
        if repeated and series in seen_series:
            continue
        if repeated:
            seen_series.add(series)
        result.append(item)
    return result


def _prioritize_story_landmarks(landmarks: list):
    """Preserve the browser's document order for natural presentation.

    Earlier versions moved a detected project collection to the front of the
    list. That made the production cursor/scroll jump down to projects and
    later backtrack through capabilities and metrics, which reads as a crawler
    rather than a person following the page. Collection completeness is now
    handled by retaining all landmarks and grouping adjacent headings; the
    original DOM order is the authoritative visual journey.
    """
    return landmarks


def _fact_for_landmark(page: PageKnowledge, landmark: str, index: int) -> str:
    facts = [*page.visible_facts, *page.visible_sections]
    if not facts:
        return landmark
    # Page discovery preserves document order, but a position is not semantic
    # provenance. A later career/project card must not inherit the paragraph
    # captured for an earlier card merely because both are headings. Prefer a
    # fact introduced by the same visible subject, then the strongest lexical
    # overlap; only retain position as a deterministic final fallback.
    target_words = set(_tokens(landmark))
    scored: list[tuple[tuple[int, int, int], str]] = []
    for fact_index, fact in enumerate(facts):
        heading = fact.split("::", 1)[0].strip().lower()
        fact_words = _tokens(fact)
        overlap = len(target_words & fact_words)
        if not overlap:
            continue
        normalized_fact = " ".join(fact.split()).casefold()
        normalized_landmark = " ".join(landmark.split()).casefold()
        # A bare section label is navigational evidence, not the explanation
        # for that section. Prefer a longer captured fact even when the label
        # itself has an exact lexical match.
        bare_label = normalized_fact == normalized_landmark
        scored.append((
            (
                1 if bare_label else 0,
                0 if heading.startswith(landmark.lower()) else 1,
                -overlap,
                abs(fact_index - index),
            ),
            fact,
        ))
    if scored:
        return min(scored, key=lambda item: item[0])[1]
    return facts[min(index, len(facts) - 1)]


def _phase_intent(phase: str, page: PageKnowledge, landmark: str, fact: str) -> str:
    verbs = {
        "establish": "Establish the page purpose and orient the viewer around",
        "explore": "Explore the visible content and context around",
        "explain": "Explain the visible value evidenced by",
        "demonstrate": "Inspect the meaningful detail or behavior shown at",
        "verify": "Hold on the visible proof and verify the takeaway from",
    }
    return f"{verbs[phase]} {landmark}: {fact}"[:500]


def validate_flow_scope(
    proposal: WorkflowProposal, *, context: ProductContext, candidate: CandidateDemoFlow | None
) -> list[str]:
    """Return scope failures; a proposal cannot silently leave selected evidence."""
    if candidate is None:
        return []
    allowed = {_canonical_url(context.url, url) for url in candidate.page_urls}
    allowed.add(_canonical_url(context.url, context.url))
    failures: list[str] = []
    for operation in proposal.steps:
        if operation.kind.value not in {"Navigate", "OpenNavigationItem"}:
            continue
        destination = next(
            (str(condition.expected) for condition in operation.postconditions if condition.kind == "url"),
            str(operation.value or ""),
        )
        if not destination:
            continue
        if destination.startswith("**/"):
            suffix = destination.removeprefix("**")
            if any(url.endswith(suffix) for url in allowed):
                continue
            failures.append(f"CANDIDATE_FLOW_SCOPE_VIOLATION:{destination}")
            continue
        absolute = _canonical_url(context.url, destination)
        if absolute not in allowed:
            failures.append(f"CANDIDATE_FLOW_SCOPE_VIOLATION:{absolute}")
    return failures
