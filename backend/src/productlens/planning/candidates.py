"""Evidence-backed candidate-flow selection and scope validation."""

from __future__ import annotations

import re
from hashlib import sha256
from urllib.parse import urljoin, urlsplit

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
from productlens.urls import canonical_product_url


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

# Records discovered from authenticated applications may contain customer
# names, phone numbers, emails, or other identifiers. They are useful for
# proving that a list exists, but never safe as editorial landmarks or cursor
# targets. Keep the rule shape generic; synthetic/demo values are still
# supplied only through the dedicated safe-data layer.
_SENSITIVE_LABEL_PATTERN = re.compile(
    r"(?:\b\d[\d .()\-]{7,}\d\b|\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b)",
    re.IGNORECASE,
)

_PLACEHOLDER_ELEMENT_PATTERN = re.compile(r"element[-_ ]?\d+$", re.IGNORECASE)

# Footer/legal documents are useful evidence only when the objective asks for
# them explicitly. A full product walkthrough should not turn every legal
# link discovered in a persistent footer into a chapter. This is a semantic
# page-classification guard, not a site or route allowlist: an explicitly
# requested policy page still passes through ``_deep_page_is_explicitly_requested``.
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
                # Do not synthesize text labels or connector paths from the
                # wording of the objective.  A fixed grid of normalized points
                # is not evidence of canvas objects and caused Excalidraw runs
                # to report successful arrows/labels on an empty canvas.  Text
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


def navigation_control_for_transition(context: ProductContext, source_url: str, destination: str):
    source = _canonical_url(context.url, source_url)
    target = _canonical_url(context.url, destination)
    controls = [*context.navigation, *context.elements]
    # An observed same-origin anchor is a semantic navigation control even
    # when an older discovery snapshot did not populate its `actionable`
    # convenience flag. Its href plus source provenance is the stronger
    # evidence; otherwise a valid visible tab would be downgraded to a direct
    # route transition merely because of migration metadata.
    exact = next(
        (
            item
            for item in controls
            if item.href
            # Fragment links move within the already-open document.  They
            # are useful local evidence, but they are not a page transition
            # and must never satisfy a cross-page navigation lookup after URL
            # canonicalization strips the fragment.
            and not urlsplit(item.href).fragment
            and _canonical_url(item.source_url or context.url, item.source_url or context.url)
            == source
            and _canonical_url(item.source_url or context.url, item.href) == target
        ),
        None,
    )
    if exact is not None:
        return exact
    # Fresh page evidence is authoritative. If the active route exposes any
    # primary controls, or has a fully captured page with no primary controls,
    # do not reuse a shell link recorded on another route. Sparse legacy
    # in-memory contexts (without page evidence) retain the compatibility
    # fallback below.
    active_primary = [
        item
        for item in controls
        if item.href
        and _canonical_url(context.url, item.source_url or context.url) == source
        and (item in context.navigation or item.navigation_scope == "primary")
    ]
    active_page = next(
        (
            page
            for page in context.page_knowledge
            if _canonical_url(context.url, page.url) == source
        ),
        None,
    )
    if active_primary or (active_page is not None and active_page.evidence_refs):
        return None
    # Persistent global navigation is sometimes captured against a page-local
    # snapshot during migration. It is still only valid when its source
    # provenance matches the page being left; reusing a control observed on a
    # different route can dispatch a stale header after an app/workspace
    # transition and makes workflow validation report a false page state.
    persistent_controls = [
        *context.navigation,
        *(item for item in context.elements if item.navigation_scope == "primary"),
    ]
    return next(
        (
            item
            for item in persistent_controls
            if item.href
            and not urlsplit(item.href).fragment
            and (
                _canonical_url(context.url, item.source_url or context.url) == source
                or active_page is None
                or not active_page.evidence_refs
            )
            and _canonical_url(item.source_url or context.url, item.href) == target
        ),
        None,
    )


# Compatibility for the internal call sites retained while integrations move
# to the explicit transition-oriented name.
_navigation_control = navigation_control_for_transition


