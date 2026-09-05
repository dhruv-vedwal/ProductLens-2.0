"""Evidence-backed candidate-flow selection and scope validation."""

from __future__ import annotations

import re
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from productlens.contracts.models import (
    CandidateDemoFlow,
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
        # Every selected landmark is a required content group.  The earlier
        # three-beat shortcut silently dropped later career roles, projects,
        # and notes from rich pages.  Pair adjacent editorial duties on the
        # same *visible* landmark, but never use that optimisation to skip a
        # discovered content group.
        group_names = [" ".join(item.name.split()) for item in landmarks]
        for phase_index, landmark in enumerate(landmarks):
            if len(landmarks) == 1:
                phase, contract_phases = "establish", ("establish", "explore", "explain", "demonstrate", "verify")
            elif phase_index == 0:
                phase, contract_phases = "establish", ("establish",)
            elif phase_index == len(landmarks) - 1:
                phase, contract_phases = (
                    "demonstrate",
                    ("explore", "explain", "demonstrate", "verify") if len(landmarks) == 2 else ("demonstrate", "verify"),
                )
            else:
                phase, contract_phases = "explore", ("explore", "explain")
            fact = _fact_for_landmark(page, landmark.name, phase_index)
            steps.append(SemanticOperation(
                kind=OperationKind.SCROLL_TO,
                intent=_phase_intent(phase, page, landmark.name, fact),
                target=Target(name=landmark.name, selector=landmark.selector, text=landmark.text or landmark.name, source_url=landmark.source_url),
                critical=True, story_phase=phase, page_url=page_url,
                page_contract_phases=list(contract_phases),
                evidence_refs=[*evidence, f"element:{landmark.name}"],
                required_content_groups=group_names,
                covered_content_groups=[" ".join(landmark.name.split())],
            ))
        outcomes.append(f"{page.purpose}: visible content established, explored, explained, demonstrated, and verified")
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
    if canonical == _canonical_url(context.url, context.url):
        return _collapse_repeated_series(_prioritize_story_landmarks(unique))[:8]
    return _collapse_repeated_series(unique)[:8]


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
    """Order generic content collections before unrelated lower-page headings.

    A collection heading such as ``Featured projects`` or ``Product
    deliveries`` establishes the group; its immediately-following headings are
    the observed representative items. This works for projects, case studies,
    builds, integrations, and similar collections without knowing their titles.
    """
    if len(landmarks) < 3:
        return landmarks
    collection_words = re.compile(r"\b(?:featured|projects?|portfolio|case\s+stud(?:y|ies)?|deliveries|built|selected\s+work)\b", re.IGNORECASE)
    collection_index = next((
        index for index, item in enumerate(landmarks)
        if collection_words.search(item.name) and index < len(landmarks) - 1
    ), None)
    if collection_index is None:
        return landmarks
    opening = landmarks[0]
    collection = landmarks[collection_index]
    # A featured collection is a page-level proof, not a representative-card
    # shortcut. Retain every discovered member up to the page's bounded
    # landmark budget so a portfolio cannot claim a full Home walkthrough
    # while silently omitting later visible projects.
    collection_items = landmarks[collection_index + 1: collection_index + 8]
    seen: set[str] = set()
    ordered = []
    for item in [opening, collection, *collection_items, *landmarks]:
        key = " ".join(item.name.split()).casefold()
        if key not in seen:
            seen.add(key)
            ordered.append(item)
    return ordered


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
