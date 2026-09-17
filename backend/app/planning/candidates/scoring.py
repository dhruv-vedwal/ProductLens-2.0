"""Candidate scoring and flow selection."""

from __future__ import annotations

import re
from hashlib import sha256
from urllib.parse import urljoin, urlsplit

from app.contracts.models import (
    CandidateDemoFlow,
    ObservedElement,
    PageKnowledge,
    ProductContext,
)
from app.urls import canonical_product_url


def _tokens(value: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]{3,}", value.lower()))
    # Route labels frequently use plural nouns while an objective uses the
    # singular feature name. Keep both spellings in
    # the evidence vocabulary so feature pages are not outranked by a
    # configuration page merely because their visible copy uses a different
    # inflection.  This is intentionally language-light and product-neutral.
    tokens.update(token[:-1] for token in tuple(tokens) if token.endswith("s") and len(token) > 3)
    # Objectives often use the full noun while compact routes use its common
    # abbreviation. Keep this language-level equivalence generic.
    if "configuration" in tokens:
        tokens.add("config")
    if "config" in tokens:
        tokens.add("configuration")
    return tokens


_RELATION_ROLE_NOISE = {
    "flow",
    "management",
    "workflow",
    "experience",
    "page",
    "pages",
    "module",
    "feature",
    "area",
    "screen",
    "view",
    "lifecycle",
    "journey",
    "progression",
    "context",
}


_SENSITIVE_LABEL_PATTERN = re.compile(
    r"(?:\b\d[\d .()\-]{7,}\d\b|\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b)",
    re.IGNORECASE,
)


_PLACEHOLDER_ELEMENT_PATTERN = re.compile(r"element[-_ ]?\d+$", re.IGNORECASE)


_POLICY_PAGE_TERMS = {
    "privacy",
    "cookie",
    "cookies",
    "terms",
    "legal",
    "gdpr",
    "sitemap",
    "accessibility",
    "imprint",
    "acceptable",
    "refund",
}


def _is_table_or_record_artifact(item: ObservedElement) -> bool:
    """Keep dense record grids from becoming editorial scroll targets.

    DOM discovery quite correctly reports table headers, cells, and sort
    buttons as actionable elements.  They are execution evidence, not
    reader-sized story landmarks: selecting them produces scenes such as
    ``PATIENT`` or an individual person's name and makes a walkthrough feel
    like a crawler.  The tag/role rules are structural and therefore apply to
    any application, without a product-specific label list.
    """
    tag = (item.tag or "").casefold()
    role = (item.role or "").casefold()
    if tag in {"table", "thead", "tbody", "tfoot", "tr", "td", "th"}:
        return True
    if role in {"row", "grid", "gridcell", "columnheader", "rowheader"}:
        return True
    # Sortable column headings are often represented as an uppercase span
    # with role=button.  Exclude only compact, self-labelled controls so a
    # sentence-case CTA is not accidentally removed.
    label = " ".join((item.name or "").split())
    if role == "button" and label and label == label.upper() and len(label.split()) <= 4:
        return True
    return role == "button" and bool(label) and label == label.upper() and len(label.split()) <= 4


def _is_safe_landmark(item: ObservedElement) -> bool:
    value = " ".join(str(part or "") for part in (item.name, item.text))
    return (
        not _SENSITIVE_LABEL_PATTERN.search(value)
        and not _PLACEHOLDER_ELEMENT_PATTERN.fullmatch((item.name or "").strip())
        and not _is_table_or_record_artifact(item)
    )


def _page_identity_words(page: PageKnowledge) -> set[str]:
    """Terms that identify a page itself, excluding navigation/body chrome."""
    return _tokens(
        " ".join(
            [
                page.title,
                page.purpose,
                urlsplit(page.url).path.replace("/", " ").replace("-", " "),
            ]
        )
    )


def _page_proves_relationship_side(
    page: PageKnowledge,
    subject: str,
    counterpart: str,
    *,
    observed_labels: set[str] | None = None,
) -> bool:
    """Whether page identity proves one side of a requested relationship.

    Product requests frequently describe an entity workspace with a generic
    role word (for example, "management"), while the UI calls it by the
    entity itself. A setup/detail page must prove its distinguishing role;
    an operational page must at least prove the shared entity. This avoids
    both route-name hardcoding and the old Settings-shell false positive.
    """
    subject_words = _tokens(subject)
    counterpart_words = _tokens(counterpart)
    meaningful = subject_words - _RELATION_ROLE_NOISE
    distinguishing = meaningful - (counterpart_words - _RELATION_ROLE_NOISE)
    required = distinguishing or meaningful or subject_words
    if _page_identity_words(page) & required:
        return True
    # A settings shell may expose a named configuration control while its URL
    # and title remain generic.  Treat that exact observed control as
    # relationship evidence, but only when the caller has supplied labels
    # from the same page-local snapshot; never infer it from a route name.
    return bool(observed_labels and any(required & _tokens(label) for label in observed_labels))


