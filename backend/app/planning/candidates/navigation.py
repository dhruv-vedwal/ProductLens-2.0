"""Navigation control selection and scope validation."""

from __future__ import annotations

import re
from hashlib import sha256
from urllib.parse import urljoin, urlsplit

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
from app.urls import canonical_product_url

from app.planning.candidates.scoring import *  # noqa: F403

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

__all__ = [
    "navigation_control_for_transition",
    "_navigation_control",
    "_is_control_chrome",
    "_has_descriptive_landmark_evidence",
    "_is_operational_workspace",
    "_page_form_controls",
    "_page_landmarks",
    "_page_story_subject",
    "_configuration_landmark",
    "_collapse_repeated_series",
    "_prioritize_story_landmarks",
    "_fact_for_landmark",
    "_phase_intent",
    "validate_flow_scope",
]