def _is_control_chrome(item: ObservedElement) -> bool:
    """Return whether a label describes page mechanics, not page content.

    Search, filter, import, and export affordances are valid interaction
    evidence. They are not, by themselves, a meaningful product chapter.
    Keeping this classification generic prevents a plan from mistaking a
    toolbar for an explanation of the records, content, or result below it.
    """
    # Do not use the broader evidence tokenizer here: its plural stemming
    # would turn ``previous`` into ``previou`` and make a plain pagination
    # control look like reader content.
    words = set(re.findall(r"[a-z0-9]{3,}", item.name.casefold()))
    chrome = {
        "search",
        "filter",
        "filters",
        "sort",
        "column",
        "columns",
        "refresh",
        "export",
        "import",
        "upload",
        "download",
        "bulk",
        "menu",
        "actions",
        "view",
        "views",
        "new",
        "add",
        "create",
        "close",
        "back",
        "next",
        "previous",
        "prev",
        "first",
        "last",
        "page",
        "pagination",
    }
    # Consent banners are transient mechanics rather than product content.
    # Treat their action labels as chrome only when the element is actionable
    # and every word belongs to the consent vocabulary; a real Privacy or
    # Cookie policy page therefore remains eligible as reader content.
    consent = {
        "cookie",
        "cookies",
        "consent",
        "accept",
        "reject",
        "necessary",
        "only",
        "close",
        "preferences",
        "non",
        "essential",
        "allow",
        "and",
    }
    if item.actionable and (
        (words & {"cookie", "cookies", "consent"} and words <= consent)
        or words <= {"necessary", "only"}
        or ("skip" in words and "content" in words)
    ):
        return True
    return bool(words) and words.issubset(chrome)


def _has_descriptive_landmark_evidence(page: PageKnowledge, item: ObservedElement) -> bool:
    """Whether a non-heading can honestly carry a reader-facing scene.

    A table header, search field, filter chip, or reminder button is useful
    execution evidence, but it is not automatically a story beat.  The former
    rule admitted any long-enough label as a supplemental landmark, which let
    dense application chrome turn into captions such as ``Enter phone number
    is in the current workflow``.  Require a local, target-headed descriptive
    fact before a non-heading earns a reading scene.  This is deliberately
    content-shape based rather than a product-specific allowlist.
    """
    name = " ".join(item.name.split()).casefold()
    if (
        not name
        or item.actionable
        or (item.tag or "").casefold() in {"input", "select", "textarea", "button"}
    ):
        return False
    for raw_fact in page.visible_facts:
        heading, separator, body = raw_fact.partition("::")
        if not separator or " ".join(heading.split()).casefold() != name:
            continue
        prose = " ".join(body.split())
        words = _tokens(prose)
        # A sentence or a sufficiently rich explanatory phrase is needed;
        # a copied inventory of controls and table columns is not enough.
        if (
            len(words) >= 8
            and ("." in prose or ";" in prose or len(words) >= 14)
            and "chevron" not in prose.casefold()
        ):
            return True
    return False


def _is_operational_workspace(page: PageKnowledge) -> bool:
    """Identify a dense records/form workspace from observed local evidence."""
    evidence = " ".join(page.visible_facts).casefold()
    has_creation = bool(re.search(r"\b(?:new|add|create)\b", evidence))
    has_records_surface = bool(
        re.search(r"\b(?:filter|search|table|status|columns?|results?|records?)\b", evidence)
    )
    return has_creation and has_records_surface


def _page_form_controls(context: ProductContext, page: PageKnowledge) -> list[ObservedElement]:
    """Return locally observed form controls suitable for read-only inspection.

    A compact records workspace can be fully visible without enough vertical
    distance for an honest scroll.  Its form schema is still meaningful local
    evidence, so the plan may inspect a field/choice without typing, opening,
    or submitting anything.  This gives the page an actual exploration beat
    instead of fabricating a zero-distance scroll to its heading.
    """
    canonical = _canonical_url(context.url, page.url)
    controls = [
        item
        for item in context.elements
        if _canonical_url(context.url, item.source_url or context.url) == canonical
        and (item.tag or "").casefold() in {"input", "select", "textarea"}
        and item.name.strip()
        and not _PLACEHOLDER_ELEMENT_PATTERN.fullmatch(item.name.strip())
    ]
    # Repeated responsive markup should not create repeated inspection beats.
    seen: set[str] = set()
    return [
        item
        for item in controls
        if not (
            " ".join(item.name.split()).casefold() in seen
            or seen.add(" ".join(item.name.split()).casefold())
        )
    ]