def _page_observed_labels(context: ProductContext, page: PageKnowledge) -> set[str]:
    canonical = _canonical_url(context.url, page.url)
    return {
        item.name
        for item in context.elements
        if item.name and _canonical_url(context.url, item.source_url or context.url) == canonical
    }


def _candidate_proves_required_relationships(
    candidate: CandidateDemoFlow,
    pages_by_url: dict[str, PageKnowledge],
    context: ProductContext,
) -> bool:
    """Require semantic detail pages for every explicit objective relation."""
    objective = context.objective
    if objective is None:
        return True
    selected = [
        pages_by_url[url]
        for url in (
            _canonical_url(context.url, item)
            for item in [*candidate.page_urls, *candidate.supporting_page_urls]
        )
        if url in pages_by_url
    ]
    for relation in objective.supporting_relationships:
        if not relation.required:
            continue
        source_pages = [
            page
            for page in selected
            if _page_proves_relationship_side(
                page,
                relation.source,
                relation.target,
                observed_labels=_page_observed_labels(context, page),
            )
        ]
        target_pages = [
            page
            for page in selected
            if _page_proves_relationship_side(
                page,
                relation.target,
                relation.source,
            )
        ]
        if not source_pages or not target_pages:
            return False
        # A relationship connects a context/detail state to a distinct
        # operational state. A setup page mentioning the entity cannot prove
        # the workspace merely because the noun appears in its route.
        if not any(
            _canonical_url(context.url, source.url) != _canonical_url(context.url, target.url)
            for source in source_pages
            for target in target_pages
        ):
            return False
    return True


