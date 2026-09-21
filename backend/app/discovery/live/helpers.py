"""Objective scoring, relationships, and page-knowledge helpers."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urljoin, urlparse

from app.contracts.models import (
    DiscoveryBudget,
    ObjectiveRelationship,
    ObjectiveSpec,
    ObservedElement,
    PageKnowledge,
    ProductContext,
    ProductRelationship,
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
    # Default documents are transport spellings of the owning route, not a
    # second application page. This is especially common when a static/SPA
    # host redirects ``/`` to ``/index.html`` after the first paint.
    if path.casefold().endswith(("/index.html", "/index.htm")):
        path = path.rsplit("/", 1)[0] or "/"
    return parsed._replace(
        scheme=scheme, netloc=parsed.netloc.lower(), path=path, params="", query="", fragment=""
    ).geturl()


def _route_depth(value: str) -> int:
    """Return semantic path depth without counting the leading/trailing slash.

    A mounted SPA such as ``/todomvc/`` is a one-section product even though
    its raw string contains two slash characters.  Counting separators made
    discovery skip these legitimate primary entry routes and inspect only the
    wrapper page.
    """
    return len([part for part in urlparse(value).path.split("/") if part])


def adaptive_exploration_budget(
    budget: DiscoveryBudget, objective: ObjectiveSpec, *, primary_route_count: int
) -> DiscoveryBudget:
    """Expand bounded discovery only when the story needs page-local actions.

    The narrow default is intentionally cheap for simple overviews.  A request
    that explicitly asks the agent to create, fill, connect, draw, or otherwise
    demonstrate a state-changing interaction needs enough *exploration* time to
    open the relevant reversible control and inspect its resulting schema.  We
    expand only for those generic interaction signals; this is not a product
    route or workflow allow-list.
    """
    interaction_terms = {
        "create",
        # Action wording is intent, not the product entity.  Keep these
        # common inflected verbs out of the fallback entity chooser so an
        # objective such as “demonstrate creating an invoice” selects
        # ``invoice`` rather than ``creating`` when no “walkthrough of …”
        # phrase is present.
        "creating",
        "building",
        "drawing",
        "designing",
        "editing",
        "filling",
        "submitting",
        "verifying",
        "opening",
        "using",
        "selecting",
        "showing",
        "making",
        "exploring",
        "navigating",
        "explaining",
        "adding",
        "new",
        "required",
        "details",
        "synthetic",
        "values",
        "add",
        "fill",
        "enter",
        "submit",
        "save",
        "configure",
        "book",
        "schedule",
        "connect",
        "draw",
        "diagram",
        "workflow",
        "upload",
        "drag",
        "drop",
        "invite",
        "send",
        "edit",
        "update",
        "delete",
        "remove",
        "filter",
        "search",
    }
    objective_words = _tokens(
        " ".join(
            [
                objective.raw,
                objective.primary_entity or "",
                *objective.requested_features,
                *objective.must_show,
            ]
        )
    )
    interactive = bool(objective_words & interaction_terms)
    if objective.demo_type != "full_walkthrough" and not interactive:
        return budget
    required_pages = max(1, primary_route_count + 1)  # opening page plus primary controls
    # Interactive feature tours need a route plus a reversible entry-control
    # probe; full tours additionally need every visible primary route.
    page_floor = 6 if interactive else 0
    if interactive and objective.demo_type != "full_walkthrough":
        # A focused workflow must not become a broad route sweep merely
        # because the shell exposes many links.  Keep enough room for the
        # entry page, the requested area, one supporting context page, and a
        # result/detail state; relevance scoring decides which four-to-six
        # routes earn that budget.
        expanded_pages = min(6, max(budget.max_pages, page_floor))
    else:
        expanded_pages = min(12, max(budget.max_pages, required_pages, page_floor))
    action_multiplier = 8 if interactive else 4
    expanded_actions = min(96, max(budget.max_actions, expanded_pages * action_multiplier))
    # Even when the default page count already covers the visible routes,
    # full-tour discovery still needs time for page-local scroll sampling and
    # semantic probes. Previously the early return left the 60-second narrow
    # budget in place and healthy SPAs timed out before planning.
    # A page can require one bounded recovery/reopen plus a scroll sample;
    # budget roughly 75 seconds per selected page so a six-page SPA does not
    # expire halfway through route-local evidence collection.
    time_floor = 240 if interactive else 180
    per_page_seconds = 45 if interactive else 75
    expanded_time = min(
        900, max(budget.max_time_seconds, time_floor, expanded_pages * per_page_seconds)
    )
    if (
        expanded_pages == budget.max_pages
        and expanded_actions == budget.max_actions
        and expanded_time == budget.max_time_seconds
    ):
        return budget
    return budget.model_copy(
        update={
            "max_pages": expanded_pages,
            "max_actions": expanded_actions,
            # Full walkthroughs are explicitly allowed to inspect more than the
            # narrow default.  Keep the cap bounded below the Browserbase lease,
            # but do not force rich documentation/canvas products to time out at
            # the same five-minute ceiling used for a focused feature scan.
            "max_time_seconds": expanded_time,
        }
    )


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
        or re.search(
            r"\b(?:every|all)\s+(?:safe\s+)?(?:primary\s+)?(?:section|page|tab)s?\b", lower
        )
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
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "this",
        "that",
        "its",
        "show",
        "give",
        "explain",
        "demonstrate",
        "demo",
        "walkthrough",
        "tour",
        "full",
        "complete",
        "brief",
        "detailed",
        "concise",
        "overview",
        "evidence",
        "grounded",
        "feature",
        "flow",
        "workflow",
        "application",
        "product",
        "create",
        "produce",
        "record",
        "minute",
        "minutes",
        "second",
        "seconds",
        "context",
        "during",
        "then",
        "establish",
        "relationship",
        "exploration",
        "explore",
        "safe",
        "isolated",
        "actual",
        "experience",
        "only",
        "thorough",
        "production",
        "workspace",
        "visible",
        "state",
        "result",
        "close",
        "viewer",
        "screen",
        "page",
        "pages",
        "content",
        "current",
        "requested",
        "relevant",
        "discovery",
        "list",
        "visibly",
        "verify",
        "one",
        "not",
        "visit",
        "unless",
        "every",
        "each",
        "all",
        "primary",
        "section",
        "sections",
        "tab",
        "tabs",
        "meaningful",
        "prospect",
        "user",
        "users",
        "business",
        "website",
        "web",
        "sales",
        "onboarding",
        "training",
        "changelog",
        "support",
        "portfolio",
        "video",
        # Editorial qualifiers describe how to present the demo, not a
        # product feature. Keeping them out of requested_features prevents a
        # generic request from selecting an arbitrary observed card as its
        # supposed subject.
        "focused",
        "observed",
        "public",
        "polished",
        "presentable",
        "human",
        "like",
        "needed",
        "valid",
        "resolve",
        "resolves",
        "overlay",
        "overlays",
        "select",
        "selects",
        "choose",
        "chooses",
        "available",
        "dependency",
        "dependencies",
        "matters",
        "changed",
    }
    words = [word for word in re.findall(r"[a-z0-9]{3,}", lower) if word not in objective_noise]
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
    walkthrough_entity = (
        " ".join(walkthrough_match.group("entity").split()) if walkthrough_match else None
    )
    if walkthrough_entity is None:
        # Imperative workflow requests commonly omit the words "walkthrough
        # of" (for example, "demonstrate the complete booking workflow").
        # Capture the observed subject before the generic workflow noun.
        workflow_match = re.search(
            r"\b(?:the\s+)?(?P<entity>[a-z][a-z0-9 /&_\-]{1,100}?)\s+workflow\b",
            lower,
        )
        if workflow_match:
            candidate = " ".join(workflow_match.group("entity").split())
            candidate = re.split(
                r"\b(?:through|using|via|with|after|before|then|and)\b",
                candidate,
                maxsplit=1,
            )[0].strip()
            candidate = re.sub(
                r"^(?:(?:demonstrate|explain|show|create|creating|complete|full|actual|visible|requested|relevant|the|a|an|one|isolated|new)\s+)+",
                "",
                candidate,
            )
            candidate = re.sub(r"\s+(?:complete|full|actual|visible)$", "", candidate)
            walkthrough_entity = candidate.strip() or None
    if walkthrough_entity:
        # Keep the requested noun phrase, but remove narrator framing that
        # providers and users commonly place around it.  Without this
        # normalization, phrases such as "the authenticated booking
        # workflow" become the required entity and fail grounding against a
        # page whose observed identity is simply "Bookings".  Domain terms
        # (including a meaningful suffix such as "management" or
        # "workflow") are retained, so existing invoice-workflow semantics
        # remain unchanged.
        walkthrough_entity = re.sub(r"^(?:the|a|an)\s+", "", walkthrough_entity)
        walkthrough_entity = re.sub(
            r"^(?:authenticated|operational|relevant|requested|actual|visible|current|primary|polished|presentable|silent|caption[- ]led|human[- ]like|short|brief|detailed)\s+",
            "",
            walkthrough_entity,
        )
        walkthrough_entity = " ".join(walkthrough_entity.split()) or None
        # A focused objective may use a generic subject ("the observed public
        # product experience") rather than naming a feature.  Do not promote
        # that editorial framing into a required entity that can never be
        # grounded by page evidence.
        if walkthrough_entity:
            entity_words = set(re.findall(r"[a-z0-9]{3,}", walkthrough_entity))
            generic_entity_words = objective_noise | {
                "focused",
                "observed",
                "public",
                "meaningful",
                "safe",
                "information",
                "browsing",
                "resource",
                "resources",
                "detail",
                "most",
                "workflow",
                "experience",
            }
            if entity_words and entity_words <= generic_entity_words:
                walkthrough_entity = None
    # Action-led visual objectives (draw/build/design a diagram, edit a
    # canvas, etc.) describe an operation over an observed surface rather
    # than asking for a literal page/entity label.  The first token after the
    # imperative is frequently an adjective ("simple", "quick", "basic")
    # and must not become a grounding requirement.  Leave entity selection to
    # the observed canvas/tool evidence so any visual editor can be planned
    # without product-specific vocabulary.
    visual_action_objective = bool(
        re.search(r"\b(?:create|build|draw|design|make|edit|sketch)\b", lower)
        and re.search(r"\b(?:diagram|architecture|whiteboard|canvas|drawing|flowchart)\b", lower)
    )
    # A completeness clause such as "walkthrough of every safe primary
    # section" describes breadth, not a product entity. Treating it as a
    # must-show feature makes full-tour planning fail before discovery can
    # inspect the actual navigation inventory.
    if (
        full
        and walkthrough_entity
        and re.match(r"^(?:every|each|all|the whole|the entire)\b", walkthrough_entity)
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
            relationships.append(
                ObjectiveRelationship(source=source, target=target, relation="context_for")
            )
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
        # ``using`` also introduces ordinary execution qualifiers ("using
        # realistic synthetic values when required"), not only a supporting
        # product surface.  Do not turn those prose qualifiers into mandatory
        # relationship pages; retain context phrases that name an observable
        # setup/authentication/configuration surface.
        source_words = _tokens(source)
        qualifier_words = {
            "realistic",
            "synthetic",
            "values",
            "only",
            "when",
            "requires",
            "required",
            "observed",
            "flow",
            "needed",
            "it",
        }
        observable_context_words = {
            "config",
            "configuration",
            "settings",
            "setting",
            "setup",
            "authentication",
            "auth",
            "permissions",
            "permission",
        }
        prose_qualifier = bool(source_words & qualifier_words) and not bool(
            source_words & observable_context_words
        )
        if source != target and not prose_qualifier:
            relationships.append(
                ObjectiveRelationship(source=source, target=target, relation="context_for")
            )
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
            "",
            source,
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
            relationships.append(
                ObjectiveRelationship(source=source, target=target, relation="context_for")
            )
    relationships = list(
        {(item.source, item.target, item.relation): item for item in relationships}.values()
    )
    generic = {
        *objective_noise,
        "config",
        "configuration",
        "settings",
        "setting",
        "every",
        "each",
        "all",
        "safe",
        "primary",
        "section",
        "sections",
        "meaningful",
        "visible",
        "content",
        "tab",
        "tabs",
        # Scope/presentation words are not a requested product entity.  Keep
        # them out of primary_entity so focused objectives remain grounded in
        # observed page evidence rather than their editorial wording.
        "focused",
        "feature",
        "observed",
        "public",
        "product",
        "experience",
        "demo",
        "demonstration",
        "evidence",
        "grounded",
        "workflow",
        "most",
        "information",
        "browsing",
        "resource",
        "resources",
        "detail",
        # Fallback entity selection must ignore action phrasing as well as
        # editorial qualifiers.  This keeps “creating an invoice” grounded
        # on the observed invoice surface instead of the verb “creating”.
        "creating",
        "building",
        "drawing",
        "designing",
        "editing",
        "filling",
        "submitting",
        "verifying",
        "opening",
        "using",
        "selecting",
        "showing",
        "making",
        "exploring",
        "navigating",
        "explaining",
        "adding",
        "new",
        "required",
        "details",
        "synthetic",
        "values",
    }
    primary_entity = (
        None
        if visual_action_objective and not walkthrough_entity
        else walkthrough_entity or next((word for word in requested if word not in generic), None)
    )
    if primary_entity is None and relationships:
        primary_entity = (
            re.sub(r"\s+(?:workflow|flow)$", "", relationships[0].target).strip() or None
        )
    isolated_creation = bool(
        re.search(
            r"\b(?:create|add|submit|save|book|register)\b[^.;]{0,120}\b(?:isolated|synthetic|demo|test)\b",
            lower,
        )
        and not re.search(
            r"\b(?:do\s+not|don't|without)\b[^.;]{0,40}\b(?:create|add|submit|save|book|register)\b",
            lower,
        )
    )
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
        success_criteria=[
            "requested content is visibly established",
            "each selected page is explored before transition",
        ],
        minimum_duration_seconds=110 if full else 60,
        target_duration_seconds=180 if full else 120,
        # A complete walkthrough targets three minutes with a bounded repair
        # envelope. Discovery must not accept an old route-sweep artifact that
        # merely accumulated remote idle time; an overlong capture is returned
        # to planning for selective, evidence-backed coverage.
        maximum_duration_seconds=240 if full else 180,
        safe_action_policy="authorized_side_effects" if isolated_creation else "read_only",
        permitted_mutations=["create_isolated_record"] if isolated_creation else [],
        safe_actions_only=not isolated_creation,
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
            if item.href
            and _canonical_route(urljoin(item.source_url or route, item.href)) == canonical
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
        "management",
        "module",
        "workflow",
        "flow",
        "experience",
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
        label_extras = {term for term in label_terms - primary if not re.fullmatch(r"v\d+", term)}
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
        if source & {"config", "configuration"} and candidate & {
            "setting",
            "settings",
            "config",
            "configuration",
        }:
            # An explicit configuration relationship means this generic entry
            # is a required context bridge, not merely another route label.
            # It must outrank similarly named but unrelated surfaces such as
            # "Invoice templates", whose only relevance is a shared entity
            # noun. The named child is still required and validated after the
            # Settings surface is reached.
            score += 12
    return score


def _relationship_supporting_routes(context: ProductContext, objective: ObjectiveSpec) -> list[str]:
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
        if parsed.scheme not in {"http", "https", "file"} or parsed.netloc not in {
            "",
            origin.netloc,
        }:
            continue
        candidate_terms = _normalized_terms(f"{item.name} {parsed.path}")
        direct_relationship_terms = set().union(
            *(
                _normalized_terms(relationship.source) | _normalized_terms(relationship.target)
                for relationship in objective.supporting_relationships
            )
        )
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
    relationship_terms = (
        set().union(
            *(
                _normalized_terms(relationship.source) | _normalized_terms(relationship.target)
                for relationship in objective.supporting_relationships
            )
        )
        if objective.supporting_relationships
        else set()
    )
    controls: list[tuple[int, ObservedElement]] = []
    for item in context.elements:
        if not item.actionable or item.href or item.tag not in {"button", "input"}:
            continue
        terms = _normalized_terms(f"{item.name} {item.text or ''}")
        if not terms or terms & _UNSAFE_ACTION_WORDS or not (terms & relationship_terms):
            continue
        controls.append((len(terms & relationship_terms), item))
    # Relationship controls are a supporting-context probe, not a second
    # crawler.  Repeated portal/virtualized controls can otherwise make every
    # matching duplicate consume a remote CDP round trip and starve the actual
    # feature page.  Keep the strongest unique semantic controls only.
    unique: list[ObservedElement] = []
    seen: set[tuple[str, str, str]] = set()
    for _score, item in sorted(controls, key=lambda candidate: -candidate[0]):
        key = (item.source_url or "", item.name.casefold(), item.selector)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) >= 4:
            break
    return unique


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
        return _normalized_terms(
            " ".join(
                [
                    context.title,
                    context.visible_text,
                    *context.content_blocks,
                ]
            )
        )

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
        return _normalized_terms(
            " ".join(
                [
                    page.title,
                    page.purpose,
                    urlparse(page.url).path,
                    *page.visible_sections,
                    *page.visible_facts[:12],
                ]
            )
        )

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
            if (
                source
                and source.issubset(page_terms)
                and (bool(source & route_terms) or semantic_probe)
            ):
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
                primary_candidates.append(
                    (
                        len(primary_terms & page_terms) + 2 * len(primary_terms & route_terms),
                        -index,
                        page,
                    )
                )
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
            page
            for page in supporting
            if _canonical_route(page.url)
            not in {_canonical_route(item.url) for item in operational}
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
        return _normalized_terms(
            " ".join(
                [
                    page.title,
                    page.purpose,
                    urlparse(page.url).path,
                    *page.visible_sections,
                    *page.visible_facts[:16],
                ]
            )
        )

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
        source_page = (
            max(source_candidates, key=lambda item: item[0])[1] if source_candidates else None
        )
        target_page = next(
            (
                page
                for _score, page in sorted(target_candidates, key=lambda item: -item[0])
                if not source_page
                or _canonical_route(page.url) != _canonical_route(source_page.url)
            ),
            None,
        )
        evidence = [f"objective_relationship:{requested.source}->{requested.target}"]
        if source_page:
            evidence.append(f"page:{source_page.url}")
        if target_page:
            evidence.append(f"page:{target_page.url}")
        confidence = 0.35 + (0.3 if source_page else 0) + (0.3 if target_page else 0)
        result.append(
            ProductRelationship(
                source=requested.source,
                target=requested.target,
                relation={
                    "context_for": "context_for",
                    "configures": "configures",
                    "depends_on": "depends_on",
                    "proves": "proves",
                }.get(requested.relation, "related_to"),
                source_url=source_page.url if source_page else None,
                target_url=target_page.url if target_page else None,
                evidence_refs=evidence[:20],
                confidence=min(1.0, confidence),
            )
        )
    return result


_REVERSIBLE_ACTION_WORDS = {"new", "create", "add", "start", "open", "schedule", "book"}
_UNSAFE_ACTION_WORDS = {
    "delete",
    "remove",
    "send",
    "email",
    "message",
    "pay",
    "charge",
    "publish",
    "invite",
}


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
    # Canvas/SVG/application regions are the primary content landmark in
    # visual editors and diagram tools; they often have no heading at all.
    # Preserve the observed region as evidence instead of requiring a
    # product-specific adapter or guessing from a route name.
    regions = [
        item.name
        for item in context.elements
        if item.tag in {"canvas", "svg"} or item.role in {"application", "toolbar"}
    ][:8]
    headings = list(dict.fromkeys([*headings, *regions]))[:20]
    visible_text_facts = [
        " ".join(fragment.split())[:320]
        for fragment in re.split(r"(?:\r?\n|(?<=[.!?])\s+)", context.visible_text)
        if 5 <= len(fragment.split()) <= 42
    ]
    # Narrative blocks precede short control text. Otherwise a dense navigation
    # or link list can consume the fact budget before a card's contribution
    # evidence is available to the editorial writer.
    facts = list(
        dict.fromkeys(
            [
                *context.content_blocks,
                *visible_text_facts,
                *[
                    " ".join((item.text or "").split())[:320]
                    for item in context.elements
                    if item.text and len(item.text.strip()) > 20
                ],
            ]
        )
    )[:30]
    all_controls = [
        item.name
        for item in context.elements
        if item.actionable and item.tag in {"a", "button", "input", "select"}
    ]
    controls = list(all_controls[:30])
    # Accessibility snapshots often place the most relevant nested settings
    # controls after a long global navigation list. Preserve the bounded
    # default, but retain observed controls that overlap the request's
    # explicit feature/configuration vocabulary so relationship planning can
    # prove the correct context page without product-specific labels.
    objective_terms = (
        _tokens(" ".join(getattr(context.objective, "requested_features", []) or []))
        if context.objective is not None
        else set()
    )
    for name in all_controls[30:]:
        if objective_terms & _tokens(name) and name not in controls:
            controls.append(name)
    fingerprint = hashlib.sha256(
        (context.url + context.title + context.visible_text[:2000]).encode()
    ).hexdigest()[:20]
    screenshot = next(
        (
            item.removeprefix("screenshot:")
            for item in context.evidence
            if item.startswith("screenshot:")
        ),
        None,
    )
    relationship_evidence = [
        item
        for item in context.evidence
        if item.startswith(("visible_relationship_control:", "relationship_context:"))
    ][:8]
    dom_evidence = [
        f"dom:{context.url}:visible-text",
        f"dom:{context.url}:interactive-inventory",
    ]
    accessibility_evidence = [
        f"accessibility:{context.url}:named-controls",
        f"accessibility:{context.url}:roles-and-states",
    ]
    geometry_evidence = [
        f"geometry:{context.url}:{item.name}"
        for item in context.elements
        if item.tag in {"canvas", "svg", "iframe"} or item.shadow_host
    ][:16]
    return PageKnowledge(
        url=context.url,
        title=context.title,
        purpose=(headings[0] if headings else context.title),
        visible_sections=headings,
        scroll_landmarks=headings,
        actionable_controls=controls,
        visible_facts=facts,
        loading_behavior=["network-idle or bounded hydration wait"],
        evidence_refs=[
            f"page:{context.url}",
            *[f"section:{heading}" for heading in headings[:12]],
            *relationship_evidence,
            *dom_evidence,
            *accessibility_evidence,
            *geometry_evidence,
        ],
        screenshot_evidence=screenshot,
        dom_evidence_refs=dom_evidence,
        accessibility_evidence_refs=accessibility_evidence,
        geometry_evidence_refs=geometry_evidence,
        fingerprint=fingerprint,
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


def _objective_nav_controls(
    context: ProductContext, objective: ObjectiveSpec, *, limit: int = 4
) -> list[ObservedElement]:
    """Rank visible href-less shell controls that name the requested entity.

    Many CRM shells expose primary destinations as clickable labels without an
    ``href``. When the standard anchor inventory is empty, these controls are
    the only same-origin evidence that can open the requested workflow area.
    """
    wanted = _tokens(
        " ".join(
            [
                objective.primary_entity or "",
                *objective.requested_features,
                *objective.must_show,
                objective.raw,
            ]
        )
    ) - {
        "create",
        "creating",
        "isolated",
        "synthetic",
        "verify",
        "verified",
        "using",
        "details",
        "fields",
        "one",
        "and",
        "the",
        "with",
    }
    if not wanted:
        return []

    def _expand(words: set[str]) -> set[str]:
        expanded = set(words)
        for word in words:
            if word.endswith("s") and len(word) > 3:
                expanded.add(word[:-1])
            else:
                expanded.add(f"{word}s")
        return expanded

    wanted = _expand(wanted)
    ranked: list[tuple[int, ObservedElement]] = []
    for item in context.elements:
        if item.href or not item.actionable:
            continue
        if item.tag in {"input", "textarea", "select", "iframe", "canvas", "svg", "tr", "td"}:
            continue
        words = _expand(_tokens(f"{item.name} {item.text or ''}"))
        if words & _UNSAFE_ACTION_WORDS:
            continue
        matched = words & wanted
        if not matched:
            continue
        scope_bonus = 4 if item.navigation_scope == "primary" else 0
        ranked.append((len(matched) * 10 + scope_bonus + len(item.name) * -0.01, item))
    ranked.sort(key=lambda entry: entry[0], reverse=True)
    selected: list[ObservedElement] = []
    seen: set[str] = set()
    for _, item in ranked:
        key = item.name.casefold()
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def _bounded_page_navigation(
    elements: list[ObservedElement], *, per_page: int = 24, maximum: int = 240
) -> list[ObservedElement]:
    """Retain route controls with page provenance instead of global first-N slicing.

    A discovery snapshot can contain several inspected pages.  Taking the
    first ``N`` links from the flattened DOM makes a dense opening page (or a
    footer) evict navigation controls from later pages.  Planning then cannot
    prove that a visible same-origin control exists and incorrectly emits a
    direct ``Navigate`` operation.  Keep a small, deterministic quota for each
    observed source page, while preserving the original discovery order within
    that page.  This is a bounded evidence policy, not a product route rule.
    """
    grouped: dict[str, list[ObservedElement]] = {}
    order: list[str] = []
    for item in elements:
        if not item.href:
            continue
        source = item.source_url or ""
        if source not in grouped:
            grouped[source] = []
            order.append(source)
        bucket = grouped[source]
        if len(bucket) < per_page:
            bucket.append(item)
    result: list[ObservedElement] = []
    # Round-robin keeps the global bound useful even when one source page has
    # many links, and guarantees that every page contributes route evidence.
    for offset in range(per_page):
        for source in order:
            bucket = grouped[source]
            if offset < len(bucket):
                result.append(bucket[offset])
                if len(result) >= maximum:
                    return result
    return result

__all__ = [
    "_REVERSIBLE_ACTION_WORDS",
    "_UNSAFE_ACTION_WORDS",
    "_bounded_page_navigation",
    "_canonical_route",
    "_capability_target",
    "_classify",
    "_derive_product_relationships",
    "_focused_relationship_evidence_complete",
    "_normalized_terms",
    "_objective_nav_controls",
    "_objective_spec",
    "_page_knowledge",
    "_relationship_child_controls",
    "_relationship_page_roles",
    "_relationship_supporting_routes",
    "_restore_missing_page_landmarks",
    "_route_depth",
    "_route_objective_score",
    "_route_score",
    "_tokens",
    "adaptive_exploration_budget",
]
