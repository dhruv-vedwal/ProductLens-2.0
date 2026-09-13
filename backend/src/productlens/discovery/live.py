"""Bounded, DOM-first product discovery for supported web applications."""

from __future__ import annotations

import asyncio
import hashlib
import re
from contextlib import suppress
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from productlens.contracts.models import (
    ActionCapability,
    CandidateDemoFlow,
    DiscoveryBudget,
    FeatureKnowledge,
    FormField,
    FormSchema,
    ObjectiveRelationship,
    ObjectiveSpec,
    ObservedElement,
    PageKnowledge,
    ProductRelationship,
    ProductContext,
    Target,
)


def _tokens(value: str) -> set[str]:
    return {item for item in re.findall(r"[a-z0-9]{3,}", value.lower())}


def _canonical_route(value: str) -> str:
    """Compare application routes without treating cosmetic URL variants as pages.

    Discovery must not spend its limited page budget on both ``/`` and an
    equivalent trailing-slash/query variant.  Fragments are presentation state,
    not independent pages for the purpose of product knowledge.
    """
    parsed = urlparse(value)
    # HTTP-to-HTTPS redirects are transport normalization, not distinct pages.
    scheme = "https" if parsed.scheme in {"http", "https"} else parsed.scheme
    path = parsed.path.rstrip("/") or "/"
    return parsed._replace(
        scheme=scheme, netloc=parsed.netloc.lower(), path=path, params="", query="", fragment=""
    ).geturl()


def adaptive_exploration_budget(
    budget: DiscoveryBudget, objective: ObjectiveSpec, *, primary_route_count: int
) -> DiscoveryBudget:
    """Expand bounded discovery only when a full tour proves it needs more pages."""
    if objective.demo_type != "full_walkthrough":
        return budget
    required_pages = max(1, primary_route_count + 1)  # opening page plus primary controls
    expanded_pages = min(12, max(budget.max_pages, required_pages))
    if expanded_pages == budget.max_pages:
        return budget
    return budget.model_copy(update={
        "max_pages": expanded_pages,
        "max_actions": min(72, max(budget.max_actions, expanded_pages * 4)),
        # Full walkthroughs are explicitly allowed to inspect more than the
        # narrow default.  Keep the cap bounded below the Browserbase lease,
        # but do not force rich documentation/canvas products to time out at
        # the same five-minute ceiling used for a focused feature scan.
        "max_time_seconds": min(900, max(budget.max_time_seconds, expanded_pages * 45)),
    })


def _classify(title: str, text: str, elements: list[ObservedElement]) -> str:
    if any(item.tag == "form" for item in elements):
        return "form_application"
    return "web_application"


def _route_score(item: ObservedElement, objective_words: set[str]) -> int:
    candidate_words = _tokens(f"{item.name} {item.text or ''} {item.href or ''}")
    score = len(objective_words & candidate_words)
    # The synonyms are only ranking assistance; the actual route remains DOM evidence.
    if {"invite", "teammate", "member", "team"} & objective_words and {
        "user",
        "users",
    } & candidate_words:
        score += 3
    if {"report", "export", "csv"} & objective_words and "report" in candidate_words:
        score += 3
    return score


def _objective_spec(objective: str) -> ObjectiveSpec:
    lower = objective.lower()
    # Full-tour intent is often expressed without the literal phrase
    # ``full walkthrough`` (for example, "complete ... walkthrough of every
    # safe primary section").  Discovery uses this classification to order
    # its page budget, so under-classifying it silently replaces primary-page
    # coverage with objective-ranked detail routes.  Keep the signal generic
    # and objective-derived; no product labels belong here.
    full = bool(
        # Natural requests commonly place punctuation and duration words
        # between the completeness signal and ``walkthrough`` (for example,
        # "complete, evidence-backed 2 to 3 minute walkthrough").  Treat
        # those separators as prose, not as a reason to downgrade the entire
        # production envelope to a short feature demo.
        re.search(r"\bfull\b(?:[\s,;:\-]+[\w-]+){0,6}[\s,;:\-]+walkthrough\b", lower)
        or re.search(r"\bcomplete\b(?:[\s,;:\-]+[\w-]+){0,10}[\s,;:\-]+walkthrough\b", lower)
        or re.search(r"\b(?:every|all)\s+(?:safe\s+)?(?:primary\s+)?(?:section|page|tab)s?\b", lower)
        or "each tab" in lower
        or "entire" in lower
        or "whole product" in lower
    )
    if re.search(r"\b(?:sales|selling|prospect|buyer)\b", lower):
        video_type = "sales_demo"
    elif re.search(r"\b(?:onboard|onboarding|getting started)\b", lower):
        video_type = "onboarding"
    elif re.search(r"\b(?:train|training|tutorial|teach)\b", lower):
        video_type = "training"
    elif re.search(r"\b(?:changelog|release notes?|what(?:'s| is) new|new release)\b", lower):
        video_type = "changelog"
    elif re.search(r"\b(?:support|troubleshoot|troubleshooting|debug)\b", lower):
        video_type = "support"
    elif re.search(r"\b(?:portfolio|recruiter|career)\b", lower):
        video_type = "portfolio"
    elif full:
        video_type = "full_tour"
    else:
        video_type = "feature_walkthrough"
    # Keep product concepts separate from the prose used to ask for a demo.
    # Feeding duration, connective, or orchestration words into relevance
    # scoring makes unrelated pages look useful simply because they mention
    # "context" or "create". The raw request remains persisted unchanged;
    # this normalized list is only the semantic feature vocabulary.
    objective_noise = {
        "the", "and", "for", "with", "from", "into", "this", "that", "its", "show", "give",
        "explain", "demonstrate", "demo", "walkthrough", "tour", "full", "complete",
        "brief", "detailed", "concise", "overview", "evidence", "grounded", "feature", "flow", "workflow",
        "application", "product", "create", "produce", "record", "minute", "minutes",
        "second", "seconds", "context", "during", "then", "establish", "relationship",
        "exploration", "explore", "safe", "isolated", "actual", "experience", "only", "thorough",
        "production", "workspace", "visible", "state", "result", "close", "viewer",
        "screen", "page", "pages", "content", "current", "requested", "relevant",
        "discovery", "list", "visibly", "verify", "one", "not", "visit", "unless",
        "every", "each", "all", "primary", "section", "sections", "tab", "tabs", "meaningful",
        "prospect", "user", "users", "business", "website", "web",
        "sales", "onboarding", "training", "changelog", "support", "portfolio", "video",
    }
    words = [
        word for word in re.findall(r"[a-z0-9]{3,}", lower)
        if word not in objective_noise
    ]
    requested = list(dict.fromkeys(words))[:12]
    # Preserve the feature phrase rather than promoting the request's first
    # verb (``Create a two minute walkthrough of invoices`` used to make
    # ``create`` the required feature). This pattern is objective language,
    # not a product taxonomy, and works for any named module or workflow.
    walkthrough_match = re.search(
        r"\b(?:create|show|give|produce|record|explain|demonstrate)?\s*(?:an?|the)?\s*"
        r"(?:full|complete|brief|detailed|polished|presentable|silent|caption[- ]led|human[- ]like|short|evidence[- ]grounded)?\s*"
        r"(?:\d+(?:\s*(?:to|-)?\s*\d+)?\s*(?:minute|min|second|sec)s?\s*)?"
        r"(?:walkthrough|demo|tour)\s+of\s+(?P<entity>[a-z][a-z0-9 /&_\-]{1,120}?)"
        r"(?=\s+(?:in the context of|configured by|using|with context from|for|including|to)\b|[:,.;]|$)",
        lower,
    )
    walkthrough_entity = " ".join(walkthrough_match.group("entity").split()) if walkthrough_match else None
    if walkthrough_entity:
        # Keep the requested noun phrase, but remove narrator framing that
        # providers and users commonly place around it.  Without this
        # normalization, phrases such as "the authenticated booking
        # workflow" become the required entity and fail grounding against a
        # page whose observed identity is simply "Bookings".  Domain terms
        # (including a meaningful suffix such as "management" or
        # "workflow") are retained, so existing invoice-workflow semantics
        # remain unchanged.
        walkthrough_entity = re.sub(
            r"^(?:the|a|an)\s+", "", walkthrough_entity
        )
        walkthrough_entity = re.sub(
            r"^(?:authenticated|operational|relevant|requested|actual|visible|current|primary|polished|presentable|silent|caption[- ]led|human[- ]like|short|brief|detailed)\s+",
            "", walkthrough_entity,
        )
        walkthrough_entity = " ".join(walkthrough_entity.split()) or None
        # A focused objective may use a generic subject ("the observed public
        # product experience") rather than naming a feature.  Do not promote
        # that editorial framing into a required entity that can never be
        # grounded by page evidence.
        if walkthrough_entity:
            entity_words = set(re.findall(r"[a-z0-9]{3,}", walkthrough_entity))
            generic_entity_words = objective_noise | {
                "focused", "observed", "public", "meaningful", "safe",
                "information", "browsing", "resource", "resources", "detail",
                "most", "workflow", "experience",
            }
            if entity_words and entity_words <= generic_entity_words:
                walkthrough_entity = None
    # A completeness clause such as "walkthrough of every safe primary
    # section" describes breadth, not a product entity. Treating it as a
    # must-show feature makes full-tour planning fail before discovery can
    # inspect the actual navigation inventory.
    if full and walkthrough_entity and re.match(
        r"^(?:every|each|all|the whole|the entire)\b", walkthrough_entity
    ):
        walkthrough_entity = None
    # An objective may explicitly require that a setup/configuration area
    # explains an operational feature. Keep that relationship as data rather
    # than making a product-specific route rule.
    relationships: list[ObjectiveRelationship] = []
    for match in re.finditer(
        r"(?P<source>[a-z][a-z0-9 ]{2,80}?)\s*(?:→|->|configures|explains|supports)\s*(?P<target>[a-z][a-z0-9 ]{2,80})(?:[,.;]|$)",
        lower,
    ):
        source, target = (" ".join(match.group(name).split()) for name in ("source", "target"))
        source = re.sub(r"^(?:show|explain|demonstrate)\s+", "", source)
        if source != target:
            relationships.append(ObjectiveRelationship(source=source, target=target, relation="context_for"))
    contextual = re.search(
        r"(?P<target>[a-z][a-z0-9 ]{2,80}?)\s+(?:in the context of|configured by|using|with context from)\s+(?P<source>[a-z][a-z0-9 ]{2,80})(?:[,.;]|$)",
        lower,
    )
    if contextual:
        source, target = (" ".join(contextual.group(name).split()) for name in ("source", "target"))
        # The left side can include the narrator's wording. When a dedicated
        # walkthrough subject was parsed above, it is the canonical feature
        # entity for this relationship.
        target = walkthrough_entity or target
        if source != target:
            relationships.append(ObjectiveRelationship(source=source, target=target, relation="context_for"))
    # Requests often describe the dependency in prose instead of using an
    # arrow or the phrase "in the context of": "inspect the Booking Config
    # to explain how bookings are configured".  Preserve that relationship
    # as structured intent so the planner can include the observed setup
    # control/page without relying on a product-specific route name.
    configured_by = re.search(
        r"(?P<source>\b[a-z][a-z0-9 -]{0,60}?\b(?:config|configuration|settings?)\b)"
        r".{0,140}?\b(?:how|way)\s+(?P<target>[a-z][a-z0-9 -]{1,60}?)\s+"
        r"(?:is|are)\s+configured\b",
        lower,
    )
    if configured_by:
        source = " ".join(configured_by.group("source").split())
        target = " ".join(configured_by.group("target").split())
        source = re.sub(
            r"^(?:(?:explore|inspect|understand|show|use|the|a|an|relevant)\s+)+",
            "", source,
        )
        # The bounded prose match can begin at an earlier verb or feature
        # phrase before the actual setup label. Keep the final, human-named
        # configuration term (plus its short noun qualifier) as the source.
        source_match = re.search(
            r"(?:[a-z0-9-]+\s+){0,4}(?:config|configuration|settings?)$",
            source,
        )
        if source_match:
            source = source_match.group(0).strip()
        source = re.sub(r"^(?:(?:the\s+)?context\s+of|in\s+the\s+context\s+of)\s+", "", source)
        target = re.sub(r"^(?:the|a|an|relevant)\s+", "", target)
        target_words = {word.rstrip("s") for word in _tokens(target)}
        duplicate_target = any(
            {word.rstrip("s") for word in _tokens(item.target)} & target_words
            for item in relationships
            if item.source == source
        )
        if source and target and source != target and not duplicate_target:
            relationships.append(ObjectiveRelationship(source=source, target=target, relation="context_for"))
    relationships = list({(item.source, item.target, item.relation): item for item in relationships}.values())
    generic = {
        *objective_noise, "config", "configuration", "settings", "setting",
        "every", "each", "all", "safe", "primary", "section", "sections",
        "meaningful", "visible", "content", "tab", "tabs",
        # Scope/presentation words are not a requested product entity.  Keep
        # them out of primary_entity so focused objectives remain grounded in
        # observed page evidence rather than their editorial wording.
        "focused", "feature", "observed", "public", "product", "experience",
        "demo", "demonstration", "evidence", "grounded", "safe", "workflow",
        "most", "information", "browsing", "resource", "resources", "detail",
    }
    primary_entity = walkthrough_entity or next((word for word in requested if word not in generic), None)
    if primary_entity is None and relationships:
        primary_entity = re.sub(r"\s+(?:workflow|flow)$", "", relationships[0].target).strip() or None
    return ObjectiveSpec(
        video_type=video_type,
        raw=objective,
        demo_type="full_walkthrough" if full else "feature_walkthrough",
        depth="thorough" if full else "standard",
        requested_features=requested,
        primary_entity=primary_entity,
        supporting_relationships=relationships,
        # A must-show requirement has to be enforceable. The old token list
        # made every adjective and duration word mandatory, so planners could
        # only ignore it. Use the requested primary entity unless the request
        # is a whole-product tour, whose primary-page contract owns coverage.
        must_show=[] if full or primary_entity is None else [primary_entity],
        success_criteria=["requested content is visibly established", "each selected page is explored before transition"],
        minimum_duration_seconds=110 if full else 60,
        target_duration_seconds=180 if full else 120,
        # A complete walkthrough targets three minutes with a bounded repair
        # envelope. Discovery must not accept an old route-sweep artifact that
        # merely accumulated remote idle time; an overlong capture is returned
        # to planning for selective, evidence-backed coverage.
        maximum_duration_seconds=240 if full else 180,
    )