def _canonical_url(base: str, value: str) -> str:
    """Compare routes by browser-visible state, not URI escaping spelling."""
    # Keep one canonicalization policy for every layer: normalize HTTP
    # redirects to HTTPS, duplicate slashes/default documents, and query
    # parameter ordering before comparing observed and planned routes.
    return canonical_product_url(urljoin(base, value))


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
    from app.planning.candidates.proposals import _page_evidence, _pages_for_context
    candidates = candidate_flows_from_evidence(context, objective)
    if not candidates:
        return None
    wanted = _tokens(objective)
    # Requests often mention both a configuration area and the user-facing
    # feature it configures.  A context-only candidate can therefore score
    # higher than the actual workflow simply because words such as
    # ``settings`` and ``config`` repeat in its evidence.  Re-rank with a
    # generic feature-vs-context signal derived from PageKnowledge: the
    # selected flow must contain a destination page whose observed content
    # matches the requested feature terms, not merely its setup page.
    context_terms = {
        "first",
        "understand",
        "config",
        "configuration",
        "settings",
        "setting",
        "relationship",
        "context",
        "actual",
        "flow",
        "experience",
        "explain",
        "show",
        "include",
        "login",
        "avoid",
        "unrelated",
        "areas",
        "page",
        "pages",
    }
    feature_terms = wanted - context_terms
    # ``thorough`` describes depth, not breadth.  A focused request may ask
    # for a thorough feature walkthrough; treating that adjective as a full
    # product tour reintroduces the route-sweep behaviour this selector is
    # responsible for preventing.  Only explicit breadth words (full,
    # complete, entire, every/each) may opt into the all-primary-pages path.
    full = bool(context.objective and context.objective.demo_type == "full_walkthrough") or bool(
        re.search(
            r"\b(?:full|complete|entire)\b.*\bwalkthrough\b|\b(?:each|every)\s+(?:tab|page|section|primary)\b",
            objective.lower(),
        )
    )
    pages_by_url = {
        _canonical_url(context.url, page.url): page for page in _pages_for_context(context)
    }
    if full and not (wanted & _POLICY_PAGE_TERMS):
        # Provider/model candidates may still contain every discovered URL,
        # including footer-only policy documents. Sanitize those candidates at
        # the selection boundary so a model response cannot bypass the same
        # page-role contract enforced for deterministic candidates. Rebuild
        # derived fields when trimming pages; otherwise stale outcomes/evidence
        # would leak into planning and narration.
        sanitized: list[CandidateDemoFlow] = []
        for candidate in candidates:
            selected_pages = [
                page_url
                for page_url in candidate.page_urls
                if (
                    (page := pages_by_url.get(_canonical_url(context.url, page_url))) is None
                    or not _is_footer_policy_page(context, page)
                )
            ]
            selected_support = [
                page_url
                for page_url in candidate.supporting_page_urls
                if (
                    (page := pages_by_url.get(_canonical_url(context.url, page_url))) is None
                    or not _is_footer_policy_page(context, page)
                )
            ]
            if not selected_pages:
                continue
            if (
                selected_pages != candidate.page_urls
                or selected_support != candidate.supporting_page_urls
            ):
                evidence_pages = [
                    pages_by_url[_canonical_url(context.url, page_url)]
                    for page_url in [*selected_pages, *selected_support]
                    if _canonical_url(context.url, page_url) in pages_by_url
                ]
                candidate = candidate.model_copy(
                    update={
                        "page_urls": selected_pages,
                        "supporting_page_urls": selected_support,
                        "expected_outcomes": [page.purpose for page in evidence_pages],
                        "semantic_steps": [f"complete:{page.purpose}" for page in evidence_pages],
                        "evidence_coverage": [
                            reference
                            for page in evidence_pages
                            for reference in _page_evidence(page)
                        ],
                    }
                )
            sanitized.append(candidate)
        candidates = sanitized
        if not candidates:
            return None
    # A requested relationship is a planning requirement, not merely a
    # scoring hint. If discovery captured a candidate that proves both
    # semantic sides, a shorter route that omits the setup/detail evidence is
    # ineligible even when its lexical score is higher.
    relationship_candidates = [
        candidate
        for candidate in candidates
        if _candidate_proves_required_relationships(candidate, pages_by_url, context)
    ]
    relationship_context_candidates = [
        candidate for candidate in relationship_candidates if candidate.supporting_page_urls
    ]
    if relationship_context_candidates:
        candidates = relationship_context_candidates
    elif relationship_candidates:
        candidates = relationship_candidates
    opening_page = pages_by_url.get(_canonical_url(context.url, context.url))
    # A post-login application often opens directly on the requested feature.
    # In that case the opening page is the destination, not generic context.
    # Treating it as ``page_urls[0]`` only made a Settings/Template route win
    # because those pages repeated a noun from the request.  Prefer the
    # opening feature page and, when requested, at most one evidenced setup
    # page.  This is derived entirely from the objective and page evidence;
    # it contains no product or route names.
    opening_feature_match = 0
    opening_path_match = False
    if opening_page is not None and feature_terms:
        opening_words = _tokens(
            " ".join(
                [
                    opening_page.title,
                    opening_page.purpose,
                    *opening_page.visible_sections,
                    *opening_page.visible_facts,
                    urlsplit(opening_page.url).path,
                ]
            )
        )
        opening_feature_match = len(opening_words & feature_terms)
        path = urlsplit(opening_page.url).path.casefold()
        opening_path_match = any(term in path for term in feature_terms if len(term) >= 4)
    required_relationships = bool(
        context.objective
        and any(item.required for item in context.objective.supporting_relationships)
    )
    # A post-authentication opening state can be an unrelated default module.
    # Once discovery has captured a destination whose *page identity* names
    # the requested entity, discard alternatives that merely share body-copy
    # words or happen to be adjacent in the navigation graph.  This keeps a
    # booking request from selecting an arbitrary sibling route while
    # remaining completely product/URL agnostic.
    if not full and context.objective and context.objective.primary_entity:
        primary_identity_terms = _tokens(context.objective.primary_entity) - _RELATION_ROLE_NOISE
        if primary_identity_terms:
            identity_matches = [
                candidate
                for candidate in candidates
                if any(
                    page is not None and bool(primary_identity_terms & _page_identity_words(page))
                    for page_url in candidate.page_urls[1:]
                    for page in [pages_by_url.get(_canonical_url(context.url, page_url))]
                )
            ]
            if identity_matches:
                candidates = identity_matches
    if (
        opening_page is not None
        and opening_feature_match
        and opening_path_match
        and not full
        and not required_relationships
    ):
        opening_url = _canonical_url(context.url, opening_page.url)
        opening_candidates = [
            candidate
            for candidate in candidates
            if candidate.page_urls
            and _canonical_url(context.url, candidate.page_urls[0]) == opening_url
        ]
        # Keep a single configuration/support page only when its own evidence
        # matches the request's context vocabulary.  Never substitute another
        # route just because it is easy to click.
        contextual: list[CandidateDemoFlow] = []
        for candidate in opening_candidates:
            destinations = candidate.page_urls[1:]
            if len(destinations) > 1:
                continue
            if not destinations:
                contextual.append(candidate)
                continue
            page = pages_by_url.get(_canonical_url(context.url, destinations[0]))
            if page is None:
                continue
            page_words = _tokens(" ".join([page.title, page.purpose, *page.visible_sections]))
            if page_words & context_terms:
                contextual.append(candidate)
        if contextual:
            requested_context = wanted & {
                "config",
                "configuration",
                "settings",
                "setting",
                "relationship",
            }
            if requested_context:
                # Configuration is useful only when the supporting page also
                # contains evidence for the requested feature.  A generic
                # Settings shell otherwise wins ties and leaves the actual
                # Lead/Booking relationship unexplained.  This is derived
                # from observed page knowledge, never from product routes.
                with_support = []
                for candidate in contextual:
                    if len(candidate.page_urls) <= 1:
                        continue
                    support_page = pages_by_url.get(
                        _canonical_url(context.url, candidate.page_urls[1])
                    )
                    if support_page is None:
                        continue
                    support_words = _tokens(
                        " ".join(
                            [
                                support_page.title,
                                support_page.purpose,
                                *support_page.visible_sections,
                                *support_page.visible_facts,
                                *support_page.scroll_landmarks,
                            ]
                        )
                    )
                    if feature_terms and not (support_words & feature_terms):
                        continue
                    with_support.append(candidate)
                if with_support:

                    def support_detail_score(
                        candidate: CandidateDemoFlow,
                    ) -> tuple[int, int, float]:
                        """Prefer a relationship detail page over its navigation shell.

                        Both a shell and its detail page can repeat a feature
                        name in visible labels. The detail page additionally
                        identifies itself through its route/title/purpose with
                        the requested context term (for example, a
                        configuration route), which is evidence that it can
                        explain the relationship rather than merely link to it.
                        """
                        support_page = pages_by_url.get(
                            _canonical_url(context.url, candidate.page_urls[1])
                        )
                        if support_page is None:
                            return (0, 0, candidate.score)
                        identity = _tokens(
                            " ".join(
                                [
                                    support_page.title,
                                    support_page.purpose,
                                    urlsplit(support_page.url)
                                    .path.replace("/", " ")
                                    .replace("-", " "),
                                ]
                            )
                        )
                        relationship_hits = 0
                        for relation in (
                            context.objective.supporting_relationships if context.objective else []
                        ):
                            source_words = _tokens(relation.source) - {
                                "flow",
                                "management",
                                "experience",
                            }
                            relationship_hits = max(relationship_hits, len(identity & source_words))
                        return (
                            len(identity & requested_context),
                            relationship_hits,
                            candidate.score,
                        )

                    return max(with_support, key=support_detail_score)
            return max(contextual, key=lambda candidate: candidate.score)
    scored: list[tuple[float, CandidateDemoFlow]] = []
    candidate_destination_matches: dict[int, int] = {}
    for candidate in candidates:
        best_match = 0
        for page_url in candidate.page_urls[1:]:
            page = pages_by_url.get(_canonical_url(context.url, page_url))
            if page is None:
                continue
            path_terms = _tokens(urlsplit(page.url).path)
            page_words = _tokens(
                " ".join(
                    [
                        page.title,
                        page.purpose,
                        *page.visible_sections,
                        *page.visible_facts,
                    ]
                )
            )
            best_match = max(best_match, len((page_words | path_terms) & feature_terms))
        candidate_destination_matches[id(candidate)] = best_match
    has_feature_destination = any(value >= 1 for value in candidate_destination_matches.values())
    for candidate in candidates:
        words = _tokens(
            " ".join(
                [
                    candidate.name,
                    *candidate.rationale,
                    *candidate.expected_outcomes,
                    *candidate.evidence_coverage,
                ]
            )
        )
        overlap = len(words & wanted)
        breadth = len(candidate.page_urls)
        score = candidate.score + overlap * 0.08
        destination_matches: list[int] = []
        destination_path_match = False
        for page_url in candidate.page_urls[1:]:
            page = pages_by_url.get(_canonical_url(context.url, page_url))
            if page is None:
                continue
            path_match = bool(_tokens(urlsplit(page.url).path) & feature_terms)
            destination_path_match = destination_path_match or path_match
            page_words = _tokens(
                " ".join(
                    [
                        page.title,
                        page.purpose,
                        *page.visible_sections,
                        *page.visible_facts,
                    ]
                )
            )
            destination_matches.append(
                len((page_words | _tokens(urlsplit(page.url).path)) & feature_terms)
            )
        if destination_path_match:
            # A same-origin route whose path semantically names the requested
            # feature is stronger evidence than a generic setup page whose
            # body happens to mention that feature.  Keep this a bounded
            # ranking bonus; page knowledge and outcome evidence still decide
            # whether the flow is ultimately valid.
            score += 0.8
        best_destination_match = max(destination_matches, default=0)
        if best_destination_match >= 2:
            score += 0.38
        elif best_destination_match == 1:
            score += 0.12
        elif feature_terms and destination_matches and has_feature_destination:
            score -= 0.45
        if full:
            score += min(0.5, breadth * 0.08)
        else:
            # A narrow feature should not win just because a generic "full"
            # candidate contains many URLs.
            score -= max(0, breadth - 2) * 0.18
        if context.objective and context.objective.supporting_relationships:
            order_score = 0.0
            canonical_pages = [_canonical_url(context.url, page) for page in candidate.page_urls]
            for relation in context.objective.supporting_relationships:
                if not relation.required:
                    continue
                source_indexes = [
                    index
                    for index, page_url in enumerate(canonical_pages)
                    if _page_proves_relationship_side(
                        pages_by_url[page_url],
                        relation.source,
                        relation.target,
                        observed_labels=_page_observed_labels(context, pages_by_url[page_url]),
                    )
                ]
                target_indexes = [
                    index
                    for index, page_url in enumerate(canonical_pages)
                    if _page_proves_relationship_side(
                        pages_by_url[page_url],
                        relation.target,
                        relation.source,
                    )
                ]
                if source_indexes and target_indexes:
                    order_score += 0.7 if min(source_indexes) < max(target_indexes) else -0.7
            score += order_score
        scored.append((score, candidate))
    selected_candidate = max(scored, key=lambda item: item[0])[1]
    # Authentication commonly lands on a default workspace unrelated to the
    # requested feature (for example, a CRM opens its leads list before a
    # booking request). Keep that initial page as browser context, but make
    # the requested operational page the first *selected* chapter so the
    # production plan cannot spend its narrow-tour budget on an unrelated
    # module. This is driven solely by the primary entity and observed page
    # identity; relationship support pages remain attached and are never
    # promoted into the main story.
    if (
        not full
        and context.objective
        and context.objective.primary_entity
        and selected_candidate.page_urls
    ):
        primary_terms = _tokens(context.objective.primary_entity) - _RELATION_ROLE_NOISE

        def is_primary(url: str) -> bool:
            page = pages_by_url.get(_canonical_url(context.url, url))
            if page is None:
                return False
            # Page identity is intentionally title/purpose/route only. Body
            # facts often repeat global navigation labels (including the
            # requested feature), which would misclassify a post-login shell
            # as the operational destination and defeat this reordering.
            return bool(primary_terms & _page_identity_words(page))

        if primary_terms and not is_primary(selected_candidate.page_urls[0]):
            primary_pages = [url for url in selected_candidate.page_urls if is_primary(url)]
            if primary_pages:
                opening_path = urlsplit(context.url).path.casefold().rstrip("/")
                generic_opening = opening_path in {"", "/dashboard", "/home"}
                # A generic dashboard/home shell is useful context; an
                # unrelated entity route (often the post-login default) is
                # not. Keep the former as an opening chapter, but do not let
                # the latter displace the requested workflow.
                prefix = selected_candidate.page_urls[:1] if generic_opening else []
                ordered = list(dict.fromkeys([*prefix, *primary_pages]))
                ordered_pages = [
                    pages_by_url[_canonical_url(context.url, url)]
                    for url in ordered
                    if _canonical_url(context.url, url) in pages_by_url
                ]
                selected_candidate = selected_candidate.model_copy(
                    update={
                        "page_urls": ordered,
                        # Reordering is a semantic selection change, not merely a
                        # URL list edit. Refresh derived outcomes/steps/evidence
                        # so stale post-login-shell labels cannot leak into the
                        # persisted plan or narration contract.
                        "expected_outcomes": [page.purpose for page in ordered_pages],
                        "semantic_steps": [f"complete:{page.purpose}" for page in ordered_pages],
                        "evidence_coverage": [
                            reference
                            for page in [
                                *ordered_pages,
                                *[
                                    pages_by_url[_canonical_url(context.url, url)]
                                    for url in selected_candidate.supporting_page_urls
                                    if _canonical_url(context.url, url) in pages_by_url
                                ],
                            ]
                            for reference in _page_evidence(page)
                        ],
                    }
                )
    return selected_candidate