def _page_landmarks(context: ProductContext, page: PageKnowledge):
    canonical = _canonical_url(context.url, page.url)
    # Visual editors and design-system shells often expose starter copy such
    # as ``Heading``/``Title`` alongside lorem ipsum. Those labels describe a
    # template placeholder, not a user-facing chapter. Suppress only when the
    # same page evidence proves the placeholder pattern; a real product whose
    # section is genuinely named "Heading" remains eligible.
    page_evidence_text = " ".join(str(value) for value in page.visible_facts).casefold()
    placeholder_landmark = bool(
        "lorem ipsum" in page_evidence_text
        and any(token in page_evidence_text for token in ("heading", "title", "description"))
    )
    placeholder_labels = {"heading", "title", "description"}
    candidates = [
        item
        for item in context.elements
        if item.name.strip()
        and item.navigation_scope != "footer"
        and _is_safe_landmark(item)
        and _canonical_url(context.url, item.source_url or context.url) == canonical
        and not (
            placeholder_landmark
            and (
                " ".join(item.name.split()).casefold() in placeholder_labels
                or "lorem ipsum" in " ".join(item.name.split()).casefold()
                or any(
                    " ".join(item.name.split()).casefold().startswith(f"{label} ")
                    for label in placeholder_labels
                )
                # An editor palette/menu is commonly exposed as href-less
                # anchor/button controls. When the page itself is proven to
                # contain starter lorem copy, short controls are chrome or
                # template samples rather than reader-facing content. Keep
                # non-anchor workspace evidence available for the actual
                # editor surface.
                or (item.tag in {"a", "button"} and not item.href and len(_tokens(item.name)) <= 2)
            )
        )
    ]
    if not candidates and page.fingerprint.startswith("observed:"):
        candidates = [
            item
            for item in context.navigation
            if item.href and _canonical_url(item.source_url or context.url, item.href) == canonical
        ]
    # A reading landmark is page content, not another navigation affordance.
    # In particular, a short Contact page often exposes a footer full of links;
    # treating those links as evidence would turn the last chapter into an
    # accidental route sweep. Prefer headings, then other non-actionable local
    # content. Actionable controls are a last-resort fallback for pages which
    # genuinely have no readable landmarks.
    headings = [
        item
        for item in candidates
        if item.tag in {"h1", "h2", "h3", "h4"} or item.role == "heading"
    ]
    passive = [item for item in candidates if not item.actionable and item not in headings]
    observed_control_names = {
        " ".join(value.split()).casefold()
        for value in page.actionable_controls
        if value and not _PLACEHOLDER_ELEMENT_PATTERN.fullmatch(value.strip())
    }
    navigation_names = {
        " ".join(item.name.split()).casefold() for item in context.navigation if item.name
    }
    actionable = [
        item
        for item in candidates
        if item.actionable
        and not item.href
        and item.navigation_scope != "primary"
        # Global shell controls and accessibility placeholders are useful
        # discovery evidence, but they are not page-local story landmarks.
        # Excluding them here prevents a planner from trying to scroll to a
        # hidden "Marketing ›" button instead of showing the booking/list
        # content that is actually visible.
        and "chevron" not in item.name.casefold()
        and not _PLACEHOLDER_ELEMENT_PATTERN.fullmatch(item.name.strip())
        and item.name.strip() not in {"0", "1"}
        and not _is_table_or_record_artifact(item)
        and not _is_control_chrome(item)
        and " ".join(item.name.split()).casefold() not in navigation_names
        # When discovery has a page-local control inventory, do not merge
        # controls from another transient state (for example an opened modal)
        # merely because both snapshots share a canonical URL.
        and (
            not observed_control_names
            or " ".join(item.name.split()).casefold() in observed_control_names
        )
        # A page with no discovered section/landmark inventory is usually a
        # transient or merged application state. Its toolbar controls are not
        # a substitute for page-local editorial evidence; handle that state
        # with a page-level verified hold below instead of inventing a target.
        and bool(page.scroll_landmarks or page.visible_sections)
    ]
    reading_candidates = headings or passive or actionable
    if headings and len(headings) < 2:
        # Data-heavy application pages frequently expose one section heading
        # followed by filters, rows, or result controls rather than nested
        # headings. Keep a few descriptive, page-local witnesses so the
        # production plan demonstrates the actual experience instead of a
        # title-only chapter. The controls are only scroll/read targets here;
        # execution still requires an explicit semantic action to dispatch.
        supplemental = [
            item
            for item in [*passive, *actionable]
            if len(" ".join((item.text or item.name).split())) >= 6
            and " ".join(item.name.split()).casefold()
            not in {"menu", "close", "back", "home", "settings"}
            and "chevron" not in item.name.casefold()
            and not item.name.casefold().startswith("element-")
            and " ".join(item.name.split()).casefold()
            not in {" ".join(nav.name.split()).casefold() for nav in context.navigation}
            and not _is_control_chrome(item)
            and _has_descriptive_landmark_evidence(page, item)
        ]
        reading_candidates = [*headings, *supplemental[:5]]
    # A feature-specific configuration card is supporting context, not a
    # product-specific route. Promote it only when the objective asks for
    # configuration context and its observed label shares a requested feature
    # token. This keeps generic Settings shells out of focused tours.
    configuration = _configuration_landmark(context, page)
    if configuration is not None and configuration not in reading_candidates:
        reading_candidates.append(configuration)
    wanted = {
        " ".join(value.split()).lower()
        for value in [*page.scroll_landmarks, *page.visible_sections]
    }
    reading_candidates.sort(
        key=lambda item: (
            0 if " ".join(item.name.split()).lower() in wanted else 1,
            0 if item.tag in {"h1", "h2", "h3", "h4"} else 1,
        )
    )
    unique, names = [], set()
    for item in reading_candidates:
        name = " ".join(item.name.split()).lower()
        if name and name not in names and len(name) <= 180:
            names.add(name)
            unique.append(item)
    if placeholder_landmark:
        # Once starter lorem evidence is present, href-less palette/menu
        # controls and search/color inputs are implementation chrome. They
        # are still available to the interaction kernel, but must not become
        # reader-facing scenes. Preserve any non-form workspace surface; if
        # none remains the page compiler will emit a verified page-level hold.
        unique = [
            item
            for item in unique
            if not (
                item.actionable and (item.tag in {"a", "button", "input", "select", "textarea"})
            )
        ]
    # A featured-work collection belongs to the opening product story. On a
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