def _normalized_terms(value: str) -> set[str]:
    """Compare observed UI phrases without a product-specific vocabulary."""
    terms = {token.rstrip("s") for token in _tokens(value) if token}
    # Routes and product UIs commonly abbreviate configuration as ``config``.
    # Treat it as a vocabulary normalization, not a product-specific synonym,
    # so an inspected ``/settings/order-config`` route can ground an
    # ``Order Configuration`` objective.
    if "config" in terms:
        terms.add("configuration")
    if "configuration" in terms:
        terms.add("config")
    return terms


def _route_objective_score(
    route: str,
    navigation: list[ObservedElement],
    objective: ObjectiveSpec,
) -> int:
    """Score a discovered route from objective relationships and visible UI.

    Configuration is commonly exposed through an observed ``Settings`` entry
    and then a named nested item. Treating settings/configuration as a generic
    relationship synonym lets a requested configuration context be discovered
    without embedding a product, module, or route name in the engine.
    """
    canonical = _canonical_route(route)
    label = next(
        (
            item.name
            for item in navigation
            if item.href and _canonical_route(urljoin(item.source_url or route, item.href)) == canonical
        ),
        "",
    )
    candidate = _normalized_terms(f"{label} {urlparse(route).path}")
    requested = _normalized_terms(" ".join([objective.raw, objective.primary_entity or ""]))
    score = len(candidate & requested)
    # A focused demo has to establish its operational surface before visiting
    # supporting configuration. Give the named entity a substantial, generic
    # preference and use shallow same-origin paths as a tie-breaker: a primary
    # top-level list/detail view is normally a better entry point than a deep
    # template or integration page that merely shares one noun. This is only a
    # discovery rank; later page evidence still decides whether the route is
    # worth keeping in the candidate workflow.
    primary = _normalized_terms(objective.primary_entity or "") - {
        "management", "module", "workflow", "flow", "experience",
    }
    direct_primary = candidate & primary
    if direct_primary and label:
        depth = len([part for part in urlparse(route).path.split("/") if part])
        score += len(direct_primary) * 15 + max(0, 4 - depth)
        # A navigation label can share the requested entity while naming a
        # different supporting artifact (for example templates, imports, or
        # integrations). Prefer the concise operational label first; a
        # template route may still be selected later if page evidence proves
        # it is needed for the story.
        label_terms = _normalized_terms(label)
        label_extras = {
            term for term in label_terms - primary
            if not re.fullmatch(r"v\d+", term)
        }
        score -= min(10, len(label_extras) * 8)
    for relationship in objective.supporting_relationships:
        source = _normalized_terms(relationship.source)
        target = _normalized_terms(relationship.target)
        # A shared entity noun is not enough to call a sibling route the
        # requested context (``invoice templates`` is not ``invoice
        # configuration``). Grant relationship weight only when the complete
        # observed relationship side is present; ordinary entity overlap is
        # already handled by the operational ranking above.
        if source and source.issubset(candidate):
            score += len(source) * 8
        elif target and target.issubset(candidate):
            score += len(target) * 3
        if source & {"config", "configuration"} and candidate & {"setting", "settings", "config", "configuration"}:
            # An explicit configuration relationship means this generic entry
            # is a required context bridge, not merely another route label.
            # It must outrank similarly named but unrelated surfaces such as
            # "Invoice templates", whose only relevance is a shared entity
            # noun. The named child is still required and validated after the
            # Settings surface is reached.
            score += 12
    return score


def _relationship_supporting_routes(
    context: ProductContext, objective: ObjectiveSpec
) -> list[str]:
    """Return visible child routes that ground an explicit context request."""
    if not objective.supporting_relationships:
        return []
    origin = urlparse(context.url)
    candidates: list[tuple[int, str]] = []
    for item in context.navigation:
        if not item.href:
            continue
        route = urljoin(context.url, item.href)
        parsed = urlparse(route)
        if parsed.scheme not in {"http", "https", "file"} or parsed.netloc not in {"", origin.netloc}:
            continue
        candidate_terms = _normalized_terms(f"{item.name} {parsed.path}")
        direct_relationship_terms = set().union(*(
            _normalized_terms(relationship.source) | _normalized_terms(relationship.target)
            for relationship in objective.supporting_relationships
        ))
        score = _route_objective_score(route, context.navigation, objective)
        # The generic Settings/configuration bridge is an entry-point signal.
        # Once inside that entry surface, only an observed named relation
        # (rather than sibling Profile/Account pages) warrants deeper probing.
        if score and candidate_terms & direct_relationship_terms:
            candidates.append((score, route))
    return [route for _score, route in sorted(candidates, key=lambda item: -item[0])]


def _relationship_child_controls(
    context: ProductContext, objective: ObjectiveSpec
) -> list[ObservedElement]:
    """Select bounded, read-only visible controls for requested context states."""
    relationship_terms = set().union(*(
        _normalized_terms(relationship.source) | _normalized_terms(relationship.target)
        for relationship in objective.supporting_relationships
    )) if objective.supporting_relationships else set()
    controls: list[tuple[int, ObservedElement]] = []
    for item in context.elements:
        if not item.actionable or item.href or item.tag not in {"button", "input"}:
            continue
        terms = _normalized_terms(f"{item.name} {item.text or ''}")
        if not terms or terms & _UNSAFE_ACTION_WORDS or not (terms & relationship_terms):
            continue
        controls.append((len(terms & relationship_terms), item))
    return [item for _score, item in sorted(controls, key=lambda candidate: -candidate[0])]


def _focused_relationship_evidence_complete(
    contexts: list[ProductContext], objective: ObjectiveSpec
) -> bool:
    """Stop a narrow context exploration once its requested relation is proven.

    A Settings landing page can contain every configuration label in a menu.
    That is an entry point, not evidence that the named configuration was
    inspected. Require source terms both in page-local evidence and in that
    page's canonical location; this distinguishes the selected detail surface
    from a broad settings directory without naming any product route.
    """
    if not objective.supporting_relationships:
        return False

    def page_terms(context: ProductContext) -> set[str]:
        return _normalized_terms(" ".join([
            context.title, context.visible_text, *context.content_blocks,
        ]))

    combined = set().union(*(page_terms(context) for context in contexts))
    primary = _normalized_terms(objective.primary_entity or "")
    if primary and not primary.issubset(combined):
        return False
    for relationship in objective.supporting_relationships:
        source = _normalized_terms(relationship.source)
        target = _normalized_terms(relationship.target)
        if target and not target.issubset(combined):
            return False
        if source and not any(
            source.issubset(page_terms(context))
            and bool(source & _normalized_terms(urlparse(context.url).path))
            for context in contexts
        ):
            return False
    return True