def candidate_flows_from_evidence(
    context: ProductContext, objective: str
) -> list[CandidateDemoFlow]:
    """Return several auditable alternatives, never a route-order tour.

    Discovery may provide model-ranked candidates, but they are only useful
    when every included page has fresh, visible page knowledge.  The generated
    alternatives make that invariant explicit and give planning a fallback
    that stays grounded when a provider response is unavailable.
    """
    from app.planning.candidates.proposals import (
        _flow_from_pages,
        _page_relevance,
        _pages_for_context,
    )
    pages = _pages_for_context(context)
    # Older discovery records may contain DOM evidence but not yet have been
    # materialised as PageKnowledge.  Promote that evidence here instead of
    # falling back to a route tour.  New discovery normally writes these
    # records directly; this bridge preserves resumable runs during migration.
    known = list(context.candidate_demo_flows)
    if not pages:
        return known
    wanted = _tokens(objective)
    # Match the selector's breadth semantics: thoroughness must not broaden a
    # feature walkthrough into every discovered route.
    full = bool(context.objective and context.objective.demo_type == "full_walkthrough") or bool(
        re.search(
            r"\b(?:full|complete|entire)\b.*\bwalkthrough\b|\b(?:each|every)\s+(?:tab|page|section|primary)\b",
            objective.lower(),
        )
    )
    relevance = {page.url: _page_relevance(page, wanted) for page in pages}
    ordered = sorted(pages, key=lambda page: (-relevance[page.url], pages.index(page)))
    opening = next(
        (
            page
            for page in pages
            if _canonical_url(context.url, page.url) == _canonical_url(context.url, context.url)
        ),
        pages[0],
    )
    generated: list[CandidateDemoFlow] = []
    if full:
        # A complete walkthrough covers safe *primary* sections and their
        # representative visible content.  Discovery may have inspected a
        # deep article/card to understand a section, but silently promoting
        # every such probe into a separate route chapter creates exactly the
        # route sweep we are trying to prevent. Keep a deep route only when
        # the request explicitly names it or it is the sole available detail.
        primary_pages = [
            page
            for page in pages
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
        generated.append(
            _flow_from_pages(
                "complete evidence walkthrough",
                primary_pages,
                relevance,
                objective,
                rationale=[
                    "every selected primary page has captured local evidence",
                    "opening page is completed before transition",
                ],
            )
        )
    else:
        for page in ordered:
            selection = [opening] if page.url == opening.url else [opening, page]
            generated.append(
                _flow_from_pages(
                    f"focused evidence walkthrough: {page.purpose}",
                    selection,
                    relevance,
                    objective,
                    rationale=["requested feature overlap", "only supporting context retained"],
                )
            )
        # If the request explicitly asks how a feature relates to setup or
        # configuration, add the strongest observed context page as a single
        # supporting chapter.  The actual feature page remains first and is
        # completed before this optional context; unrelated routes are never
        # pulled in merely because they are navigable.
        context_requested = wanted & {
            "config",
            "configuration",
            "settings",
            "setting",
            "relationship",
        }
        if context_requested:
            support = next(
                (
                    page
                    for page in ordered
                    if page is not opening
                    and _tokens(" ".join([page.title, page.purpose, *page.visible_sections]))
                    & context_requested
                ),
                None,
            )
            if support is not None:
                generated.append(
                    _flow_from_pages(
                        f"focused evidence walkthrough: {opening.purpose} with supporting context",
                        [opening, support],
                        relevance,
                        objective,
                        rationale=[
                            "requested feature evidence established first",
                            "explicit setup relationship is grounded",
                        ],
                    )
                )
        relationship_pages: list[PageKnowledge] = [opening]
        relationship_support: list[PageKnowledge] = []
        for relation in context.objective.supporting_relationships if context.objective else []:
            if not relation.required:
                continue
            source = next(
                (
                    page
                    for page in ordered
                    if _page_proves_relationship_side(
                        page,
                        relation.source,
                        relation.target,
                        observed_labels=_page_observed_labels(context, page),
                    )
                ),
                None,
            )
            target = next(
                (
                    page
                    for page in ordered
                    if page is not source
                    and _page_proves_relationship_side(
                        page,
                        relation.target,
                        relation.source,
                    )
                ),
                None,
            )
            if source is None or target is None:
                continue
            # Context is learned and cited during exploration. It is not
            # automatically a production chapter: a feature walkthrough must
            # not detour through setup screens unless the request explicitly
            # asks to demonstrate that configuration. This preserves a clean
            # operational journey while retaining auditable relationship proof.
            if source not in relationship_support:
                relationship_support.append(source)
            if target not in relationship_pages:
                relationship_pages.append(target)
        if relationship_support:
            generated.append(
                _flow_from_pages(
                    "focused evidence walkthrough with required context relationship",
                    relationship_pages,
                    relevance,
                    objective,
                    rationale=[
                        "explicit relationship detail is semantically grounded",
                        "operational experience is demonstrated without a setup detour",
                    ],
                    supporting_pages=relationship_support,
                )
            )
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
            if candidate.page_urls and all(
                _canonical_url(context.url, url) in page_urls for url in candidate.page_urls
            ):
                generated.append(candidate)
    deduped: dict[tuple[str, ...], CandidateDemoFlow] = {}
    for candidate in generated:
        # A page's readable duration is not determined by headings alone.
        # Interactive surfaces (forms, tables, editors, canvases, and
        # overlays) each add a bounded establish/explain/verify chapter.  Use
        # observed DOM semantics and capability evidence, never a product or
        # route name, so a sparse-looking canvas is not rejected merely
        # because its toolbar has no textual headings.  The cap keeps this an
        # evidence estimate rather than a renderer padding mechanism.
        capability_kinds: set[str] = set()
        for page_url in candidate.page_urls:
            canonical_page = _canonical_url(context.url, page_url)
            page_elements = [
                item
                for item in context.elements
                if _canonical_url(item.source_url or context.url, item.source_url or context.url)
                == canonical_page
            ]
            for item in page_elements:
                tag = (item.tag or "").casefold()
                role = (item.role or "").casefold()
                if tag in {"canvas", "svg"} or role in {"application", "graphics-document"}:
                    capability_kinds.add("visual_surface")
                if item.draggable or item.dropzone:
                    capability_kinds.add("drag_drop")
                if tag in {"input", "textarea", "select"} or role in {
                    "textbox",
                    "combobox",
                    "checkbox",
                    "radio",
                }:
                    capability_kinds.add("form")
                if item.shadow_host:
                    capability_kinds.add("shadow_dom")
            for capability in context.capabilities:
                # Resumed runs can contain pre-contract dictionaries from
                # older artifact snapshots. Read them defensively while the
                # typed RuntimeCapability model remains authoritative for new
                # records.
                capability_source = (
                    capability.get("source_url")
                    if isinstance(capability, dict)
                    else getattr(capability, "source_url", None)
                )
                capability_kind = (
                    capability.get("kind")
                    if isinstance(capability, dict)
                    else getattr(capability, "kind", None)
                )
                if (
                    isinstance(capability_source, str)
                    and _canonical_url(capability_source, capability_source) == canonical_page
                    and isinstance(capability_kind, str)
                ):
                    capability_kinds.add(capability_kind)
        if capability_kinds:
            # Visual/editing surfaces need several distinct, evidence-backed
            # beats (orient the workspace, explain controls, show the surface,
            # and verify its state) even when the DOM exposes only one canvas
            # node. Other capabilities receive smaller bounded weights; this
            # is a content estimate, not an instruction to pad footage.
            capability_weights = {
                "visual_surface": 4,
                "canvas": 4,
                "graph": 4,
                "form": 3,
                "drag_drop": 3,
                "rich_text": 3,
                "table": 2,
                "virtualized_table": 2,
                "modal": 2,
                "detail": 2,
                "iframe": 2,
                "shadow_dom": 1,
            }
            capability_beats = min(
                8,
                sum(capability_weights.get(kind, 1) for kind in capability_kinds),
            )
            candidate = candidate.model_copy(
                update={
                    "estimated_duration_seconds": min(
                        180,
                        candidate.estimated_duration_seconds + capability_beats * 10,
                    )
                }
            )
        key = (
            *(_canonical_url(context.url, url) for url in candidate.page_urls),
            "|support|",
            *(_canonical_url(context.url, url) for url in candidate.supporting_page_urls),
        )
        existing = deduped.get(key)
        if existing is None or candidate.score > existing.score:
            deduped[key] = candidate
    return sorted(deduped.values(), key=lambda candidate: candidate.score, reverse=True)


def _is_footer_policy_page(context: ProductContext, page: PageKnowledge) -> bool:
    """Return whether a policy-like page is reachable only from page chrome."""
    identity = _tokens(f"{page.title} {page.purpose}")
    if not identity & _POLICY_PAGE_TERMS:
        return False
    page_url = _canonical_url(context.url, page.url)
    controls = [*context.navigation, *context.elements]
    has_primary_entry = any(
        item.href
        and not urlsplit(item.href).fragment
        and item.navigation_scope != "footer"
        and _canonical_url(item.source_url or context.url, item.href) == page_url
        and (item.navigation_scope == "primary" or not (_tokens(item.name) & _POLICY_PAGE_TERMS))
        for item in controls
    )
    return not has_primary_entry


def _is_primary_page(context: ProductContext, page: PageKnowledge) -> bool:
    """Whether a page is an opening or top-level same-origin destination."""
    page_url = _canonical_url(context.url, page.url)
    if page_url == _canonical_url(context.url, context.url):
        return True
    # A shallow URL is not sufficient proof that a page is a primary product
    # area. Legal/consent documents commonly live at ``/<name>`` and are
    # exposed only from the footer. Keep them out of a complete walkthrough
    # unless a visible non-footer control actually establishes them as a
    # product destination.
    if _is_footer_policy_page(context, page):
        return False
    path = [segment for segment in urlsplit(page_url).path.split("/") if segment]
    return len(path) <= 1


def _route_ancestors(pages: list[PageKnowledge], page: PageKnowledge) -> list[PageKnowledge]:
    """Return captured proper route ancestors, shallowest first.

    This is route topology, not a product rule: a discovered deep settings
    detail often requires its visible parent page before its local control can
    be used. Only captured pages qualify, so no route is inferred or crawled.
    """
    target = urlsplit(page.url)
    path = [part for part in target.path.split("/") if part]
    ancestors: list[PageKnowledge] = []
    for depth in range(1, len(path)):
        ancestor_path = "/" + "/".join(path[:depth])
        match = next(
            (
                candidate
                for candidate in pages
                if urlsplit(candidate.url).scheme == target.scheme
                and urlsplit(candidate.url).netloc == target.netloc
                and urlsplit(candidate.url).path.rstrip("/") == ancestor_path
            ),
            None,
        )
        if match is not None:
            ancestors.append(match)
    return ancestors


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
            len(
                [
                    segment
                    for segment in urlsplit(_canonical_url(context.url, page.url)).path.split("/")
                    if segment
                ]
            ),
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
        insertion_page = (
            parent if parent in primary_urls else (source if source in primary_urls else parent)
        )
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
        "full",
        "complete",
        "thorough",
        "walkthrough",
        "demo",
        "video",
        "product",
        "website",
        "application",
        "pages",
        "page",
        "tabs",
        "tab",
        "show",
        "tour",
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
            groups = [
                groups[0],
                *[groups[1][index : index + 3] for index in range(0, len(groups[1]), 3)],
            ]
        else:
            groups = [groups[0], *[[item] for item in groups[1]]]
    # Sparse pages without section hierarchy (for example a list of article
    # cards) still need several visible beats. Keep at most three adjacent
    # cards per beat so every card is traversed and explained, without turning
    # a long collection into a route-like slideshow.
    if (
        len(groups) == 1
        and len(groups[0]) > 1
        and all(_heading_level(item) > 2 for item in groups[0])
    ):
        items = groups[0]
        groups = (
            [items[index : index + 3] for index in range(0, len(items), 3)]
            if len(items) > 6
            else [[item] for item in items]
        )
    # A dense h2 section often contains a card collection (featured work,
    # roles, designs, or metrics).  Keep its section heading with the first
    # cards, then continue in three-card beats.  This preserves continuous
    # document order and lets the narration identify what each visible card
    # contributes, instead of asking one caption to read a whole collection.
    expanded: list[list[ObservedElement]] = []
    for group in groups:
        parent = [group[0]] if group and _heading_level(group[0]) <= 2 else []
        children = group[len(parent) :]
        if len(children) > 3 and all(_heading_level(item) >= 3 for item in children):
            expanded.append([*parent, *children[:3]])
            expanded.extend(children[index : index + 3] for index in range(3, len(children), 3))
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
        groups[index : index + 2] = [[*groups[index], *groups[index + 1]]]
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
    from app.planning.candidates.navigation import _fact_for_landmark
    expanded: list[list[ObservedElement]] = []
    for group in groups:
        if len(group) < 3:
            expanded.append(group)
            continue
        parent = [group[0]] if _heading_level(group[0]) <= 2 else []
        children = group[len(parent) :]
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
                signature = " ".join(
                    sorted(_tokens((body[start.start() :] if start else body)[:260]))
                )
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
    from app.planning.candidates.navigation import _fact_for_landmark
    """Identify adjacent headings that expose the same observed description."""
    # Buttons, filters, and input labels often share one page-wide accessibility
    # snapshot.  Their lexical overlap is not proof that they are duplicate
    # story beats; coalescing them would collapse a data-heavy workflow into a
    # single no-op scroll.  Only heading/card evidence may be merged.
    if not any(
        (item.tag or "").lower() in {"h1", "h2", "h3", "h4", "h5", "h6"} or item.role == "heading"
        for item in group
    ):
        return ""
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
        signature = " ".join(sorted(_tokens(body[start.start() :][:260])))
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
    usually live), then distribute the remaining budget through the page using
    evidence value. Meaningful concluding groups remain eligible without being
    forced ahead of a required secondary role or project. The result is
    generic, deterministic, and never reorders a page or fabricates content.
    """
    if len(groups) <= maximum:
        return groups
    # Preserve the opening, then spend the remaining action budget on groups
    # that carry semantic content rather than blindly taking
    # evenly spaced headings.  Even spacing skipped a second career role (or
    # a representative project) on rich pages, while the score below remains
    # generic and is derived only from observed labels/text.
    # The opening group is always retained. The closing group competes on
    # semantic value instead of being forced into every small budget; forcing
    # it was the reason an internship/secondary role could disappear from a
    # portfolio chapter. A meaningful closing group still wins naturally via
    # the same evidence score.
    chosen: set[int] = {0}
    # Strong cues represent content the viewer expects from a walkthrough;
    # weaker cues keep useful context eligible without crowding those beats
    # out. These are semantic words, not acceptance-site names or routes.
    content_cues = {
        "intern": 4,
        "developer": 4,
        "engineer": 4,
        "experience": 3,
        "career": 4,
        "role": 4,
        "contribution": 4,
        "project": 4,
        "delivery": 4,
        "deliveries": 4,
        "system": 3,
        "note": 3,
        "article": 3,
        "feature": 3,
        "metric": 2,
        "achievement": 3,
        "message": 3,
        "form": 3,
        "result": 3,
        "outcome": 3,
        "detail": 2,
        "challenge": 3,
        "architecture": 3,
        "decision": 3,
        "recovery": 3,
        "scale": 3,
        "build": 3,
        "progress": 3,
        "today": 3,
        "week": 3,
        "lesson": 3,
        "module": 3,
        "stage": 2,
        "platform": 2,
        "application": 2,
        "work": 2,
    }

    def score(group: list[ObservedElement], index: int) -> tuple[int, int, int]:
        text = " ".join(
            " ".join(str(value or "").split()) for item in group for value in (item.name, item.text)
        ).casefold()
        words = set(re.findall(r"[a-z0-9]{3,}", text))
        cue_score = sum(content_cues[word] for word in words if word in content_cues)
        # A grouped collection is a real explanatory beat even when its
        # labels do not contain a domain noun (for example three named apps).
        if len(group) >= 3:
            cue_score += 1
        # Rich observed text is a useful tie-breaker, but never outweighs a
        # semantically important role/project/detail cue.
        return cue_score, min(len(text), 400), -index

    remaining = max(0, maximum - len(chosen))
    ranked = sorted(
        (index for index in range(len(groups)) if index not in chosen),
        key=lambda index: score(groups[index], index),
        reverse=True,
    )
    chosen.update(ranked[:remaining])
    return [groups[index] for index in sorted(chosen)]

__all__ = [
    "_PLACEHOLDER_ELEMENT_PATTERN",
    "_POLICY_PAGE_TERMS",
    "_RELATION_ROLE_NOISE",
    "_SENSITIVE_LABEL_PATTERN",
    "_candidate_proves_required_relationships",
    "_canonical_url",
    "_coalesce_duplicate_fact_groups",
    "_deep_page_is_explicitly_requested",
    "_editorial_landmark_groups",
    "_fact_evidence_ref",
    "_group_fact_signature",
    "_heading_level",
    "_insert_representative_detail_pages",
    "_is_footer_policy_page",
    "_is_primary_page",
    "_is_safe_landmark",
    "_is_table_or_record_artifact",
    "_limit_editorial_groups",
    "_order_pages_by_visible_navigation",
    "_page_identity_words",
    "_page_observed_labels",
    "_page_proves_relationship_side",
    "_route_ancestors",
    "_select_representative_groups",
    "_split_rich_content_groups",
    "_tokens",
    "candidate_flows_from_evidence",
    "select_candidate_flow",
]