def _page_story_subject(page: PageKnowledge) -> str:
    """Return a reader-facing page subject, avoiding proven template filler."""
    subject = " ".join((page.purpose or page.title or "page content").split())
    evidence = " ".join(str(value) for value in page.visible_facts).casefold()
    if "lorem ipsum" in evidence and (
        subject.casefold() in {"heading", "title", "description"}
        or "lorem ipsum" in subject.casefold()
    ):
        # The document title is a better neutral description of a blank editor
        # than its starter heading. It is evidence-backed and does not encode
        # a site-specific route or workflow.
        return " ".join((page.title or "workspace").split())
    return subject or "page content"


def _configuration_landmark(context: ProductContext, page: PageKnowledge):
    """Find an observed feature-configuration control for page-local context."""
    objective = getattr(context, "objective", None)
    if objective is None:
        return None
    raw = " ".join(getattr(objective, "requested_features", []) or []) or str(
        getattr(objective, "raw", "")
    )
    requested = _tokens(raw) - {
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
        "settings",
        "setting",
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
    page_words = _tokens(" ".join([page.title, page.purpose, *page.visible_sections]))
    if not requested or not page_words & {"settings", "setting", "config", "configuration"}:
        return None
    canonical = _canonical_url(context.url, page.url)
    return next(
        (
            item
            for item in context.elements
            if item.name.strip()
            and item.actionable
            and "config" in item.name.casefold()
            and _tokens(item.name) & requested
            and _canonical_url(context.url, item.source_url or context.url) == canonical
        ),
        None,
    )


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
        scored.append(
            (
                (
                    1 if bare_label else 0,
                    0 if heading.startswith(landmark.lower()) else 1,
                    -overlap,
                    abs(fact_index - index),
                ),
                fact,
            )
        )
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
    # Intent is an audit explanation, not an accessibility-tree dump.  The
    # full fact remains in immutable evidence_refs; retaining it here used to
    # leak hundreds of unrelated shell labels into every plan and made a weak
    # editorial replacement appear grounded.
    fact = " ".join(fact.split())
    if "::" in fact:
        fact = fact.split("::", 1)[-1].strip()
    if "chevron" in fact.casefold() or len(fact.split()) > 28:
        fact = ""
    suffix = f": {fact}" if fact else ""
    return f"{verbs[phase]} {landmark}{suffix}"[:240]


def validate_flow_scope(
    proposal: WorkflowProposal, *, context: ProductContext, candidate: CandidateDemoFlow | None
) -> list[str]:
    """Return scope/state failures before a browser is allowed to execute.

    Coverage alone is insufficient: the former validator accepted an action
    from any page that had appeared earlier in the journey. That permits an
    invalid sequence such as ``Leads -> Settings -> open New Lead`` without a
    visible transition back to Leads. Keep an explicit current page so each
    target is proven to exist in the page state where it will be dispatched.
    """
    if candidate is None:
        return []
    allowed = {_canonical_url(context.url, url) for url in candidate.page_urls}
    allowed.add(_canonical_url(context.url, context.url))
    failures: list[str] = []
    observed_destinations: set[str] = set()
    active_page = _canonical_url(context.url, context.url)

    def destination_for(operation: SemanticOperation) -> str | None:
        value = next(
            (
                str(condition.expected)
                for condition in operation.postconditions
                if condition.kind == "url"
            ),
            str(operation.value or ""),
        )
        if not value:
            return None
        if value.startswith("**/"):
            suffix = value.removeprefix("**")
            return next((url for url in allowed if url.endswith(suffix)), None)
        return _canonical_url(context.url, value)

    def is_persistent_primary_target(operation: SemanticOperation, active_page_url: str) -> bool:
        """Allow a global control only when it was observed on this page.

        Discovery may see the same header on several routes, but a control
        captured on the opening page is not proof that it is currently
        interactable after a transition (embedded apps often replace the
        header entirely).  Requiring page-local provenance prevents a
        complete tour from dispatching a root-page action inside an app route.
        """
        if operation.target is None:
            return False
        if not any(page.evidence_refs for page in context.page_knowledge):
            # Legacy/in-memory plans have no page-local evidence from which to
            # distinguish a persistent header from a route-local control.
            # Preserve their established navigation semantics; fresh
            # discovery is subject to the stricter branch below.
            return any(
                (item in context.navigation or item.navigation_scope == "primary")
                and item.selector == operation.target.selector
                and item.name.casefold() == operation.target.name.casefold()
                for item in [*context.navigation, *context.elements]
            )
        active = _canonical_url(context.url, active_page_url)
        controls = [*context.navigation, *context.elements]
        active_controls = [
            item
            for item in controls
            if _canonical_url(context.url, item.source_url or context.url) == active
            and (item in context.navigation or item.navigation_scope == "primary")
        ]
        active_page = next(
            (
                page
                for page in context.page_knowledge
                if _canonical_url(context.url, page.url) == active
            ),
            None,
        )
        # If discovery did not capture any controls for the active page (a
        # common legacy snapshot shape), a persistent primary header remains a
        # valid compatibility witness.  Once page-local controls are present,
        # however, they are the authoritative state and a prior-page target is
        # not assumed to survive an app/workspace transition.
        if active_controls:
            eligible = active_controls
        elif active_page is not None and active_page.evidence_refs:
            # A fully captured page with no local controls is an explicit
            # proof that the prior-page control is not available here. Do not
            # silently treat it as a persistent header.
            eligible = []
        else:
            # Sparse legacy snapshots do not carry page records/controls for
            # every route. Preserve their compatibility with a persistent
            # primary header until fresh discovery supplies page-local proof.
            eligible = [
                item
                for item in controls
                if item in context.navigation or item.navigation_scope == "primary"
            ]
        return any(
            item.selector == operation.target.selector
            and item.name.casefold() == operation.target.name.casefold()
            for item in eligible
        )

    for operation in proposal.steps:
        if operation.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}:
            destination = destination_for(operation)
            if destination is None:
                continue
            if destination not in allowed:
                failures.append(f"CANDIDATE_FLOW_SCOPE_VIOLATION:{destination}")
                continue
            if operation.target and operation.target.source_url:
                source = _canonical_url(context.url, operation.target.source_url)
                if source != active_page and not is_persistent_primary_target(
                    operation, active_page
                ):
                    failures.append(
                        "WORKFLOW_PAGE_STATE_DISCONTINUITY:"
                        f"{operation.target.name}:{source}!={active_page}"
                    )
            # Direct URL navigation is a declared fallback. When a semantic,
            # visible control from the current page was discovered, using the
            # URL instead is a workflow validation failure, not a renderer
            # preference.
            if (
                operation.kind is OperationKind.NAVIGATE
                and destination != active_page
                and _navigation_control(context, active_page, destination) is not None
                and "fallback:direct-navigation-no-visible-control" not in operation.evidence_refs
            ):
                failures.append(f"WORKFLOW_DIRECT_NAVIGATION_WHEN_VISIBLE_CONTROL:{destination}")
            observed_destinations.add(destination)
            active_page = destination
            continue

        if operation.target and operation.target.source_url:
            source = _canonical_url(context.url, operation.target.source_url)
            if source != active_page:
                failures.append(
                    "WORKFLOW_PAGE_STATE_DISCONTINUITY:"
                    f"{operation.target.name}:{source}!={active_page}"
                )
        if operation.page_url and _canonical_url(context.url, operation.page_url) != active_page:
            failures.append(
                f"WORKFLOW_PAGE_STATE_DISCONTINUITY:operation:{operation.page_url}!={active_page}"
            )
    if not failures and context.page_knowledge:
        for required in candidate.page_urls[1:]:
            canonical = _canonical_url(context.url, required)
            if canonical not in observed_destinations:
                failures.append(f"CANDIDATE_FLOW_MISSING_SELECTED_PAGE:{canonical}")
    return failures