def _relationship_page_roles(
    pages: list[PageKnowledge], objective: ObjectiveSpec
) -> tuple[list[PageKnowledge], list[PageKnowledge]]:
    """Split operational story surfaces from exploration-only context.

    A configuration page is important evidence when an objective says that it
    contextualises a workflow.  It is not, however, automatically a chapter
    in the production demo.  Keeping the distinction here means the persisted
    discovery artifact is already honest about what production should show,
    rather than relying on a later planner to repair a route tour.
    """
    if not objective.supporting_relationships:
        return pages, []

    def terms(page: PageKnowledge) -> set[str]:
        return _normalized_terms(" ".join([
            page.title, page.purpose, urlparse(page.url).path,
            *page.visible_sections, *page.visible_facts[:12],
        ]))

    source_urls: set[str] = set()
    target_urls: set[str] = set()
    for relationship in objective.supporting_relationships:
        source = _normalized_terms(relationship.source)
        target = _normalized_terms(relationship.target)
        for page in pages:
            page_terms = terms(page)
            # A Settings directory can list the source label without exposing
            # its content. Require a source term in the canonical route, or
            # an explicitly probed in-page relationship state, so only the
            # inspected detail surface becomes relationship evidence.
            route_terms = _normalized_terms(urlparse(page.url).path)
            semantic_probe = any(
                reference.startswith("visible_relationship_control:")
                and bool(source & _normalized_terms(reference))
                for reference in page.evidence_refs
            )
            if source and source.issubset(page_terms) and (bool(source & route_terms) or semantic_probe):
                source_urls.add(_canonical_route(page.url))
            if (
                target
                and target.issubset(page_terms)
                and bool(target & route_terms)
                and _canonical_route(page.url) not in source_urls
            ):
                target_urls.add(_canonical_route(page.url))

    # The requested primary feature is the operational surface even when the
    # target phrase is abbreviated in the relationship. Prefer it over a
    # broad Settings shell which merely lists the configuration link.
    primary_terms = _normalized_terms(objective.primary_entity or "")
    primary_candidates: list[tuple[int, int, PageKnowledge]] = []
    for index, page in enumerate(pages):
        # An explicitly proven configuration/detail page may document the
        # operational feature it governs. Its source role takes precedence so
        # that explanatory documentation does not become a production stop.
        if primary_terms and _canonical_route(page.url) not in source_urls:
            page_terms = terms(page)
            route_terms = _normalized_terms(urlparse(page.url).path)
            if primary_terms.issubset(page_terms):
                primary_candidates.append((
                    len(primary_terms & page_terms) + 2 * len(primary_terms & route_terms),
                    -index,
                    page,
                ))
    if primary_candidates:
        _score, _order, selected = max(primary_candidates, key=lambda item: (item[0], item[1]))
        target_urls.add(_canonical_route(selected.url))
    elif primary_terms:
        # UI headings routinely shorten an objective ("Orders" for "Order
        # Management"). Choose the best *observed operational* surface by
        # page and route evidence rather than demoting the request into a
        # Settings/configuration tour because an exact phrase was absent.
        candidates: list[tuple[int, int, PageKnowledge]] = []
        for index, page in enumerate(pages):
            canonical = _canonical_route(page.url)
            if canonical in source_urls:
                continue
            page_terms = terms(page)
            route_terms = _normalized_terms(urlparse(page.url).path)
            score = len(primary_terms & page_terms) + 2 * len(primary_terms & route_terms)
            if score:
                candidates.append((score, -index, page))
        if candidates:
            _score, _order, selected = max(candidates, key=lambda item: (item[0], item[1]))
            target_urls.add(_canonical_route(selected.url))

    supporting = [page for page in pages if _canonical_route(page.url) in source_urls]
    operational = [page for page in pages if _canonical_route(page.url) in target_urls]
    # A page can mention both terms. It remains operational when it is the
    # primary requested feature; otherwise retain it as context-only so a
    # config detail does not displace the actual workflow.
    if primary_terms:
        supporting = [
            page for page in supporting
            if _canonical_route(page.url) not in {_canonical_route(item.url) for item in operational}
            or not primary_terms.issubset(terms(page))
        ]
    return operational, supporting


def _derive_product_relationships(
    pages: list[PageKnowledge], objective: ObjectiveSpec
) -> list[ProductRelationship]:
    """Compile an evidence-backed relationship graph from observed pages.

    This intentionally starts with relationships requested by the user.  The
    endpoints are matched against page-local headings, prose, and canonical
    route terms; a lexical match alone never becomes an executable action.
    Unknown endpoints remain represented with low confidence so the planner
    can report the missing evidence instead of inventing a route.
    """
    if not objective.supporting_relationships:
        return []

    def page_terms(page: PageKnowledge) -> set[str]:
        return _normalized_terms(" ".join([
            page.title, page.purpose, urlparse(page.url).path,
            *page.visible_sections, *page.visible_facts[:16],
        ]))

    result: list[ProductRelationship] = []
    for requested in objective.supporting_relationships:
        source_terms = _normalized_terms(requested.source)
        target_terms = _normalized_terms(requested.target)
        source_candidates: list[tuple[int, PageKnowledge]] = []
        target_candidates: list[tuple[int, PageKnowledge]] = []
        for page in pages:
            terms = page_terms(page)
            route_terms = _normalized_terms(urlparse(page.url).path)
            source_score = len(source_terms & terms) + 2 * len(source_terms & route_terms)
            target_score = len(target_terms & terms) + 2 * len(target_terms & route_terms)
            if source_score:
                source_candidates.append((source_score, page))
            if target_score:
                target_candidates.append((target_score, page))
        source_page = max(source_candidates, key=lambda item: item[0])[1] if source_candidates else None
        target_page = next(
            (page for _score, page in sorted(target_candidates, key=lambda item: -item[0])
             if not source_page or _canonical_route(page.url) != _canonical_route(source_page.url)),
            None,
        )
        evidence = [f"objective_relationship:{requested.source}->{requested.target}"]
        if source_page:
            evidence.append(f"page:{source_page.url}")
        if target_page:
            evidence.append(f"page:{target_page.url}")
        confidence = 0.35 + (0.3 if source_page else 0) + (0.3 if target_page else 0)
        result.append(ProductRelationship(
            source=requested.source,
            target=requested.target,
            relation={"context_for": "context_for", "configures": "configures",
                      "depends_on": "depends_on", "proves": "proves"}.get(requested.relation, "related_to"),
            source_url=source_page.url if source_page else None,
            target_url=target_page.url if target_page else None,
            evidence_refs=evidence[:20],
            confidence=min(1.0, confidence),
        ))
    return result


_REVERSIBLE_ACTION_WORDS = {"new", "create", "add", "start", "open", "schedule", "book"}
_UNSAFE_ACTION_WORDS = {"delete", "remove", "send", "email", "message", "pay", "charge", "publish", "invite"}


def _capability_target(item: ObservedElement) -> Target:
    return Target(
        name=item.name,
        role=item.role or ("button" if item.tag == "button" else None),
        selector=item.selector if item.selector.startswith(("#", "[")) else None,
        text=item.name,
        source_url=item.source_url,
    )


def _page_knowledge(context: ProductContext) -> PageKnowledge:
    headings = [item.name for item in context.elements if item.tag in {"h1", "h2", "h3", "h4"}][:20]
    # Narrative blocks precede short control text. Otherwise a dense navigation
    # or link list can consume the fact budget before a card's contribution
    # evidence is available to the editorial writer.
    facts = list(dict.fromkeys([
        *context.content_blocks,
        *[" ".join((item.text or "").split())[:320] for item in context.elements if item.text and len(item.text.strip()) > 20],
    ]))[:30]
    all_controls = [item.name for item in context.elements if item.actionable and item.tag in {"a", "button", "input", "select"}]
    controls = list(all_controls[:30])
    # Accessibility snapshots often place the most relevant nested settings
    # controls after a long global navigation list. Preserve the bounded
    # default, but retain observed controls that overlap the request's
    # explicit feature/configuration vocabulary so relationship planning can
    # prove the correct context page without product-specific labels.
    objective_terms = _tokens(
        " ".join(getattr(context.objective, "requested_features", []) or [])
    ) if context.objective is not None else set()
    for name in all_controls[30:]:
        if objective_terms & _tokens(name) and name not in controls:
            controls.append(name)
    fingerprint = hashlib.sha256((context.url + context.title + context.visible_text[:2000]).encode()).hexdigest()[:20]
    screenshot = next(
        (item.removeprefix("screenshot:") for item in context.evidence if item.startswith("screenshot:")),
        None,
    )
    relationship_evidence = [
        item for item in context.evidence
        if item.startswith(("visible_relationship_control:", "relationship_context:"))
    ][:8]
    return PageKnowledge(
        url=context.url, title=context.title, purpose=(headings[0] if headings else context.title),
        visible_sections=headings, scroll_landmarks=headings, actionable_controls=controls,
        visible_facts=facts, loading_behavior=["network-idle or bounded hydration wait"],
        evidence_refs=[
            f"page:{context.url}", *[f"section:{heading}" for heading in headings[:12]],
            *relationship_evidence,
        ],
        screenshot_evidence=screenshot, fingerprint=fingerprint,
    )


def _restore_missing_page_landmarks(
    elements: list[ObservedElement], pages: list[PageKnowledge]
) -> list[ObservedElement]:
    """Keep every inspected page executable when a dense DOM crowds out headings.

    Discovery intentionally caps raw controls per page.  On long tables this
    can leave a page with durable ``PageKnowledge`` but no retained local
    landmark, which made a full-tour planner navigate there and immediately
    advance.  Reconstitute only the page's observed heading evidence as
    role-grounded ScrollTo targets.  These are not guessed selectors: the
    locator resolves by the exact accessible heading name at production time.
    """
    restored = list(elements)
    existing = {
        ((item.source_url or "").rstrip("/"), " ".join(item.name.split()).casefold())
        for item in restored
    }
    for page in pages:
        page_url = page.url.rstrip("/")
        labels = list(dict.fromkeys([*page.scroll_landmarks, *page.visible_sections]))[:12]
        for label in labels:
            normalized = " ".join(label.split())
            key = (page_url, normalized.casefold())
            if len(normalized) < 3 or key in existing:
                continue
            digest = hashlib.sha256(f"{page_url}|{normalized}".encode()).hexdigest()[:12]
            restored.append(
                ObservedElement(
                    tag="h2",
                    role="heading",
                    name=normalized,
                    selector=f"observed-heading:{digest}",
                    text=normalized,
                    source_url=page.url,
                    actionable=True,
                )
            )
            existing.add(key)
    return restored


