"""Prompt construction, navigation compile, and route circuit."""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse, urlsplit

from app.contracts.models import (
    ObservedElement,
    OperationKind,
    Postcondition,
    ProductContext,
    SemanticOperation,
    Target,
    WorkflowProposal,
)
from app.observability.logging import redact_prompt_text
from app.planning.production.shared import *


class RouteCompileMixin:
    @staticmethod
    def _prompt(objective: str, context: ProductContext, allow_side_effects: bool) -> str:
        evidence = {
            "url": context.url,
            "title": context.title,
            "application_type": context.application_type,
            "authentication_state": context.authentication_state,
            "routes": context.relevant_routes,
            "elements": [item.model_dump() for item in context.elements[:40]],
            "reversible_capabilities": context.capabilities[:12],
            "historical_successful_actions": context.successful_action_hints[:12],
        }
        return (
            "Create a concise but human-paced semantic product-demo workflow. Return only the supplied schema. "
            "Use only targets present in the observed evidence, never coordinates, credentials, secrets, "
            "or a historical target that is absent from the current observed evidence. Historical successful "
            "actions are advisory only; current DOM evidence remains authoritative. "
            "or invented controls. A discovered reversible capability proves a form can be opened, not that it can be "
            "submitted; do not plan a submit unless an explicit outcome postcondition is grounded. Every actionable operation must have at least one postcondition; only "
            "ScrollTo and ReadValue may omit one. "
            f"External side effects are {'allowed' if allow_side_effects else 'not allowed'}; "
            "when disallowed, do not submit, toggle, check, uncheck, delete, or publish. "
            "The JSON object MUST have exactly these top-level fields: narrative_goal (string), "
            "selected_workflow (string), steps (array of SemanticOperation objects), expected_outcomes "
            "(string array), important_elements (string array), excluded_areas (string array), risk_flags "
            "(string array). Each step uses kind, intent, target/value, and postconditions. A target is an "
            'object like {"name":"Invite by email","selector":"[data-testid=\\"invite-email\\"]"}; '
            'a postcondition is an object like {"kind":"value","expected":"person@example.test",'
            '"target":{"name":"Invite by email","selector":"[data-testid=\\"invite-email\\"]"}}. '
            "Never use strings in target or postconditions. Target must use "
            "an observed selector or exact observed name. Valid kinds include Navigate, Click, FillText, "
            "FillEmail, SelectOption, Submit, OpenNavigationItem, ScrollTo, VerifyState, Hover, KeyPress, "
            "PointerSequence, and Drag. PointerSequence values must contain observed points or a relative path "
            "over an observed canvas/SVG/application surface; Drag values must contain an observed semantic "
            "destination and optional duration. These are universal browser gestures, never product-specific "
            "record or graph actions. For a visual-editor objective, plan a causal sequence rather than one "
            "decorative stroke: choose an observed tool, place multiple meaningful elements, label them when a "
            "text tool is observed, connect related elements, and verify the changed surface. Each pointer step "
            "must include evidence_refs and relative_points or points grounded in the current surface; never "
            "return an incomplete gesture. evidence_refs is an array of plain string evidence IDs only "
            "(for example geometry:https://example.test/:canvas workspace); never put a target object, "
            "selector object, or other JSON object inside evidence_refs. "
            "Postcondition kind must be exactly one of url, visible, value, test_state, text; never use state. "
            "When the workflow uses a discovered route, begin with a Navigate step to that exact observed route "
            "before referring to controls found there. "
            "Choose a coherent viewer journey, not arbitrary clickable coverage. For a content "
            "collection, first ScrollTo the collection and show several observed items before optionally opening "
            "one representative item. Give each ScrollTo an intent explaining what the viewer is about to see. "
            "Avoid making one project, footer, or contact CTA the entire story when a richer overview is observed. "
            "Editorial few-shot guidance: acceptable narration intent is 'Explain the Projects section: it lists "
            "the visible systems and what each does, then show one representative card'; unacceptable is "
            "'Open Projects' or merely repeating a route label. For a career page, acceptable is to summarize "
            "the observed role and contribution; unacceptable is naming only the company. Keep every claim tied "
            "to the supplied evidence and keep captions concise enough to read while the page remains visible. "
            "Never use a wrapper field named workflow. "
            f"Objective: {redact_prompt_text(objective)}\nObserved evidence: "
            f"{redact_prompt_text(json.dumps(evidence, separators=(',', ':')))}"
        )

    @staticmethod
    def _compile_navigation(
        proposal: WorkflowProposal, context: ProductContext
    ) -> WorkflowProposal:
        """Make page transitions deterministic from discovery provenance, never model guesswork."""
        source_by_selector = {item.selector: item.source_url for item in context.elements}
        steps = list(proposal.steps)
        active_page = _canonical_url(context.url)
        for index, operation in enumerate(steps):
            if operation.kind in {
                OperationKind.NAVIGATE,
                OperationKind.OPEN_NAVIGATION_ITEM,
            }:
                # A coverage compiler already provides an exact observed
                # destination. Never rewrite it using the next scene's source
                # page; doing so corrupts a tab tour into repeated routes.
                destination = next(
                    (
                        str(condition.expected)
                        for condition in operation.postconditions
                        if condition.kind == "url" and condition.expected
                    ),
                    str(operation.value or ""),
                )
                current = urljoin(context.url, destination)
                if not destination:
                    # An observed navigation control without a URL is an
                    # in-page menu/action, not a route transition. Let the
                    # normal target grounding and state verification handle
                    # it instead of guessing a destination.
                    continue
                # The recorder has already opened the canonical request URL
                # before execution. Keep the explicit first Navigate in the
                # plan so the production executor can remove that no-op
                # opening operation; converting it into a click on a logo or
                # home link would refresh the page and reintroduce the blank/
                # duplicated opening seen in cloud recordings.
                if (
                    operation.kind is OperationKind.NAVIGATE
                    and index == 0
                    and _canonical_url(current) == _canonical_url(context.url)
                ):
                    active_page = _canonical_url(current)
                    continue
                visible_navigation = next(
                    (
                        item
                        for item in context.navigation
                        if item.href
                        and _canonical_url(item.source_url or context.url) == active_page
                        and not urlsplit(item.href).fragment
                        and (
                            _canonical_url(urljoin(item.source_url or context.url, item.href))
                            == _canonical_url(current)
                            # Query parameters frequently represent a default
                            # filter or SPA state.  A visible control exposing
                            # the same route path is still the authoritative
                            # transition; do not let a query-bearing page be
                            # rewritten through a selector-only lookup.
                            or _route_key(urljoin(item.source_url or context.url, item.href))
                            == _route_key(current)
                        )
                    ),
                    None,
                )
                if visible_navigation is None:
                    # A stale model URL can make route matching impossible;
                    # recover only when the operation's semantic intent
                    # identifies one observed control on the active page.
                    # This remains evidence-grounded and avoids choosing an
                    # arbitrary link in a dense navigation bar.
                    intent_words = _semantic_words(operation.intent) - {
                        "open",
                        "show",
                        "view",
                        "navigate",
                        "go",
                        "to",
                        "the",
                    }
                    semantic_matches = [
                        item
                        for item in context.navigation
                        if item.href
                        and _canonical_url(item.source_url or context.url) == active_page
                        and not urlsplit(item.href).fragment
                        and intent_words
                        and len(intent_words & _semantic_words(item.name))
                        >= max(1, min(2, len(intent_words)))
                    ]
                    if len(semantic_matches) == 1:
                        visible_navigation = semantic_matches[0]
                if visible_navigation is not None:
                    # The captured same-origin control is authoritative. A
                    # model may normalize a route, omit a SPA prefix, or
                    # carry a stale destination from another page; retaining
                    # that guess would fail validation even though the
                    # visible link is safe and fully grounded. Preserve the
                    # observed query/hash-free destination for execution and
                    # postcondition verification.
                    observed_destination = urljoin(
                        visible_navigation.source_url or context.url,
                        visible_navigation.href,
                    )
                    # Preserve an intentional query/hash state when the
                    # observed control points at the same route.  For a
                    # genuinely contradictory path, the visible href wins.
                    authoritative_destination = (
                        current
                        if _route_key(current) == _route_key(observed_destination)
                        else observed_destination
                    )
                    steps[index] = operation.model_copy(
                        update={
                            "kind": OperationKind.OPEN_NAVIGATION_ITEM,
                            "intent": f"Open the visible {visible_navigation.name} navigation item",
                            "target": Target(
                                name=visible_navigation.name,
                                selector=visible_navigation.selector,
                                text=visible_navigation.name,
                                source_url=visible_navigation.source_url,
                            ),
                            "value": None,
                            "postconditions": [
                                Postcondition(kind="url", expected=authoritative_destination)
                            ],
                        }
                    )
                    active_page = _canonical_url(authoritative_destination)
                    continue
                if _canonical_url(current) in {
                    _canonical_url(value) for value in (context.url, *context.relevant_routes)
                } or _route_key(current) in {
                    _route_key(value) for value in (context.url, *context.relevant_routes)
                }:
                    # The model/fallback already supplied an observed
                    # destination.  Keep it intact; never infer a new value
                    # from the next target's (often non-unique) selector.
                    active_page = _canonical_url(current)
                    continue
                active_page = _canonical_url(current)
                continue
            if operation.target is not None and operation.target.selector:
                # LLMs may return a card's accessible name with incidental
                # index/progress lines (for example ``3\nWeek 3\n0%``).
                # Re-ground it to the canonical observed element before
                # validation/execution so duplicate SPA selectors remain
                # page-local and the executor receives stable semantics.
                target = operation.target
                # Select by semantic identity before selector identity.  A
                # bare tag selector (for example ``a`` or ``button``) is
                # common in imported discovery snapshots and is not unique;
                # choosing its first occurrence can silently turn a visible
                # feature link into a footer policy link.  Prefer the
                # observed name/text within the source page, then fall back to
                # the selector only when it is actually specific.
                target_name = " ".join(target.name.split()).casefold()
                target_source = target.source_url
                observed = next(
                    (
                        item
                        for item in context.elements
                        if target.selector
                        and item.selector == target.selector
                        and (not target_source or item.source_url == target_source)
                        and " ".join(item.name.split()).casefold() == target_name
                    ),
                    None,
                )
                if observed is None:
                    observed = next(
                        (
                            item
                            for item in context.elements
                            if target.selector
                            and item.selector == target.selector
                            and (not target_source or item.source_url == target_source)
                        ),
                        None,
                    )
                if observed is None:
                    normalized = target_name
                    observed = next(
                        (
                            item
                            for item in context.elements
                            if " ".join(item.name.split()).lower() == normalized
                            and (not target.source_url or item.source_url == target.source_url)
                        ),
                        None,
                    )
                if observed is not None:
                    canonical_selector = observed.selector
                    if observed.href and observed.tag in {"a", "button"}:
                        safe_href = observed.href.replace("'", "\\'")
                        canonical_selector = f"{observed.tag}[href='{safe_href}']"
                    steps[index] = operation.model_copy(
                        update={
                            "target": target.model_copy(
                                update={
                                    "name": observed.name,
                                    "text": observed.text or observed.name,
                                    "selector": canonical_selector,
                                    "source_url": observed.source_url,
                                    "role": observed.role,
                                }
                            )
                        }
                    )
                    operation = steps[index]
            # The target carries page provenance after semantic re-grounding.
            # Do not recover it from a selector-only map: generic selectors
            # such as ``a``/``button`` occur on many pages and the last map
            # entry can point at an unrelated footer or policy link.
            source = (
                operation.target.source_url
                if operation.target and operation.target.source_url
                else (
                    source_by_selector.get(operation.target.selector or "")
                    if operation.target
                    else None
                )
            )
            # Incremental callers may submit a page-local reading target while
            # the browser is already on that page, without capturing its
            # navigation link.  Do not invent a leading route transition in
            # that compatibility mode; the target's source provenance remains
            # intact for execution and narration.
            if (
                source
                and source != context.url
                and operation.kind is OperationKind.SCROLL_TO
                and not context.navigation
            ):
                continue
            if source and source != context.url:
                already_opened = any(
                    candidate.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}
                    and (
                        (
                            candidate.kind is OperationKind.NAVIGATE
                            and urljoin(context.url, str(candidate.value or "")).rstrip("/")
                            == source.rstrip("/")
                        )
                        or any(
                            condition.kind == "url"
                            and urljoin(context.url, str(condition.expected)).rstrip("/")
                            == source.rstrip("/")
                            for condition in candidate.postconditions
                        )
                    )
                    for candidate in steps[:index]
                )
                if already_opened:
                    if operation.kind is OperationKind.OPEN_NAVIGATION_ITEM:
                        destination = next(
                            (
                                str(condition.expected)
                                for condition in operation.postconditions
                                if condition.kind == "url"
                            ),
                            None,
                        )
                        if destination:
                            active_page = _canonical_url(urljoin(context.url, destination))
                    continue
                navigate = SemanticOperation(
                    kind=OperationKind.NAVIGATE,
                    intent=f"Open observed workflow page: {source}",
                    value=source,
                    postconditions=[Postcondition(kind="url", expected=source)],
                )
                steps.insert(index, navigate)
                break
            if operation.kind is OperationKind.OPEN_NAVIGATION_ITEM:
                destination = next(
                    (
                        str(condition.expected)
                        for condition in operation.postconditions
                        if condition.kind == "url"
                    ),
                    None,
                )
                if destination:
                    active_page = _canonical_url(urljoin(context.url, destination))
        # A model/fallback proposal can contain both the visible navigation
        # operation and an equivalent direct Navigate for the same destination
        # (common when a page-local target is compiled after a navigation
        # control). Collapse only adjacent equivalent transitions; a later
        # revisit separated by real work remains visible and is judged by
        # journey QA rather than silently erased.
        deduped: list[SemanticOperation] = []
        for operation in steps:
            if operation.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}:
                destination = next(
                    (
                        str(condition.expected)
                        for condition in operation.postconditions
                        if condition.kind == "url"
                    ),
                    str(operation.value or ""),
                )
                canonical = _canonical_url(urljoin(context.url, destination)) if destination else ""
                if (
                    deduped
                    and deduped[-1].kind
                    in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}
                    and canonical
                    and _canonical_url(
                        urljoin(
                            context.url,
                            next(
                                (
                                    str(condition.expected)
                                    for condition in deduped[-1].postconditions
                                    if condition.kind == "url"
                                ),
                                str(deduped[-1].value or ""),
                            ),
                        )
                    )
                    == canonical
                ):
                    continue
            deduped.append(operation)
        return proposal.model_copy(update={"steps": deduped})

    @staticmethod
    def _route_tour(objective: str, context: ProductContext) -> WorkflowProposal:
        """Removed: route tours are not a valid recovery strategy.

        Kept as a compatibility symbol for callers importing the old private
        helper, but deliberately fails instead of producing a crawler-like
        workflow. Recovery must use a scored, page-grounded candidate flow.
        """
        raise PlanningValidationError(
            "generic route tours are disabled; use an evidence-grounded candidate flow"
        )

        # Legacy implementation intentionally unreachable; retained below only
        # to keep source-level migrations readable while older integrations are
        # upgraded. No execution path can invoke it.
        # Direct-route fallback is permitted only for primary destinations
        # observed from the opening page. Do not fall back to every discovered
        # route: that turns a failed plan into a deep-link crawler.
        routes = [context.url]
        visible_links = [*context.navigation, *[item for item in context.elements if item.href]]
        for item in visible_links:
            if not item.href or (item.source_url or context.url).rstrip("/") != context.url.rstrip(
                "/"
            ):
                continue
            route = urljoin(context.url, item.href)
            if route not in routes:
                routes.append(route)
        routes = routes[:8]
        objective_words = set(re.findall(r"[a-z0-9]{3,}", objective.lower()))
        # A rejected model plan should not collapse a rich product screen into
        # a generic route tour. Prefer a few grounded, read-only links whose
        # visible labels/hrefs match the objective. The initial page remains
        # visible as the opening beat, so it can introduce a Today/dashboard
        # view without inventing a redundant click.
        candidates: list[tuple[int, ObservedElement, str]] = []
        opening_candidates: list[tuple[int, ObservedElement]] = []
        seen_destinations: set[str] = set()
        # A fallback must be more conservative than model output: only use
        # visible navigation discovered on the current page.  `elements`
        # contains page-local cards gathered during multi-page exploration;
        # selecting one of those deep links without first opening its owning
        # section violates the validated navigation contract.
        for item in visible_links:
            if (item.source_url or context.url).rstrip("/") != context.url.rstrip("/"):
                continue
            if not item.href:
                continue
            destination = urljoin(item.source_url or context.url, item.href)
            if urlparse(destination).netloc not in {"", urlparse(context.url).netloc}:
                continue
            words = set(
                re.findall(r"[a-z0-9]{3,}", f"{item.name} {item.text or ''} {item.href}".lower())
            )
            score = len(objective_words & words)
            # A product's current dashboard/Tabs are a meaningful part of a
            # walkthrough even when their href resolves to the current URL.
            # Record the visible state rather than issuing a redundant click.
            if destination == context.url:
                if score:
                    opening_candidates.append((score, item))
                continue
            if destination in seen_destinations:
                continue
            if score:
                candidates.append((score, item, destination))
                seen_destinations.add(destination)
        # The opening frame is established by navigation itself.  Do not make a
        # responsive-only navigation label (often present in cloud discovery but
        # hidden at the production viewport) a critical action requirement.
        # This produces a deterministic opening beat from the actual route.
        opening_steps = [
            SemanticOperation(
                kind=OperationKind.NAVIGATE,
                intent="Open the product's starting page",
                value=context.url,
                postconditions=[Postcondition(kind="url", expected=context.url)],
            )
        ]
        selected: list[tuple[int, ObservedElement, str]] = []
        selected_groups: set[str] = set()
        for candidate in sorted(candidates, key=lambda item: (-item[0], item[1].name)):
            path = urlparse(candidate[2]).path.rstrip("/")
            # Repeated entity links (weeks, projects, articles) are a single
            # representative story choice, not separate primary sections.
            parts = [part for part in path.split("/") if part]
            group = "/" + parts[0] if parts else "/"
            if group in selected_groups:
                continue
            selected.append(candidate)
            selected_groups.add(group)
            if len(selected) == 2:
                break
        targeted_steps = [
            SemanticOperation(
                kind=OperationKind.CLICK,
                intent=(
                    item.name.splitlines()[0][:80]
                    if item.name.lower().startswith("open ")
                    else f"Open {item.name.splitlines()[0][:80]}"
                ),
                # Preserve the page that supplied this control. Selectors are
                # frequently reused by SPAs (e.g. repeated week cards), so
                # validation must not re-ground the target against a later
                # page with the same selector.
                target=Target(name=item.name, selector=item.selector, source_url=item.source_url),
                postconditions=[Postcondition(kind="url", expected=destination)],
            )
            for _, item, destination in selected
        ]
        steps = opening_steps + (
            targeted_steps
            or [
                SemanticOperation(
                    kind=OperationKind.NAVIGATE,
                    intent=f"Open {route.rsplit('/', 1)[-1] or 'dashboard'}",
                    value=route,
                    postconditions=[Postcondition(kind="url", expected=route)],
                )
                for route in routes[1:]
            ]
        )
        if not steps:
            raise PlanningValidationError("No observed route is available for a safe demo")
        return WorkflowProposal(
            narrative_goal=objective,
            selected_workflow="Observed product route tour",
            steps=steps,
            # Coverage must be tied to concrete observed targets. A generic
            # promise such as “Observed product sections are shown” cannot be
            # proven from a trace and causes valid conservative fallbacks to
            # fail delivery QA.
            expected_outcomes=[item.name for _, item, _ in selected] or [context.title],
            important_elements=[item.name for _, item, _ in selected] or [context.title],
            excluded_areas=["unobserved controls", "external links", "data-changing actions"],
            risk_flags=["Model plan contradicted observed DOM; using safe route tour"],
        )

__all__ = [
    "RouteCompileMixin",
]