class LiveDiscovery:
    """Inspects only the current page and its visible navigation; it never crawls blindly."""

    async def _visible_semantic_control(self, page, item: ObservedElement):
        """Resolve one currently visible discovery control without coordinates."""
        role = item.role if item.role in {"button", "link"} else "button"
        locators = [page.get_by_role(role, name=item.name, exact=True)]
        if item.selector.startswith(("#", "[")):
            locators.append(page.locator(item.selector))
        locators.append(page.get_by_text(item.name, exact=True))
        viewport = await page.evaluate("() => ({width: window.innerWidth, height: window.innerHeight})")
        deferred = None
        for locator in locators:
            for index in range(await locator.count()):
                candidate = locator.nth(index)
                if not await candidate.is_visible():
                    continue
                box = await candidate.bounding_box()
                if not box or box["width"] < 2 or box["height"] < 2:
                    continue
                in_viewport = (
                    box["x"] + box["width"] > 0
                    and box["x"] < viewport["width"]
                    and box["y"] + box["height"] > 0
                    and box["y"] < viewport["height"]
                )
                if in_viewport:
                    return candidate
                deferred = deferred or candidate
        if deferred is not None:
            # The semantic locator is still valid but currently below the
            # fold. Scroll its DOM element into the reading region, then
            # re-check geometry; do not treat Playwright's CSS visibility as
            # proof that an off-screen virtualized duplicate is actionable.
            try:
                await deferred.evaluate(
                    "element => element.scrollIntoView({block: 'center', inline: 'nearest'})"
                )
                await page.wait_for_timeout(150)
                box = await deferred.bounding_box()
                if (
                    box
                    and box["width"] >= 2
                    and box["height"] >= 2
                    and box["x"] + box["width"] > 0
                    and box["x"] < viewport["width"]
                    and box["y"] + box["height"] > 0
                    and box["y"] < viewport["height"]
                ):
                    return deferred
            except PlaywrightError:
                pass
        return None

    async def _form_schema_from_scope(self, scope, source_url: str) -> FormSchema:
        """Extract only stable, human-identifiable controls from an open form.

        A full-page inspection is intentionally broad for navigation and
        editorial evidence. It is unsafe as a form schema: design systems can
        expose anonymous helper inputs or repeated checkbox internals outside
        the active modal. The open dialog/form is the authoritative boundary.
        """
        raw = await scope.locator(
            "input, select, textarea, [contenteditable='true'], [role='combobox'], [role='textbox'], [role='searchbox'], [role='spinbutton'], [role='checkbox'], [role='radio']"
        ).evaluate_all(
            """nodes => nodes.map(node => {
                const text = value => (value || '').replace(/\\s+/g, ' ').trim();
                const labelled = (node.getAttribute('aria-labelledby') || '').split(/\\s+/)
                    .map(id => document.getElementById(id)?.innerText || '')
                    .map(text).filter(Boolean).join(' ');
                const labels = Array.from(node.labels || []).map(label => text(label.innerText)).filter(Boolean).join(' ');
                const enclosingLabel = text(node.closest('label')?.innerText || '');
                const idLabel = node.id ? text(document.querySelector(`label[for="${CSS.escape(node.id)}"]`)?.innerText || '') : '';
                // Component libraries commonly place the human label,
                // required marker, helper text and validation message around
                // a generated input rather than on the input itself.  Looking
                // only at `parentElement` loses that evidence for MUI/Radix
                // style comboboxes and causes an unsafe partial submit later.
                // Keep the evidence local to this field: described-by nodes
                // are explicitly associated with the control, while the
                // nearest semantic/form-control ancestor carries its label.
                const describedBy = (node.getAttribute('aria-describedby') || '').split(/\\s+/)
                    .map(id => document.getElementById(id)?.innerText || '')
                    .map(text).filter(Boolean).join(' ');
                const fieldContainer = node.closest(
                    '[role="group"], .MuiFormControl-root, [data-field], [data-slot="field"]'
                ) || node.parentElement?.parentElement || node.parentElement;
                const localContext = text([fieldContainer?.innerText || '', describedBy].filter(Boolean).join(' '));
                const selectedText = node.tagName.toLowerCase() === 'select' && node.selectedOptions?.length
                    ? text(node.selectedOptions[0].innerText || node.selectedOptions[0].textContent || '')
                    : '';
                const displayText = text(selectedText || node.innerText || node.value || '');
                const ariaLabel = text(node.getAttribute('aria-label') || '');
                // Some component libraries expose a locale/value code such
                // as ``en`` through aria-label while the visible control says
                // ``English``.  A short code is not useful narration or
                // grounding evidence when a readable selected label exists,
                // so prefer the visible label without relying on any product
                // vocabulary.
                const accessibleLabel = ariaLabel && ariaLabel.length <= 3 && displayText.length > ariaLabel.length
                    ? displayText
                    : ariaLabel;
                const semanticLabel = text(
                    accessibleLabel || labelled || labels || enclosingLabel || idLabel ||
                    node.getAttribute('placeholder') || node.getAttribute('name') || displayText || ''
                );
                // A visible asterisk is direct required-field evidence, not
                // part of the label later used for semantic grounding.
                const semanticName = text(semanticLabel.replace(/\\*+/g, ' '));
                const stableId = node.id && !/^:r[0-9a-z]+:$/i.test(node.id);
                const selector = node.getAttribute('data-testid') ? `[data-testid="${CSS.escape(node.getAttribute('data-testid'))}"]` :
                    node.getAttribute('name') ? `[name="${CSS.escape(node.getAttribute('name'))}"]` :
                    node.getAttribute('placeholder') ? `[placeholder="${CSS.escape(node.getAttribute('placeholder'))}"]` :
                    stableId ? `#${CSS.escape(node.id)}` :
                    node.getAttribute('aria-label') ? `[aria-label="${CSS.escape(node.getAttribute('aria-label'))}"]` :
                    // The execution target retains the accessible label and
                    // deliberately ignores this marker as CSS. It is safer
                    // than dropping a labelled control merely because a
                    // component did not expose a stable attribute selector.
                    `label:${semanticName}`;
                const role = node.getAttribute('role') || '';
                const type = (node.getAttribute('type') || node.tagName.toLowerCase()).toLowerCase();
                const controlType = role || (
                    node.getAttribute('aria-haspopup') === 'listbox' ||
                    node.getAttribute('aria-autocomplete') === 'list'
                        ? 'combobox'
                        : type
                );
                return {
                    name: semanticName, selector, controlType,
                    // Browser-native validation is ideal, but form libraries
                    // often expose an asterisk only through the visible label.
                    // It is still direct UI evidence and lets a safe planner
                    // prefer the fields a person is expected to complete.
                    required: node.required === true || node.getAttribute('aria-required') === 'true' ||
                        /\\*/.test(semanticLabel) || /\\brequired\\b/i.test(localContext),
                    visible: !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length),
                    disabled: node.disabled === true || node.getAttribute('aria-disabled') === 'true',
                    options: node.tagName.toLowerCase() === 'select'
                        ? Array.from(node.options).map(option => option.value || option.text).filter(Boolean).slice(0, 40)
                        : [],
                };
            }).filter(item => item.visible && !item.disabled && item.selector && item.name &&
                !/^(?:element-\\d+|on|off|x|true|false|\\d+)$/i.test(item.name) &&
                !['hidden', 'submit', 'button', 'reset'].includes(item.controlType))"""
        )
        fields: list[FormField] = []
        seen: set[tuple[str, str]] = set()
        for item in raw:
            key = (str(item["selector"]), str(item["name"]).casefold())
            if key in seen:
                continue
            seen.add(key)
            fields.append(FormField(
                name=str(item["name"])[:200], selector=str(item["selector"]),
                control_type=str(item["controlType"]), required=bool(item["required"]),
                options=[str(value)[:200] for value in item.get("options", [])], confidence=0.95,
            ))
        scope_text = (await scope.inner_text())[:8_000]
        observed_required = any(field.required for field in fields)
        # "Required" is an explicit visible form-state signal. If the scoped
        # DOM exposes it but no required editable control could be grounded,
        # preserve that uncertainty so the workflow cannot submit guessed or
        # partial data. This remains generic across design systems.
        unresolved_required = bool(re.search(r"\brequired\b", scope_text, flags=re.IGNORECASE)) and not observed_required
        evidence = ["active form scope", "accessible label/placeholder/name", "stable semantic selector"]
        if unresolved_required:
            evidence.append("unresolved visible required controls")
        return FormSchema(
            source_url=source_url, fields=fields, evidence=evidence,
            unresolved_required_fields=unresolved_required,
        )

    async def _enrich_choice_options(self, page, schema: FormSchema) -> FormSchema:
        """Record observed choices for native and accessible select controls.

        Opening a choice list is reversible and happens only inside the
        already-open form probe. A production plan can therefore select a
        real option rather than inventing a value for a custom combobox.
        """
        enriched = []
        for field in schema.fields:
            if field.control_type.casefold() not in {"select", "combobox"} or field.options:
                enriched.append(field)
                continue
            locators = [page.get_by_label(field.name, exact=True)]
            if field.control_type.casefold() == "combobox":
                locators.append(page.get_by_role("combobox", name=field.name, exact=True))
            if field.selector.startswith(("#", "[")):
                locators.append(page.locator(field.selector))
            control = None
            for locator in locators:
                for index in range(await locator.count()):
                    candidate = locator.nth(index)
                    if await candidate.is_visible():
                        control = candidate
                        break
                if control is not None:
                    break
            if control is None and field.control_type.casefold() == "combobox":
                # A number of design systems render a visually labelled
                # combobox without a `for`, aria-label, or aria-labelledby
                # relationship.  Do not fall back to a coordinate or a broad
                # first-combobox click. Instead, find the unique visible
                # combobox whose *nearest* readable ancestor contains this
                # field's observed label. This is DOM evidence and remains
                # safe across component libraries.
                semantic_candidates = await page.locator("[role='combobox']").evaluate_all(
                    """(nodes, fieldName) => {
                        const normal = value => (value || '').replace(/\\s+/g, ' ').trim().toLocaleLowerCase();
                        const sought = normal(fieldName);
                        return nodes.map((node, index) => {
                            let parent = node.parentElement;
                            for (let depth = 1; parent && depth <= 6; depth += 1, parent = parent.parentElement) {
                                const label = normal(parent.innerText);
                                if (label.includes(sought) && label.length <= 420) return {index, depth};
                            }
                            return null;
                        }).filter(Boolean);
                    }""",
                    field.name,
                )
                if semantic_candidates:
                    nearest_depth = min(int(item["depth"]) for item in semantic_candidates)
                    nearest = [item for item in semantic_candidates if int(item["depth"]) == nearest_depth]
                    if len(nearest) == 1:
                        candidate = page.locator("[role='combobox']").nth(int(nearest[0]["index"]))
                        if await candidate.is_visible():
                            control = candidate
            if control is None:
                enriched.append(field)
                continue
            try:
                # Cloud CDP actionability can take a few seconds after a
                # dialog's entrance transition. A 1.5-second probe timeout
                # made a real, visible selector appear optionless and later
                # authorised an incomplete submit. This remains a bounded,
                # reversible discovery click; it is not a production retry.
                await control.click(timeout=5_000)
                # Some accessible comboboxes expose their first available
                # choice only after keyboard expansion. ArrowDown is a
                # reversible inspection gesture: it changes neither the form
                # value nor product state, and lets discovery observe choices
                # without fabricating a search term or selecting anything.
                if field.control_type.casefold() == "combobox":
                    try:
                        await control.press("ArrowDown")
                    except PlaywrightError:
                        pass
                # Remote/custom comboboxes commonly fetch their list after
                # the click.  A single 200ms snapshot made ProductLens treat
                # an otherwise valid dependent control as optionless. Wait
                # only for *visible observed* choices, never for a guessed
                # value or an arbitrary fixed form delay.
                values: list[str] = []
                for _attempt in range(8):
                    options = page.locator(
                        "[role='option'], [role='listbox'] li, [role='menu'] [role='menuitem']"
                    )
                    for index in range(await options.count()):
                        option = options.nth(index)
                        if not await option.is_visible():
                            continue
                        label = " ".join((await option.inner_text()).split())
                        if label:
                            values.append(label)
                    if values:
                        break
                    await page.wait_for_timeout(300)
                await page.keyboard.press("Escape")
                enriched.append(field.model_copy(update={"options": list(dict.fromkeys(values))[:40]}))
            except PlaywrightError:
                try:
                    await page.keyboard.press("Escape")
                except PlaywrightError:
                    pass
                enriched.append(field)
        return schema.model_copy(update={"fields": enriched})

    async def inspect(
        self, page, objective: str, budget: DiscoveryBudget | None = None
    ) -> ProductContext:
        budget = budget or DiscoveryBudget()
        if budget.max_pages < 1 or budget.max_actions < 1:
            raise ValueError("Discovery budget does not permit a page inspection")
        # SPA controls often appear immediately after DOMContentLoaded.  Give the
        # hydrated accessibility tree a brief, bounded settling window.
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except PlaywrightTimeoutError:
            # Long-polling applications may never be idle; the short fallback is
            # still enough to inspect their hydrated DOM without stalling a run.
            await page.wait_for_timeout(700)
        title = await page.title()
        visible_text = (await page.locator("body").inner_text())[:8_000]
        # Cards and article bodies carry the explanation a viewer needs, but
        # are often not actionable controls. Capture their visible prose as
        # evidence for the editorial layer without turning them into click
        # targets or treating a title as a complete fact.
        raw_blocks = await page.locator("main section, main article, section, article, [data-testid]").evaluate_all(
            """nodes => nodes.slice(0, 40).map(node => (node.innerText || '').replace(/\\s+/g, ' ').trim()).filter(text => text.length >= 40).slice(0, 20)"""
        )
        # Some content/timeline cards are plain divs rather than semantic
        # sections or heading containers. Select bounded *leaf-like* readable
        # cards so a company/project title retains the visible role and
        # contribution text needed for editorial narration.
        card_blocks = await page.locator("main div").evaluate_all(
            """nodes => nodes.map(node => {
                const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                const childWithSameText = Array.from(node.children).some(child =>
                    ((child.innerText || '').replace(/\\s+/g, ' ').trim()) === text
                );
                return {text, childWithSameText};
            }).filter(item => item.text.length >= 120 && item.text.length <= 1400 && !item.childWithSameText)
              .map(item => item.text).slice(0, 40)"""
        )
        # Component libraries frequently render project cards as nested divs
        # rather than semantic articles. Associate every visible heading with
        # its nearest readable container so editorial facts retain the card's
        # description, role, contribution, and outcome—not merely its title.
        heading_blocks = await page.locator("h1,h2,h3,h4").evaluate_all(
            """nodes => nodes.slice(0, 60).map(node => {
                const heading = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                let parent = node.parentElement, body = '';
                while (parent) {
                    const candidate = (parent.innerText || '').replace(/\\s+/g, ' ').trim();
                    // Rich timeline cards can legitimately exceed a small
                    // container threshold. Keep the nearest readable card and
                    // apply the artifact size limit after evidence extraction.
                    if (candidate.length >= 80) { body = candidate; break; }
                    parent = parent.parentElement;
                }
                return heading && body ? `${heading} :: ${body}` : '';
            }).filter(Boolean).slice(0, 50)"""
        )
        content_blocks = list(dict.fromkeys(str(block)[:700] for block in [*heading_blocks, *raw_blocks, *card_blocks]))
        # Some dashboard shells do not expose a semantic ``main`` container
        # and render their page purpose as short text nodes between controls.
        # Without a fallback, PageKnowledge contains only date chips and the
        # editorial layer has no truthful purpose sentence to use. Collect a
        # bounded set of visible, action-oriented body lines; this remains
        # evidence extraction, never a product-specific template.
        if len(content_blocks) < 3:
            body_lines = await page.locator("body").evaluate(
                "node => (node.innerText || '').split(/\\n+/).map(value => value.replace(/\\s+/g, ' ').trim()).filter(Boolean)"
            )
            action_words = re.compile(
                r"\\b(?:manage|track|review|schedule|configure|organize|monitor|plan|create|compare|follow|coordinate|support|filter|list|build|edit|transition|confirm)\\b",
                re.IGNORECASE,
            )
            fallback_blocks = [
                str(line)[:320]
                for line in body_lines
                if isinstance(line, str)
                and 4 <= len(line.split()) <= 18
                and action_words.search(line)
            ]
            content_blocks = list(dict.fromkeys([*content_blocks, *fallback_blocks]))[:50]
        # Headings are also valid presentation landmarks.  They are not
        # necessarily clickable, but retaining them lets a director create a
        # natural scroll tour of a project collection rather than jumping to
        # whichever CTA happens to be actionable.
        raw = await page.locator("a,button,input,select,textarea,[role='button'],[role='combobox'],[role='option'],[role='checkbox'],[role='radio'],[role='alert'],[role='status'],[role='dialog'],[role='row'],[role='gridcell'],tr,td,li,h1,h2,h3,h4").evaluate_all(
            """nodes => nodes.map((node, index) => ({
                // Keep the same human field identity used by scoped form
                // discovery. A placeholder is an implementation hint, not
                // presenter copy; using it as a scene name can leak a sample
                // email and makes a visible labelled form look anonymous.
                name: (() => {
                    const text = value => (value || '').replace(/\\s+/g, ' ').trim();
                    const labelled = (node.getAttribute('aria-labelledby') || '').split(/\\s+/)
                        .map(id => document.getElementById(id)?.innerText || '').map(text).filter(Boolean).join(' ');
                    const labels = Array.from(node.labels || []).map(label => text(label.innerText)).filter(Boolean).join(' ');
                    const enclosing = text(node.closest('label')?.innerText || '');
                    const idLabel = node.id ? text(document.querySelector(`label[for="${CSS.escape(node.id)}"]`)?.innerText || '') : '';
                    return text(node.getAttribute('aria-label') || labelled || labels || enclosing || idLabel ||
                        node.getAttribute('name') || node.getAttribute('placeholder') ||
                        (/^h[1-4]$/i.test(node.tagName) ? (node.innerText || '').split(/\\n/)[0] : '') ||
                        node.innerText || node.value || `element-${index}`);
                })(),
                tag: node.tagName.toLowerCase(),
                // Headings inside a card-link are still excellent reading
                // landmarks, but they are also a discoverable, semantic way
                // to enter the representative detail. Retain the ancestor's
                // route instead of losing it just because the card's label is
                // rendered by a nested heading.
                role: node.getAttribute('role') || node.closest('a,button,[role="button"]')?.getAttribute('role'),
                // Component libraries often generate ids such as ``:r30:``
                // for a single mount.  Those are useful neither in a fresh
                // rehearsal nor in production, so prefer stable semantic
                // attributes before accepting a real, non-generated id.
                selector: node.getAttribute('data-testid') ? `[data-testid="${node.getAttribute('data-testid')}"]` :
                    node.getAttribute('name') ? `[name="${node.getAttribute('name')}"]` :
                    node.getAttribute('placeholder') ? `[placeholder="${node.getAttribute('placeholder')}"]` :
                    (node.id && !/^:r[0-9a-z]+:$/i.test(node.id)) ? `#${CSS.escape(node.id)}` :
                    node.tagName.toLowerCase(),
                href: node.getAttribute('href') || node.closest('a[href]')?.getAttribute('href'), type: node.getAttribute('type'),
                required: node.required === true, autocomplete: node.getAttribute('autocomplete'),
                options: node.tagName.toLowerCase() === 'select' ? Array.from(node.options).map(option => option.value || option.text).filter(Boolean).slice(0, 40) : [],
                text: node.innerText || null,
                formPriority: node.closest('form,[role="dialog"],dialog') ? 1 : 0,
                navigation_scope: (() => { const container=node.closest('header,nav,footer,[role="navigation"]'); if (!container) return 'unknown'; if (container.tagName.toLowerCase()==='footer') return 'footer'; return 'primary'; })(),
                visible: !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length)
            })).sort((left, right) => right.formPriority - left.formPriority || Number(right.visible) - Number(left.visible)).slice(0, 160).map(({formPriority, ...item}) => item)"""
        )
        elements = [
            ObservedElement(
                tag=item["tag"],
                role=item.get("role"),
                name=str(item["name"]).strip()[:300],
                selector=item["selector"],
                href=item.get("href"),
                element_type=item.get("type"),
                required=bool(item.get("required")),
                options=[str(option)[:200] for option in item.get("options", [])],
                autocomplete=item.get("autocomplete"),
                text=item.get("text"),
                source_url=page.url,
                actionable=item["visible"],
                navigation_scope=item.get("navigation_scope", "unknown"),
            )
            for item in raw
            if item["visible"] and str(item["name"]).strip()
        ]
        objective_words = _tokens(objective)
        origin = urlparse(page.url)
        navigation = [item for item in elements if item.href]
        ranked = sorted(
            navigation,
            key=lambda item: _route_score(item, objective_words),
            reverse=True,
        )
        routes: list[str] = []
        canonical_routes: set[str] = set()
        for item in ranked:
            absolute = urljoin(page.url, item.href or "")
            parsed = urlparse(absolute)
            if parsed.scheme not in {"http", "https", "file"} or parsed.netloc not in {
                "",
                origin.netloc,
            }:
                continue
            canonical = _canonical_route(absolute)
            if canonical not in canonical_routes:
                routes.append(absolute)
                canonical_routes.add(canonical)
            if len(routes) >= max(1, budget.max_pages - 1):
                break
        password_control = any("password" in (item.element_type or "").lower() for item in elements)
        # A public automation sandbox may intentionally expose a *login
        # example* alongside many other controls.  Treating any visible
        # password input as an authentication wall incorrectly blocks
        # preflight and full-tour planning.  Require a semantic auth heading
        # or an auth-like route before classifying the opening page as gated.
        auth_heading = any(
            item.tag in {"h1", "h2", "h3"}
            and re.search(r"\b(?:sign[ -]?in|log[ -]?in|authenticate)\b", item.name, re.IGNORECASE)
            for item in elements
        )
        auth_route = bool(re.search(r"/(?:login|signin|sign-in|auth)(?:/|$)", origin.path, re.IGNORECASE))
        login = password_control and (auth_heading or auth_route)
        return ProductContext(
            url=page.url,
            title=title[:500],
            application_type=_classify(title, visible_text, elements),
            authentication_state="login_required" if login else "unknown",
            visible_text=visible_text,
            content_blocks=content_blocks,
            relevant_routes=routes,
            navigation=navigation[:40],
            # Form/dialog controls take priority above and remain intact here;
            # a global navigation-heavy shell must not erase the submit/result
            # evidence needed to compile a safe capability.
            elements=elements[:160],
            blockers=["authentication required"] if login else [],
            evidence=[
                "current DOM",
                "visible text",
                "accessible names",
                "same-origin navigation only",
            ],
            confidence=0.8 if elements else 0.2,
        )

    async def _probe_reversible_capabilities(
        self,
        page,
        context: ProductContext,
        objective: str,
        *,
        remaining: int,
    ) -> tuple[list[ActionCapability], list[str], list[str]]:
        """Open a few reversible product controls to learn their real schema.

        A page title or a visible "Create" button is not workflow evidence.
        This probe is deliberately constrained to non-link controls with a
        reversible intent, and it never fills or submits.  It gives planning
        a generic form/modal capability without turning discovery into a
        crawler or changing product data.
        """
        if remaining <= 0:
            return [], [], []
        wanted = _tokens(objective)
        ranked: list[tuple[int, ObservedElement]] = []
        for item in context.elements:
            if not item.actionable or item.href or item.tag not in {"button", "input"}:
                continue
            words = _tokens(f"{item.name} {item.text or ''}")
            if words & _UNSAFE_ACTION_WORDS or not (words & _REVERSIBLE_ACTION_WORDS):
                continue
            ranked.append((len(words & wanted) * 3 + len(words & _REVERSIBLE_ACTION_WORDS), item))
        capabilities: list[ActionCapability] = []
        actions: list[str] = []
        blockers: list[str] = []
        for _, item in sorted(ranked, key=lambda candidate: -candidate[0])[:remaining]:
            prior_url = page.url
            try:
                visible = await self._visible_semantic_control(page, item)
                if visible is None:
                    continue
                # A visible control can still be completing a design-system
                # entrance/portal transition immediately after a route probe.
                # Give the browser one bounded paint interval before deciding
                # it is non-actionable; production has its own stricter scene
                # readiness checks and never relies on this discovery timing.
                await page.wait_for_timeout(500)
                try:
                    await visible.click(timeout=5_000)
                except PlaywrightTimeoutError:
                    # This remains a safe, pre-submit exploration click on an
                    # already verified visible semantic control. A single
                    # force attempt handles transient sticky headers or
                    # animation wrappers without broadening to coordinates.
                    await visible.click(timeout=3_000, force=True)
                await page.wait_for_timeout(500)
                dialogs = page.get_by_role("dialog")
                dialog = None
                for index in range(await dialogs.count()):
                    candidate = dialogs.nth(index)
                    if await candidate.is_visible():
                        dialog = candidate
                        break
                if dialog is None:
                    # A form may be rendered in a page-side panel rather than
                    # a semantic dialog. It is still safe to record only when
                    # it exposes an actual visible form.
                    forms = page.locator("form")
                    scope = None
                    for index in range(await forms.count()):
                        candidate = forms.nth(index)
                        if await candidate.is_visible():
                            scope = candidate
                            break
                else:
                    scope = dialog
                if scope is None:
                    if _canonical_route(page.url) != _canonical_route(prior_url):
                        await page.go_back(wait_until="domcontentloaded")
                    else:
                        await page.keyboard.press("Escape")
                    continue
                observed = await self.inspect(page, objective)
                schema: FormSchema = await self._enrich_choice_options(
                    page, await self._form_schema_from_scope(scope, page.url)
                )
                if not schema.fields:
                    await page.keyboard.press("Escape")
                    continue
                submit_target = None
                close_target = None
                for candidate in observed.elements:
                    name = candidate.name.lower()
                    if candidate.tag in {"button", "input"} and submit_target is None and (
                        "submit" in name or "save" in name or "create" in name or "add" in name
                    ):
                        submit_target = _capability_target(candidate)
                    if candidate.tag == "button" and close_target is None and any(word in name for word in ("close", "cancel", "dismiss")):
                        close_target = _capability_target(candidate)
                if close_target is None:
                    await page.keyboard.press("Escape")
                else:
                    closer = page.get_by_role("button", name=close_target.name, exact=True)
                    if await closer.count():
                        await closer.first.click()
                    else:
                        await page.keyboard.press("Escape")
                await page.wait_for_timeout(250)
                capabilities.append(ActionCapability(
                    kind="form", purpose=item.name, source_url=prior_url,
                    entry_target=_capability_target(item), form_schema=schema,
                    submit_target=submit_target, close_target=close_target,
                    evidence_refs=[f"capability:{prior_url}:{item.name}", *schema.evidence],
                    # Discovery proves only that a reversible form can be
                    # opened.  It must never imply that submission succeeds
                    # or that a new record has appeared; rehearsal promotes
                    # it only after an independent visible outcome witness.
                    verified=False,
                ))
                actions.append(f"reversible_form_probe:{item.name}")
            except PlaywrightError as error:
                blockers.append(f"capability_probe_failed:{item.name}:{str(error)[:120]}")
                try:
                    await page.keyboard.press("Escape")
                except PlaywrightError:
                    pass
        return capabilities, actions, blockers

    async def discover(
        self,
        page,
        objective: str,
        budget: DiscoveryBudget | None = None,
        known_routes: list[str] | None = None,
        known_actions: list[dict] | None = None,
        explore_visible_routes: bool = False,
        screenshot_directory: Path | None = None,
        objective_spec: ObjectiveSpec | None = None,
        known_product_fingerprint: str | None = None,
    ) -> ProductContext:
        """Explore a bounded set of objective-ranked, same-origin routes and restore the entry URL."""
        budget = budget or DiscoveryBudget()
        entry_url = page.url
        discovery_started = asyncio.get_running_loop().time()

        async def revive_page_if_closed() -> bool:
            """Reopen the authenticated entry state after a transient probe closure.

            Remote CDP browsers can close a page when a reversible control opens
            a portal/new target or when an extension probe tears down its target.
            Discovery evidence collected before that event is still valid; the
            route collector only needs a live page to continue.  Reusing the
            existing browser context preserves cookies without replaying a
            production navigation (this helper is exploration-only).
            """
            nonlocal page
            try:
                if not page.is_closed():
                    return True
            except PlaywrightError:
                pass
            try:
                browser_context = page.context
                page = await browser_context.new_page()
                await page.goto(entry_url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(1_100)
                return not page.is_closed()
            except PlaywrightError:
                return False

        async def reopen_unresponsive_page(url: str) -> bool:
            """Replace a live-but-unresponsive remote target without losing auth.

            CDP can leave ``page.is_closed()`` false while a renderer target
            stops answering (a common failure after a long SPA transition).
            Reusing the existing context preserves authenticated storage and
            avoids replaying credentials; the old target is closed only on a
            best-effort basis.
            """
            nonlocal page
            try:
                browser_context = page.context
                replacement = await asyncio.wait_for(browser_context.new_page(), timeout=10)
                await asyncio.wait_for(
                    replacement.goto(url, wait_until="domcontentloaded", timeout=20_000),
                    timeout=25,
                )
                await replacement.wait_for_timeout(700)
                old_page = page
                page = replacement
                with suppress(Exception):
                    await asyncio.wait_for(old_page.close(), timeout=5)
                return not page.is_closed()
            except (PlaywrightError, TimeoutError):
                return False

        async def inspect_with_scroll_evidence(recovery_url: str | None = None) -> ProductContext:
            """Collect bounded lower-page evidence without turning discovery into capture.

            Animated timelines and card collections often hydrate their detailed
            content only after it enters the viewport.  Discovery therefore
            samples two semantic scroll regions, merges the observed evidence,
            and restores the opening position before planning/recording.
            """
            # Route transitions in animated SPAs may report network-idle before
            # their timeline/card components have committed.  A bounded settle
            # window makes visible contribution text discoverable without
            # coupling discovery to arbitrary long sleeps.
            await page.wait_for_timeout(1_100)
            try:
                initial = await asyncio.wait_for(
                    self.inspect(page, objective, budget), timeout=30
                )
            except TimeoutError:
                # A remote renderer may remain nominally open but stop
                # answering DOM calls. Recreate only the exploration target,
                # then retry once against the same route; production remains
                # isolated and never inherits this recovery.
                target_url = recovery_url or entry_url
                if not await reopen_unresponsive_page(target_url):
                    raise
                initial = await asyncio.wait_for(
                    self.inspect(page, objective, budget), timeout=30
                )
            if screenshot_directory is not None:
                screenshot_directory.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256(_canonical_route(page.url).encode("utf-8")).hexdigest()[:16]
                screenshot = screenshot_directory / f"{digest}.png"
                await page.screenshot(path=str(screenshot), full_page=False)
                initial = initial.model_copy(
                    update={
                        "evidence": [
                            *initial.evidence,
                            f"screenshot:discovery/screenshots/{screenshot.name}",
                        ]
                    }
                )
            samples = [initial]
            try:
                metrics = await page.evaluate(
                    "() => ({height: document.documentElement.scrollHeight, viewport: window.innerHeight})"
                )
                height, viewport = float(metrics["height"]), float(metrics["viewport"])
                if height > viewport * 1.35:
                    for fraction in (0.45, 0.9):
                        await page.evaluate("y => window.scrollTo({top: y, behavior: 'instant'})", max(0, height * fraction - viewport * 0.4))
                        await page.wait_for_timeout(1_000)
                        samples.append(await asyncio.wait_for(
                            self.inspect(page, objective, budget), timeout=30
                        ))
            finally:
                await page.evaluate("() => window.scrollTo({top: 0, behavior: 'instant'})")
            elements: list[ObservedElement] = []
            seen_elements: set[tuple[str | None, str, str]] = set()
            for sample in samples:
                for item in sample.elements:
                    key = (item.source_url, item.selector, item.name)
                    if key not in seen_elements:
                        seen_elements.add(key)
                        elements.append(item)
            blocks = list(dict.fromkeys(block for sample in samples for block in sample.content_blocks))
            return initial.model_copy(update={
                "elements": elements,
                "content_blocks": blocks,
                "visible_text": max((sample.visible_text for sample in samples), key=len),
                "evidence": [*initial.evidence, "bounded scroll-state evidence"],
            })

        primary = await inspect_with_scroll_evidence()
        objective_spec = objective_spec or _objective_spec(objective)
        primary_route_count = len({
            _canonical_route(urljoin(entry_url, item.href))
            for item in primary.navigation
            if item.href
            and urlparse(urljoin(entry_url, item.href)).netloc in {"", urlparse(entry_url).netloc}
            and _canonical_route(urljoin(entry_url, item.href)) != _canonical_route(entry_url)
        })
        original_budget = budget
        budget = adaptive_exploration_budget(
            budget, objective_spec, primary_route_count=primary_route_count
        )
        # A fresh timestamp alone does not prove the product is unchanged.
        # Reuse cached routes/actions only when the current opening page has
        # the same content fingerprint; otherwise the live DOM remains the
        # sole source of route relevance and the cache is ignored for this
        # exploration.
        current_fingerprint = _page_knowledge(primary).fingerprint
        cache_matches = bool(
            known_product_fingerprint
            and known_product_fingerprint == current_fingerprint
        )
        capabilities, capability_actions, capability_blockers = await self._probe_reversible_capabilities(
            page, primary, objective, remaining=max(0, min(3, budget.max_actions // 6))
        )
        # A probe must never make the rest of discovery unusable. If a remote
        # target disappeared, reopen the same authenticated entry state and
        # refresh its page-local evidence before traversing visible routes.
        if await revive_page_if_closed():
            if any("Target page" in item for item in capability_blockers):
                primary = await inspect_with_scroll_evidence()
        if explore_visible_routes:
            origin = urlparse(entry_url)
            visible_routes: list[str] = []
            for item in primary.navigation:
                absolute = urljoin(entry_url, item.href or "")
                parsed = urlparse(absolute)
                if parsed.scheme in {"http", "https", "file"} and parsed.netloc in {"", origin.netloc}:
                    visible_routes.append(absolute)
            # A thorough walkthrough must give every safe primary tab a chance
            # to contribute page-local knowledge.  Keep the visible navigation
            # controls ahead of deep cards such as Week 1, while retaining the
            # objective-ranked candidates afterwards for feature requests.
            primary_controls = [
                route for route in visible_routes
                if urlparse(route).path.count("/") <= 1
            ]
            ordered_routes = [
                *primary_controls,
                *primary.relevant_routes,
                *visible_routes,
            ] if objective_spec.demo_type == "full_walkthrough" else [
                # For a focused request, visible navigation is the strongest
                # discovery signal.  Rank it by semantic overlap before
                # cached/model-ranked routes; otherwise a bounded page budget
                # can be consumed by generic dashboard links before the
                # requested module (for example a feature route that is
                # visible in the sidebar).  This remains product-neutral: the
                # score uses the objective words, label, and route path only.
                *sorted(visible_routes, key=lambda route: -_route_objective_score(route, primary.navigation, objective_spec)),
                *primary.relevant_routes,
            ]
            deduped_routes: list[str] = []
            seen_routes: set[str] = set()
            for route in ordered_routes:
                canonical = _canonical_route(route)
                if canonical not in seen_routes:
                    seen_routes.add(canonical)
                    deduped_routes.append(route)
            primary = primary.model_copy(
                # Preserve objective-ranked routes first. Replacing them with
                # DOM navigation order made a small exploration budget inspect
                # generic dashboard links before the route relevant to the
                # requested workflow.
                update={
                    "relevant_routes": deduped_routes
                }
            )
        same_origin_known = [
            route
            for route in (known_routes or []) if cache_matches
            if urlparse(route).netloc in {"", urlparse(entry_url).netloc}
        ]
        # Fresh visible primary controls outrank cached/deep routes for a full
        # walkthrough.  Cached knowledge still participates, but must not turn
        # a whole-product tour into a sweep of repeated detail URLs.
        # Fresh evidence must lead for focused requests as well as full tours.
        # Cached knowledge is useful as a fallback, but putting it first lets
        # stale/generic routes consume the bounded page budget before a newly
        # visible objective route (for example ``/bookings``) is inspected.
        # This ordering is deliberately product-neutral and is later
        # canonicalized/deduplicated below.
        route_sources = [*primary.relevant_routes, *same_origin_known]
        deduped_route_sources: list[str] = []
        seen_route_sources: set[str] = set()
        for route in route_sources:
            canonical = _canonical_route(route)
            if canonical not in seen_route_sources:
                seen_route_sources.add(canonical)
                deduped_route_sources.append(route)
        primary = primary.model_copy(
            update={
                "relevant_routes": deduped_route_sources,
                "evidence": [*primary.evidence, "fresh knowledge routes re-grounded"],
            }
        )
        collected = [primary]
        navigation_probes: list[str] = []
        rejected_routes: list[str] = []
        try:
            entry_canonical = _canonical_route(entry_url)
            # The collection loop has its own full-walkthrough ordering rather
            # than relying on a route list which may include deep links from a
            # cached knowledge record.  This guarantees that top-level visible
            # controls (Weeks, DSA, Builds, Progress) are inspected before any
            # repeated week/card routes.
            routes_to_inspect = list(primary.relevant_routes)
            if objective_spec.demo_type == "full_walkthrough":
                primary_routes = [
                    urljoin(entry_url, item.href or "")
                    for item in primary.navigation
                    if item.href
                    and urlparse(urljoin(entry_url, item.href)).netloc in {"", urlparse(entry_url).netloc}
                    and urlparse(urljoin(entry_url, item.href)).path.count("/") <= 1
                ]
                routes_to_inspect = [*primary_routes, *routes_to_inspect]
            seen_inspection_routes: set[str] = set()
            visible_controls_by_route: dict[str, ObservedElement] = {}
            for item in primary.navigation:
                if not item.href or not item.actionable:
                    continue
                destination = urljoin(entry_url, item.href)
                if urlparse(destination).netloc not in {"", urlparse(entry_url).netloc}:
                    continue
                visible_controls_by_route.setdefault(_canonical_route(destination), item)
            for route_index, route in enumerate(routes_to_inspect):
                if asyncio.get_running_loop().time() - discovery_started >= budget.max_time_seconds:
                    rejected_routes.append("discovery_time_budget_exhausted")
                    break
                if not await revive_page_if_closed():
                    rejected_routes.append(f"page_unavailable:{route}")
                    continue
                # The opening page is already represented by ``primary``.
                # Skipping its canonical equivalents preserves the bounded
                # budget for actual primary sections.
                canonical = _canonical_route(route)
                if canonical == entry_canonical or canonical in seen_inspection_routes:
                    continue
                seen_inspection_routes.add(canonical)
                if len(collected) >= budget.max_pages:
                    break
                control = visible_controls_by_route.get(canonical)
                used_visible_control = False
                if control is not None:
                    try:
                        # Responsive layouts legitimately duplicate a semantic
                        # navigation label.  A unique accessible name is not a
                        # prerequisite for visible navigation: choose the first
                        # *visible* same-destination anchor in DOM order, which
                        # remains a real product control rather than falling
                        # back to a direct URL merely because mobile markup is
                        # also present in the accessibility tree.
                        anchors = page.locator("a")
                        for index in range(await anchors.count()):
                            candidate = anchors.nth(index)
                            href = await candidate.get_attribute("href")
                            if not href or _canonical_route(urljoin(page.url, href)) != canonical:
                                continue
                            if not await candidate.is_visible():
                                continue
                            try:
                                await candidate.click(timeout=2_500)
                            except PlaywrightError:
                                # Cloud browsers occasionally report a
                                # transient animation overlay over a genuine
                                # visible navigation control. This is still a
                                # semantic element click (not coordinates or
                                # direct navigation); force is limited to
                                # read-only same-origin anchor probes.
                                await candidate.click(timeout=2_500, force=True)
                            await page.wait_for_timeout(650)
                            used_visible_control = _canonical_route(page.url) == canonical
                            if used_visible_control:
                                break
                    except PlaywrightError:
                        used_visible_control = False
                if used_visible_control:
                    navigation_probes.append(f"visible_navigation:{control.name}")
                else:
                    # Navigation is direct only when no visible, uniquely
                    # resolvable same-origin link is available at this state.
                    try:
                        # A route probe is bounded discovery evidence, not a
                        # production navigation. Keep an unresponsive SPA
                        # route from consuming the entire adaptive budget.
                        await page.goto(route, wait_until="domcontentloaded", timeout=15_000)
                    except PlaywrightError:
                        rejected_routes.append(f"navigation_failed:{route}")
                        continue
                    navigation_probes.append(f"direct_navigation_fallback:{route}")
                inspected = await inspect_with_scroll_evidence(recovery_url=route)
                remaining_capabilities = max(0, min(3, budget.max_actions // 6) - len(capabilities))
                if remaining_capabilities:
                    found, actions, blockers = await self._probe_reversible_capabilities(
                        page, inspected, objective, remaining=remaining_capabilities
                    )
                    capabilities.extend(found)
                    capability_actions.extend(actions)
                    capability_blockers.extend(blockers)
                collected.append(inspected)
                for item in inspected.navigation:
                    if not item.href or not item.actionable:
                        continue
                    destination = urljoin(inspected.url, item.href)
                    if urlparse(destination).netloc not in {"", urlparse(inspected.url).netloc}:
                        continue
                    visible_controls_by_route.setdefault(_canonical_route(destination), item)
                # Relationships can be represented by an in-page button
                # rather than a route (common in SPA Settings dashboards).
                # Probe only an explicitly requested, non-mutating semantic
                # control and retain its independently observed state as page
                # knowledge. This is discovery evidence, never a production
                # shortcut or a data-changing action.
                for relationship_control in _relationship_child_controls(inspected, objective_spec):
                    if asyncio.get_running_loop().time() - discovery_started >= budget.max_time_seconds:
                        rejected_routes.append("discovery_time_budget_exhausted")
                        break
                    if len(collected) >= budget.max_pages:
                        break
                    try:
                        visible = await self._visible_semantic_control(page, relationship_control)
                        if visible is None:
                            continue
                        before_text = (await page.locator("body").inner_text())[:8_000]
                        await page.wait_for_timeout(500)
                        try:
                            await visible.click(timeout=5_000)
                        except PlaywrightTimeoutError:
                            await visible.click(timeout=3_000, force=True)
                        await page.wait_for_timeout(650)
                        revealed = await inspect_with_scroll_evidence()
                        changed = revealed.visible_text != before_text
                        if not changed:
                            capability_blockers.append(
                                f"relationship_probe_no_visible_state_change:{relationship_control.name}"
                            )
                            continue
                        revealed = revealed.model_copy(update={
                            "evidence": [
                                *revealed.evidence,
                                f"visible_relationship_control:{relationship_control.name}",
                                f"relationship_context:{relationship_control.source_url or inspected.url}",
                            ]
                        })
                        collected.append(revealed)
                        navigation_probes.append(f"visible_relationship_control:{relationship_control.name}")
                    except PlaywrightError as error:
                        capability_blockers.append(
                            f"relationship_probe_failed:{relationship_control.name}:{str(error)[:120]}"
                        )
                    finally:
                        # Restore a clean parent state before the next bounded
                        # discovery probe. Escape is semantic and harmless;
                        # the page reload is only a read-only fallback when a
                        # component does not expose a dismissible state.
                        try:
                            await page.keyboard.press("Escape")
                            await page.wait_for_timeout(250)
                        except PlaywrightError:
                            pass
                # A relationship context often lives one visible navigation
                # level below its generic entry surface (for example, an
                # observed Settings item followed by a named configuration).
                # Expand only those explicitly relevant child controls and do
                # it immediately while the verified visible parent remains
                # active, so discovery prefers semantic navigation over a
                # direct URL fallback.
                supporting = _relationship_supporting_routes(inspected, objective_spec)
                queued = {
                    _canonical_route(candidate)
                    for candidate in routes_to_inspect[route_index + 1 :]
                }
                additions = [
                    candidate for candidate in supporting
                    if _canonical_route(candidate) not in seen_inspection_routes
                    and _canonical_route(candidate) not in queued
                ]
                if additions:
                    if objective_spec.demo_type == "full_walkthrough":
                        # A complete walkthrough must establish every visible
                        # primary section before spending the bounded page
                        # budget on deep cards/articles. Inserting a child
                        # route immediately after its parent used to evict a
                        # later top-level tab (for example Contact) and made
                        # planning fail despite that tab being visible.
                        routes_to_inspect.extend(additions)
                    else:
                        routes_to_inspect[route_index + 1 : route_index + 1] = additions
                # An explicit configuration/dependency request is complete
                # once the operational feature and a page-local supporting
                # detail state have both been grounded. Continuing through a
                # bounded route list after that point is crawler behavior and
                # only weakens the final candidate flow with unrelated pages.
                if _focused_relationship_evidence_complete(collected, objective_spec):
                    break
        finally:
            pass
        # Discovery owns evidence, not a browser state for later capture.
        # Production always uses a clean context, and viewport selection can
        # explicitly revisit the opening URL if it needs to. Replaying the
        # entry navigation here caused duplicate loads and could discard fully
        # collected knowledge when a provider lease expired during cleanup.
        # The same generic selector (for example ``h1`` or ``a``) is valid on
        # several routes.  De-duplicating without page provenance silently
        # removes later-page landmarks and makes a planner open a tab with no
        # way to explore it.  Preserve each page-local observation.
        seen: set[tuple[str | None, str, str]] = set()
        elements: list[ObservedElement] = []
        for context in collected:
            for item in context.elements:
                key = (item.source_url, item.selector, item.name)
                if key not in seen:
                    seen.add(key)
                    elements.append(item)
        observed_targets = {
            (item.selector, item.name.lower())
            for item in elements
        }
        action_hints = []
        for hint in known_actions or []:
            target = hint.get("target") or {}
            selector, name = target.get("selector"), str(target.get("name", "")).lower()
            if (selector, name) in observed_targets or any(
                selector and selector == observed_selector or name and name == observed_name
                for observed_selector, observed_name in observed_targets
            ):
                action_hints.append(hint)
        pages = [_page_knowledge(context) for context in collected]
        elements = _restore_missing_page_landmarks(elements, pages)
        relationships = _derive_product_relationships(pages, objective_spec)
        objective_words = _tokens(objective)
        features = []
        for page_info in pages:
            terms = _tokens(" ".join([page_info.title, page_info.purpose, *page_info.visible_sections, *page_info.visible_facts[:6]]))
            score = min(1.0, 0.25 + 0.15 * len(terms & objective_words) + (0.35 if objective_spec.demo_type == "full_walkthrough" else 0))
            # Semantic relevance is still grounded in the observed page terms;
            # these synonym bridges prevent an "invite teammate" objective
            # from ranking a generic settings page above an observed Users
            # page merely because neither label repeats the request verbatim.
            if {"invite", "teammate", "member", "team"} & objective_words and {"user", "users", "member", "members"} & terms:
                score = max(score, 0.82)
            if {"report", "export", "csv"} & objective_words and {"report", "reports", "analytics"} & terms:
                score = max(score, 0.82)
            related_urls = list(dict.fromkeys(
                url for relationship in relationships
                for url in (relationship.source_url, relationship.target_url)
                if url and _canonical_route(url) != _canonical_route(page_info.url)
            ))
            features.append(FeatureKnowledge(name=page_info.purpose, purpose=page_info.purpose, entry_urls=[page_info.url], related_urls=related_urls[:12], evidence=[f"page:{page_info.url}", *[f"section:{section}" for section in page_info.visible_sections[:4]]], relevance_score=score))
        ranked_pages = sorted(pages, key=lambda page_info: next((feature.relevance_score for feature in features if feature.name == page_info.purpose), 0), reverse=True)
        operational_pages, supporting_pages = _relationship_page_roles(pages, objective_spec)
        if objective_spec.demo_type == "full_walkthrough":
            flow_page_infos = pages
        elif objective_spec.supporting_relationships and operational_pages:
            # The source configuration is evidence gathered during discovery;
            # the operational feature is what a viewer asked to see.  Never
            # turn this into a Settings/configuration route tour merely
            # because lexical relevance happens to tie.
            flow_page_infos = operational_pages
        else:
            flow_page_infos = ranked_pages[: min(3, len(ranked_pages))]
        flow_pages = [page_info.url for page_info in flow_page_infos]
        flow = CandidateDemoFlow(
            name="evidence-backed walkthrough", page_urls=flow_pages,
            supporting_page_urls=[page_info.url for page_info in supporting_pages],
            rationale=[
                "objective-relevant operational page knowledge",
                "relationship context grounded during exploration",
                "visible sections and controls inspected",
            ] if supporting_pages else ["objective-relevant page knowledge", "visible sections and controls inspected"],
            expected_outcomes=[page_info.purpose for page_info in flow_page_infos[:6]],
            risks=["external links and side effects excluded"],
            estimated_duration_seconds=objective_spec.target_duration_seconds,
            evidence_coverage=[evidence for page_info in ranked_pages for evidence in page_info.evidence_refs[:3]],
            score=0.85 if pages else 0.0,
        )
        candidates = [flow]
        # Retain alternatives as explicit, scored evidence. Planning can then
        # reject a merely navigable page instead of silently broadening a
        # narrow request into a route sweep.
        if objective_spec.demo_type != "full_walkthrough":
            # Relationship support is deliberately not emitted as an isolated
            # production candidate. It may be selected only alongside its
            # operational story surface through ``supporting_page_urls``.
            focused_pages = flow_page_infos if objective_spec.supporting_relationships else ranked_pages[: min(4, len(ranked_pages))]
            for page_info in focused_pages:
                # The opening page already provides context.  A duplicate
                # "focused" candidate contains no feature outcome and can win
                # by being artificially short, which would make scope
                # validation reject the genuinely relevant observed page.
                if _canonical_route(page_info.url) == _canonical_route(primary.url):
                    continue
                feature = next((item for item in features if item.name == page_info.purpose), None)
                relevance = feature.relevance_score if feature else 0.25
                candidates.append(CandidateDemoFlow(
                    name=f"focused: {page_info.purpose}", page_urls=[primary.url, page_info.url],
                    supporting_page_urls=[item.url for item in supporting_pages],
                    rationale=["direct operational relevance", "supporting context verified during discovery"],
                    expected_outcomes=[page_info.purpose], risks=["unrelated primary pages excluded"],
                    estimated_duration_seconds=90,
                    evidence_coverage=page_info.evidence_refs[:8], score=round(relevance, 3),
                ))
        # Preserve a bounded, objective-ranked inventory of every visible
        # same-origin navigation control.  The page collection above may stop
        # after ``max_pages``; losing a visible objective route from
        # ``relevant_routes`` would make the planner fall back to stale cached
        # knowledge (and, for example, miss a Booking page even though the
        # sidebar exposed it).  This is route evidence only; the collection
        # budget still controls which pages are actually inspected.
        visible_inventory: list[str] = []
        entry_origin = urlparse(entry_url)
        for item in primary.navigation:
            if not item.href:
                continue
            candidate = urljoin(entry_url, item.href)
            parsed = urlparse(candidate)
            if parsed.scheme not in {"http", "https", "file"} or parsed.netloc not in {"", entry_origin.netloc}:
                continue
            visible_inventory.append(candidate)
        objective_words = _tokens(objective)
        if objective_spec.demo_type != "full_walkthrough":
            visible_inventory.sort(
                key=lambda route: -_route_objective_score(route, primary.navigation, objective_spec)
            )
        route_inventory = list(dict.fromkeys(visible_inventory))
        observed_routes = list(dict.fromkeys(route for context in collected for route in context.relevant_routes))
        return primary.model_copy(
            update={
                # Keep enough evidence for every inspected page.  A single
                # dense DSA table can legitimately contain more than 120
                # controls; a global first-N truncation used to erase Builds
                # and Progress entirely after they had been successfully
                # explored.  The bounded route/page budget keeps this payload
                # finite, while planner prompts apply their own compact view.
                "elements": elements[:360],
                "navigation": [item for item in elements if item.href][:50],
                "relevant_routes": list(dict.fromkeys([*route_inventory, *observed_routes]))[: budget.max_pages],
                "evidence": [
                    *primary.evidence,
                    f"bounded routes inspected: {len(collected)}",
                    *([f"adaptive full-walkthrough budget: {original_budget.max_pages}->{budget.max_pages}"]
                      if budget.max_pages != original_budget.max_pages else []),
                ],
                "successful_action_hints": action_hints[:30],
                "confidence": min(1.0, primary.confidence + 0.05 * (len(collected) - 1)),
                "objective": objective_spec,
                "page_knowledge": pages,
                "feature_knowledge": features,
                "relationships": relationships,
                "candidate_demo_flows": sorted(candidates, key=lambda candidate: candidate.score, reverse=True),
                "capabilities": [capability.model_dump(mode="json") for capability in capabilities],
                "exploration_actions": [*navigation_probes, *capability_actions],
                "blockers": [*primary.blockers, *capability_blockers],
                "rejected_routes": rejected_routes,
                "effective_discovery_budget": budget,
            }
        )

    async def enrich_with_stagehand(self, page, context: ProductContext, observation) -> ProductContext:
        """Re-ground optional Stagehand suggestions against this Playwright page.

        A Stagehand response is never trusted as execution evidence on its own.
        Invalid, hidden, or ambiguous selectors are discarded before planning.
        """
        additions: list[ObservedElement] = []
        known = {item.selector for item in context.elements}
        for candidate in observation.candidates:
            if not candidate.selector or candidate.selector in known:
                continue
            try:
                locator = page.locator(candidate.selector)
                if await locator.count() != 1 or not await locator.is_visible():
                    continue
                item = await locator.evaluate(
                    """node => ({tag:node.tagName.toLowerCase(), role:node.getAttribute('role'),
                    name:node.getAttribute('aria-label') || node.getAttribute('name') ||
                    node.getAttribute('placeholder') || node.innerText || '', href:node.getAttribute('href'),
                    type:node.getAttribute('type'), required:node.required === true,
                    autocomplete:node.getAttribute('autocomplete'),
                    options:node.tagName.toLowerCase() === 'select' ? Array.from(node.options).map(option => option.value || option.text).filter(Boolean).slice(0, 40) : [], text:node.innerText || null})"""
                )
            except PlaywrightError:
                continue
            name = str(item.get("name") or candidate.description).strip()
            if not name:
                continue
            additions.append(
                ObservedElement(
                    tag=item["tag"], role=item.get("role"), name=name[:300],
                    selector=candidate.selector, href=item.get("href"),
                    element_type=item.get("type"), required=bool(item.get("required")),
                    options=[str(option)[:200] for option in item.get("options", [])],
                    autocomplete=item.get("autocomplete"), text=item.get("text"),
                    source_url=page.url, actionable=True, navigation_scope="unknown",
                )
            )
        # Stagehand's extraction is useful only if it is provably describing
        # text already visible on this exact page.  Do not preserve a model
        # paraphrase as product knowledge: a matching visible phrase is the
        # minimum evidence threshold for a section/control label.
        visible_text = " ".join(context.visible_text.split()).casefold()

        def grounded(phrases: list[str]) -> list[str]:
            result: list[str] = []
            for phrase in phrases:
                normalized = " ".join(phrase.split())
                if len(normalized) >= 3 and normalized.casefold() in visible_text and normalized not in result:
                    result.append(normalized)
            return result

        analysis = getattr(observation, "analysis", None)
        grounded_sections = grounded(analysis.visible_sections) if analysis else []
        grounded_controls = grounded(analysis.meaningful_controls) if analysis else []
        current_route = _canonical_route(page.url)
        page_knowledge = []
        for known_page in context.page_knowledge:
            if _canonical_route(known_page.url) != current_route:
                page_knowledge.append(known_page)
                continue
            page_knowledge.append(known_page.model_copy(update={
                "visible_sections": list(dict.fromkeys([*known_page.visible_sections, *grounded_sections]))[:30],
                "actionable_controls": list(dict.fromkeys([*known_page.actionable_controls, *grounded_controls]))[:40],
                "evidence_refs": [
                    *known_page.evidence_refs,
                    *[f"stagehand-grounded-section:{item}" for item in grounded_sections],
                    *[f"stagehand-grounded-control:{item}" for item in grounded_controls],
                ][:60],
            }))
        if not additions and not grounded_sections and not grounded_controls:
            return context
        return context.model_copy(update={
            # ``context.elements`` already has a bounded discovery budget.
            # Do not reapply a lower Stagehand-specific cap here: it used to
            # remove the synthesized page-local landmarks at the tail of the
            # evidence list, leaving later primary pages unexecutable.
            "elements": [*context.elements, *additions],
            "content_blocks": list(dict.fromkeys([
                *context.content_blocks, *grounded_sections, *grounded_controls,
            ]))[:60],
            "page_knowledge": page_knowledge,
            "evidence": [
                *context.evidence,
                f"stagehand suggestions re-grounded: {len(additions)}",
                f"stagehand semantic labels re-grounded: {len(grounded_sections) + len(grounded_controls)}",
            ],
        })
