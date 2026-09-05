"""Structured LLM planning with ProductLens-owned evidence and safety validation."""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from pydantic import ValidationError

from productlens.contracts.models import (
    DemoPlan,
    ObservedElement,
    OperationKind,
    Postcondition,
    ProductContext,
    SemanticOperation,
    Target,
    WorkflowProposal,
    WorkflowStep,
)
from productlens.planning.candidates import (
    build_page_complete_proposal,
    select_candidate_flow,
    validate_flow_scope,
)
from productlens.planning.side_effects import (
    SideEffectPolicyError,
    authorize_operation,
    side_effect_decision,
)
from productlens.planning.synthetic import hydrate_operations
from productlens.providers.errors import ProviderError
from productlens.providers.interfaces import LLMProvider


class PlanningValidationError(ValueError):
    pass


SIDE_EFFECTING = {OperationKind.SUBMIT, OperationKind.CHECK, OperationKind.UNCHECK}
NO_POSTCONDITION_REQUIRED = {OperationKind.SCROLL_TO, OperationKind.READ_VALUE}


class ProductionPlanningService:
    def __init__(self, provider: LLMProvider):
        self.provider = provider

    async def plan(
        self,
        *,
        objective: str,
        context: ProductContext,
        allow_external_side_effects: bool = False,
        audience: str = "product prospect",
        target_duration_seconds: int = 120,
        minimum_duration_seconds: int | None = None,
        maximum_duration_seconds: int | None = None,
    ) -> DemoPlan:
        objective_spec = context.objective
        # The public API's normal 120-second default is intentionally neutral.
        # A discovered full-walkthrough objective already carries a stronger
        # editorial contract, so do not silently collapse it back to a generic
        # overview merely because the caller omitted duration fields.
        if (
            objective_spec is not None
            and objective_spec.demo_type == "full_walkthrough"
            and target_duration_seconds == 120
        ):
            target_duration_seconds = objective_spec.target_duration_seconds
        approved_minimum = minimum_duration_seconds or (
            objective_spec.minimum_duration_seconds if objective_spec else max(30, target_duration_seconds - 45)
        )
        approved_maximum = maximum_duration_seconds or (
            objective_spec.maximum_duration_seconds if objective_spec else min(900, max(180, target_duration_seconds + 60))
        )
        if not approved_minimum <= target_duration_seconds <= approved_maximum:
            raise PlanningValidationError("requested duration is outside the approved objective envelope")
        candidate = select_candidate_flow(context, objective)
        objective_lower = objective.lower()
        complete_objective = bool(
            re.search(r"\bfull\b(?:\s+\w+){0,4}\s+walkthrough\b", objective_lower)
        ) or "each tab" in objective_lower or "every safe primary" in objective_lower
        # “Thorough walkthrough” is an explicit depth request even when the
        # user does not say “full”. It must cover primary sections rather than
        # accepting a narrow model-selected route and later stretching it.
        complete_objective = complete_objective or (
            "walkthrough" in objective_lower
            and any(depth in objective_lower for depth in ("thorough", "complete", "entire"))
        )
        # Whole-product requests are not a deterministic navigation tour.
        # They must be compiled from the fresh PageKnowledge selected by a
        # scored candidate, so every transition has local evidence and every
        # page receives establish/explore/explain/demonstrate/verify beats.
        if complete_objective:
            if candidate is None:
                raise PlanningValidationError("no evidence-grounded candidate flow for complete walkthrough")
            try:
                proposal = build_page_complete_proposal(context, candidate)
            except ValueError as error:
                raise PlanningValidationError(str(error)) from error
        else:
            try:
                proposal = await self.provider.structured(
                    self._prompt(objective, context, allow_external_side_effects), WorkflowProposal
                )
            except (ValidationError, ProviderError):
                # A provider outage must not degrade a focused request into a
                # shallow route sweep. Compile the already-scored evidence
                # candidate through the same page-complete contract used by a
                # full walkthrough.
                proposal = self._evidence_fallback(candidate, context)
        if not complete_objective:
            proposal = self._compile_navigation(proposal, context)
            try:
                self._validate(proposal, context, allow_external_side_effects)
                scope_failures = validate_flow_scope(proposal, context=context, candidate=candidate)
                if scope_failures:
                    raise PlanningValidationError(scope_failures[0])
            except PlanningValidationError as error:
                if "not authorized" in str(error):
                    raise
                proposal = self._evidence_fallback(candidate, context)
            # The deterministic fallback is not exempt from the same evidence
            # scope. Reject rather than delivering a broad crawler tour.
            self._validate(proposal, context, allow_external_side_effects)
            scope_failures = validate_flow_scope(proposal, context=context, candidate=candidate)
            if scope_failures:
                raise PlanningValidationError(scope_failures[0])
        else:
            self._validate(proposal, context, allow_external_side_effects)
            scope_failures = validate_flow_scope(proposal, context=context, candidate=candidate)
            if scope_failures:
                raise PlanningValidationError(scope_failures[0])
        steps = self._ground(proposal, context)
        steps, synthetic_dataset = hydrate_operations(steps, product_key=context.url)
        side_effects = {
            operation.id: side_effect_decision(operation)
            for operation in steps
            if operation.kind in SIDE_EFFECTING
        }
        return DemoPlan(
            objective=objective,
            narrative_goal=proposal.narrative_goal,
            audience=audience,
            target_duration_seconds=target_duration_seconds,
            # The target is not a renderer stretch instruction.  It is the
            # centre of an approved editorial envelope that production QA can
            # enforce after native-speed capture and directed presentation.
            minimum_duration_seconds=approved_minimum,
            maximum_duration_seconds=approved_maximum,
            selected_workflow=proposal.selected_workflow,
            workflow_steps=[
                WorkflowStep(
                    id=f"step-{index + 1}",
                    intent=operation.intent,
                    operation=operation,
                    page_requirement=operation.target.source_url if operation.target else None,
                    importance="critical" if operation.critical else "supporting",
                    narration_intent=f"Explain why {operation.intent.lower()} matters to the viewer.",
                    visual_intent="Show the target and enough surrounding product context to understand the result.",
                    fallback_strategy="semantic re-ground before dispatch; never replay a dispatched side effect",
                    allowed_retries=1,
                    completion_criteria=[
                        f"{condition.kind}:{condition.expected}"
                        for condition in operation.postconditions
                    ] or ["operation completed and the resulting state is readable"],
                    evidence_refs=[
                        *operation.evidence_refs,
                        *([f"page:{operation.page_url}"] if operation.page_url else []),
                        *[
                        reference
                        for reference in (
                            f"element:{operation.target.name}" if operation.target else None,
                            f"page:{operation.target.source_url}" if operation.target and operation.target.source_url else None,
                        )
                        if reference
                        ],
                    ],
                    page_phase=operation.story_phase,
                )
                for index, operation in enumerate(steps)
            ],
            synthetic_data_plan={
                "source": "planner_and_semantic_fallback",
                "must_not_use_credentials": True,
                "generated_values": synthetic_dataset,
                "side_effect_decisions": side_effects,
                "selected_candidate_flow": candidate.model_dump(mode="json") if candidate else None,
            },
            expected_outcomes=proposal.expected_outcomes,
            important_elements=proposal.important_elements,
            excluded_areas=proposal.excluded_areas,
            viewport_strategy="focus observed semantic targets; preserve readable context",
            risk_flags=[
                *proposal.risk_flags,
                *[f"side_effect:{operation_id}:{decision}" for operation_id, decision in side_effects.items()],
            ],
            stop_conditions=[
                "all critical postconditions pass",
                "unexpected authentication",
                "budget exhausted",
            ],
        )

    @staticmethod
    def _prompt(objective: str, context: ProductContext, allow_side_effects: bool) -> str:
        evidence = {
            "url": context.url,
            "title": context.title,
            "application_type": context.application_type,
            "authentication_state": context.authentication_state,
            "routes": context.relevant_routes,
            "elements": [item.model_dump() for item in context.elements[:40]],
            "historical_successful_actions": context.successful_action_hints[:12],
        }
        return (
            "Create a concise but human-paced semantic product-demo workflow. Return only the supplied schema. "
            "Use only targets present in the observed evidence, never coordinates, credentials, secrets, "
            "or a historical target that is absent from the current observed evidence. Historical successful "
            "actions are advisory only; current DOM evidence remains authoritative. "
            "or invented controls. Every actionable operation must have at least one postcondition; only "
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
            "FillEmail, SelectOption, Submit, OpenNavigationItem, ScrollTo, and VerifyState. "
            "Postcondition kind must be exactly one of url, visible, value, test_state, text; never use state. "
            "When the workflow uses a discovered route, begin with a Navigate step to that exact observed route "
            "before referring to controls found there. "
            "Choose a coherent viewer journey, not arbitrary clickable coverage. For a portfolio or content "
            "collection, first ScrollTo the collection and show several observed items before optionally opening "
            "one representative item. Give each ScrollTo an intent explaining what the viewer is about to see. "
            "Avoid making one project, footer, or contact CTA the entire story when a richer overview is observed. "
            "Editorial few-shot guidance: acceptable narration intent is 'Explain the Projects section: it lists "
            "the visible systems and what each does, then show one representative card'; unacceptable is "
            "'Open Projects' or merely repeating a route label. For a career page, acceptable is to summarize "
            "the observed role and contribution; unacceptable is naming only the company. Keep every claim tied "
            "to the supplied evidence and keep captions concise enough to read while the page remains visible. "
            "Never use a wrapper field named workflow. "
            f"Objective: {objective}\nObserved evidence: {json.dumps(evidence, separators=(',', ':'))}"
        )

    @staticmethod
    def _compile_navigation(
        proposal: WorkflowProposal, context: ProductContext
    ) -> WorkflowProposal:
        """Make page transitions deterministic from discovery provenance, never model guesswork."""
        source_by_selector = {item.selector: item.source_url for item in context.elements}
        steps = list(proposal.steps)
        for index, operation in enumerate(steps):
            if operation.kind is OperationKind.NAVIGATE:
                # A coverage compiler already provides an exact observed
                # destination. Never rewrite it using the next scene's source
                # page; doing so corrupts a tab tour into repeated routes.
                current = urljoin(context.url, str(operation.value or ""))
                visible_navigation = next(
                    (
                        item for item in context.navigation
                        if item.href and urljoin(item.source_url or context.url, item.href).rstrip("/") == current.rstrip("/")
                    ),
                    None,
                )
                if visible_navigation is not None:
                    steps[index] = operation.model_copy(update={
                        "kind": OperationKind.OPEN_NAVIGATION_ITEM,
                        "intent": f"Open the visible {visible_navigation.name} navigation item",
                        "target": Target(name=visible_navigation.name, selector=visible_navigation.selector, text=visible_navigation.name),
                        "value": None,
                        "postconditions": [Postcondition(kind="url", expected=current)],
                    })
                    continue
                if current in {context.url, *context.relevant_routes}:
                    continue
                next_target = next(
                    (item.target for item in steps[index + 1 :] if item.target is not None), None
                )
                source = source_by_selector.get(next_target.selector or "") if next_target else None
                if source and source in context.relevant_routes:
                    steps[index] = operation.model_copy(update={"value": source})
                continue
            if operation.target is not None and operation.target.selector:
                # LLMs may return a card's accessible name with incidental
                # index/progress lines (for example ``3\nWeek 3\n0%``).
                # Re-ground it to the canonical observed element before
                # validation/execution so duplicate SPA selectors remain
                # page-local and the executor receives stable semantics.
                target = operation.target
                observed = next(
                    (item for item in context.elements if target.selector and item.selector == target.selector
                     and (not target.source_url or item.source_url == target.source_url)),
                    None,
                )
                if observed is None:
                    normalized = " ".join(target.name.split()).lower()
                    observed = next(
                        (item for item in context.elements
                         if " ".join(item.name.split()).lower() == normalized
                         and (not target.source_url or item.source_url == target.source_url)),
                        None,
                    )
                if observed is not None:
                    canonical_selector = observed.selector
                    if observed.href and observed.tag in {"a", "button"}:
                        safe_href = observed.href.replace("'", "\\'")
                        canonical_selector = f"{observed.tag}[href='{safe_href}']"
                    steps[index] = operation.model_copy(update={
                        "target": target.model_copy(update={
                            "name": observed.name,
                            "text": observed.text or observed.name,
                            "selector": canonical_selector,
                            "source_url": observed.source_url,
                            "role": observed.role,
                        })
                    })
                    operation = steps[index]
            source = (
                source_by_selector.get(operation.target.selector or "")
                if operation.target
                else None
            )
            if source and source != context.url:
                already_opened = any(
                    candidate.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}
                    and (
                        (candidate.kind is OperationKind.NAVIGATE and urljoin(context.url, str(candidate.value or "")).rstrip("/") == source.rstrip("/"))
                        or any(
                            condition.kind == "url" and urljoin(context.url, str(condition.expected)).rstrip("/") == source.rstrip("/"
                            )
                            for condition in candidate.postconditions
                        )
                    )
                    for candidate in steps[:index]
                )
                if already_opened:
                    continue
                navigate = SemanticOperation(
                    kind=OperationKind.NAVIGATE,
                    intent=f"Open observed workflow page: {source}",
                    value=source,
                    postconditions=[Postcondition(kind="url", expected=source)],
                )
                steps.insert(index, navigate)
                break
        return proposal.model_copy(update={"steps": steps})

    @staticmethod
    def _validate(
        proposal: WorkflowProposal,
        context: ProductContext,
        allow_side_effects: bool,
    ) -> None:
        observed_selectors = {item.selector for item in [*context.elements, *context.navigation]}
        observed_names = {item.name.lower() for item in [*context.elements, *context.navigation]}
        observed_routes = {urljoin(item.source_url or context.url, item.href) for item in context.navigation if item.href}
        source_by_selector = {item.selector: item.source_url for item in context.elements}
        navigated_to: set[str] = {context.url}
        for operation in proposal.steps:
            if operation.kind in SIDE_EFFECTING:
                try:
                    authorize_operation(operation, allow_side_effects)
                except SideEffectPolicyError as error:
                    raise PlanningValidationError(str(error)) from error
            if operation.kind is OperationKind.NAVIGATE:
                destination = urljoin(context.url, str(operation.value))
                if destination not in {context.url, *context.relevant_routes, *observed_routes}:
                    raise PlanningValidationError(
                        "Navigation target is not observed and same-origin"
                    )
            elif operation.target is None:
                raise PlanningValidationError(f"Target is required for {operation.kind}")
            elif (
                operation.target.selector not in observed_selectors
                and operation.target.name.lower() not in observed_names
            ):
                raise PlanningValidationError(
                    f"Target is not grounded in current evidence: {operation.target.name}"
                )
            if operation.kind is OperationKind.NAVIGATE:
                navigated_to.add(urljoin(context.url, str(operation.value)))
            elif operation.kind is OperationKind.OPEN_NAVIGATION_ITEM:
                destination = next((str(condition.expected) for condition in operation.postconditions if condition.kind == "url"), None)
                if destination:
                    navigated_to.add(urljoin(context.url, destination))
            elif operation.target is not None:
                # Prefer provenance carried on the operation target. A
                # selector is not globally unique in SPAs and may otherwise
                # resolve to a later page's repeated card/control.
                source = (
                    operation.target.source_url
                    if operation.target.selector
                    else source_by_selector.get(operation.target.selector or "")
                )
                if source and source not in navigated_to:
                    raise PlanningValidationError(
                        f"Target {operation.target.name} requires an observed navigation to {source}"
                    )
                item = next(
                    (
                        candidate
                        for candidate in context.elements
                        if candidate.name.lower() == operation.target.name.lower()
                    ),
                    next(
                        (candidate for candidate in context.elements if candidate.selector == operation.target.selector),
                        None,
                    ),
                )
                expected_url = next(
                    (
                        str(condition.expected)
                        for condition in operation.postconditions
                        if condition.kind == "url"
                    ),
                    None,
                )
                if item and item.href and expected_url:
                    observed_destination = urljoin(item.source_url or context.url, item.href)
                    matches_expected = (
                        observed_destination.endswith(expected_url.removeprefix("**"))
                        if expected_url.startswith("**/")
                        else urljoin(context.url, expected_url) == observed_destination
                    )
                    if not matches_expected:
                        raise PlanningValidationError(
                            "Navigation postcondition contradicts observed target href"
                        )
            if operation.kind not in NO_POSTCONDITION_REQUIRED and not operation.postconditions:
                raise PlanningValidationError(f"Postcondition required for {operation.kind}")

    @staticmethod
    def _ground(proposal: WorkflowProposal, context: ProductContext) -> list:
        """Replace planner target hints with the exact observed locator evidence."""
        observed_items = [*context.elements, *context.navigation]
        by_selector = {item.selector: item for item in observed_items}
        by_name = {item.name.lower(): item for item in observed_items}
        roles = {"a": "link", "button": "button", "select": "combobox", "textarea": "textbox", "h1": "heading", "h2": "heading", "h3": "heading", "h4": "heading"}
        grounded = []
        for operation in proposal.steps:
            if operation.kind is OperationKind.NAVIGATE:
                grounded.append(
                    operation.model_copy(
                        update={"value": urljoin(context.url, str(operation.value))}
                    )
                )
                continue
            if operation.target is None:
                grounded.append(operation)
                continue
            selector = operation.target.selector or ""
            # Discovery uses generic tag selectors only when a page does not
            # expose a stronger id/test id. Such selectors are not unique, so
            # prefer the observed accessible name for grounding in that case.
            # Page provenance is stronger than a generic selector or name.
            # In multi-page discovery the same label is often present in every
            # global navigation bar; falling back to a name map first can make
            # a planned local heading scroll on an entirely different page.
            # A deterministic tour has already selected a specific local
            # landmark. Preserve that exact selector/name/source triple before
            # applying the broad source/name fallback; otherwise a navbar link
            # that happens to share the heading text wins simply because it is
            # first in the discovery inventory.
            exact_source_match = next(
                (
                    candidate
                    for candidate in observed_items
                    if operation.target.source_url
                    and candidate.source_url == operation.target.source_url
                    and candidate.selector == selector
                    and candidate.name.lower() == operation.target.name.lower()
                ),
                None,
            )
            source_match = exact_source_match or next(
                (
                    candidate
                    for candidate in observed_items
                    if operation.target.source_url
                    and candidate.source_url == operation.target.source_url
                    and candidate.name.lower() == operation.target.name.lower()
                ),
                None,
            )
            item = source_match
            if item is None:
                item = (
                    by_name.get(operation.target.name.lower())
                    if selector in {"a", "button", "input", "select", "textarea"}
                    else by_selector.get(selector)
                ) or by_name.get(operation.target.name.lower())
            if item is None:
                raise PlanningValidationError(
                    f"Target is not grounded in current evidence: {operation.target.name}"
                )
            test_id = None
            if item.selector.startswith('[data-testid="'):
                test_id = item.selector.removeprefix('[data-testid="').removesuffix('"]')
            stable_selector = item.selector if item.selector.startswith(("#", "[")) else None
            if item.href and item.tag == "a":
                stable_selector = f"a[href='{item.href.replace(chr(39), chr(92) + chr(39))}']"
            target = Target(
                name=item.name,
                test_id=test_id,
                role=item.role or roles.get(item.tag),
                selector=stable_selector,
                # Generic anchors/buttons may not expose a usable ARIA role
                # in real product DOMs. Retain exact observed text as a second
                # deterministic locator rather than falling back to geometry.
                text=item.name if item.selector in {"a", "button", "h1", "h2", "h3", "h4"} else (
                    None if item.role or item.tag in roles else item.name
                ),
                source_url=item.source_url,
                confidence_required=operation.target.confidence_required,
            )
            value = operation.value
            if operation.kind in {
                OperationKind.FILL_TEXT,
                OperationKind.FILL_EMAIL,
                OperationKind.FILL_PHONE,
                OperationKind.SELECT_OPTION,
                OperationKind.SELECT_DATE,
            }:
                expected = next(
                    (
                        condition.expected
                        for condition in operation.postconditions
                        if condition.kind == "value" and condition.target is not None
                    ),
                    None,
                )
                if expected is not None:
                    value = expected
            if operation.kind is OperationKind.SELECT_OPTION and value in (None, ""):
                selected = next(
                    (
                        option for option in item.options
                        if option.strip() and option.strip().lower() not in {"select", "select an option", "choose", "choose an option"}
                    ),
                    None,
                )
                if selected is None:
                    raise PlanningValidationError(
                        f"Select option target has no observed selectable value: {item.name}"
                    )
                value = selected
            # Postconditions are executed through the same live grounding
            # adapter as actions.  Reusing the resolved semantic target keeps
            # a generic discovery selector (for example ``a``) from leaking
            # into a later visibility/value assertion.
            has_url_transition = any(
                condition.kind == "url" for condition in operation.postconditions
            )
            postconditions = []
            for condition in operation.postconditions:
                # A route transition is proven by its exact observed URL. Low-cost
                # planners sometimes add a second visible assertion copied from
                # the source page (for example the "Week 1" link after clicking
                # it). That assertion is neither destination-grounded nor needed
                # once the URL has passed, so never replay stale source evidence.
                if (
                    has_url_transition
                    and condition.kind == "visible"
                    and condition.target is not None
                    and condition.target.name.lower() != operation.target.name.lower()
                ):
                    continue
                postconditions.append(
                    condition.model_copy(update={"target": target})
                    if condition.target and condition.target.name.lower() == operation.target.name.lower()
                    else condition
                )
            if operation.kind is OperationKind.SELECT_OPTION and not any(
                condition.kind == "value" for condition in postconditions
            ):
                postconditions.append(Postcondition(kind="value", expected=value, target=target))
            grounded.append(
                operation.model_copy(
                    update={"target": target, "value": value, "postconditions": postconditions}
                )
            )
        return grounded

    @staticmethod
    def _page_exploration_targets(
        context: ProductContext, destination: str, *, limit: int
    ) -> list[ObservedElement]:
        """Return readable, page-local landmarks for a chapter.

        A page visit is only narratively valid once material content is shown.
        This is deliberately DOM-backed rather than route-name driven: headings
        are preferred, then meaningful links/buttons with visible descriptive
        text. It also handles pages whose landmark heading is not exposed by
        selecting the strongest observed local controls as a conservative
        fallback.
        """
        canonical = destination.rstrip("/")
        local = [
            item for item in context.elements
            if (item.source_url or context.url).rstrip("/") == canonical
            and item.name.strip()
        ]
        page = next((item for item in context.page_knowledge if item.url.rstrip("/") == canonical), None)
        landmark_order = {
            " ".join(name.split()).lower(): index
            for index, name in enumerate(page.scroll_landmarks if page else [])
        }
        def priority(candidate: ObservedElement) -> tuple[int, int, int, int]:
            name = " ".join(candidate.name.split()).lower()
            return (
                0 if name in landmark_order else 1,
                landmark_order.get(name, 10_000),
                0 if candidate.tag in {"h1", "h2", "h3", "h4"} else 1,
            )
        excluded = {
            "reason", "message", "submit", "home", "back", "menu", "close",
            # Footer/navigation headings are persistent chrome, not the local
            # chapter content a full walkthrough should spend reading time on.
            "navigation", "connect", "footer",
        }
        navigation_labels = {
            " ".join(item.name.split()).lower()
            for item in context.navigation
        }
        seen: set[str] = set()
        repeated_series: set[str] = set()
        candidates: list[ObservedElement] = []
        for item in sorted(
            local,
            key=priority,
        ):
            label = " ".join(item.name.split())
            key = label.lower()
            if (
                key in seen
                or len(label) < 3
                or len(label) > 120
                # Composite responsive labels are not stable readable targets
                # in the production accessibility tree.
                or "%" in label
                or label.lower() in excluded
                # A page-local chapter must never spend its reading beats on
                # the persistent global navbar.  Those labels are evidence for
                # transition, not the destination's content.
                or (item.tag == "a" and key in navigation_labels)
            ):
                continue
            # Repeated numbered schedule/list entries are usually equivalent
            # examples, not distinct story chapters.  A full walkthrough
            # establishes the parent collection and then shows one
            # representative week/day/module/lesson detail; expanding every
            # visible numbered sibling turns a human demo back into crawler
            # coverage.  This is deliberately semantic and product-agnostic.
            series_key = re.sub(r"\b\d+\b", "#", key)
            is_repeated_series = bool(
                re.search(r"\b(?:week|day|module|lesson|chapter|step)\s+#(?:\s|$)", series_key)
            )
            if is_repeated_series and series_key in repeated_series:
                continue
            seen.add(key)
            if is_repeated_series:
                repeated_series.add(series_key)
            candidates.append(item)

        # Repeated h3 cards directly beneath an h2 usually form a meaningful
        # collection (projects, case studies, releases, articles). A route
        # sweep previously consumed this area with whichever utility headings
        # appeared first. Establish the page, then include the largest visible
        # collection before filling remaining slots in document order.
        # Some modern sites use h4 labels for the cards that actually contain
        # the demonstrable system/module content.  Treat them as landmarks as
        # well; otherwise a page with one h1 and several rich h4 cards is
        # falsely considered explored after only its title is shown.
        heading_candidates = [item for item in candidates if item.tag in {"h1", "h2", "h3", "h4"}]
        # Inputs and buttons can be workflow targets, but are not reading
        # landmarks. Prefer semantic page headings whenever they are present.
        if heading_candidates:
            candidates = heading_candidates
        collections: list[tuple[ObservedElement, list[ObservedElement]]] = []
        for index, item in enumerate(heading_candidates):
            if item.tag != "h2":
                continue
            members: list[ObservedElement] = []
            for following in heading_candidates[index + 1 :]:
                if following.tag in {"h1", "h2"}:
                    break
                if following.tag == "h3":
                    members.append(following)
            if len(members) >= 3:
                collections.append((item, members))
        featured = max(collections, key=lambda item: len(item[1]), default=None)
        selected: list[ObservedElement] = []
        first_heading = next((item for item in heading_candidates if item.tag == "h1"), None)
        if first_heading is not None:
            selected.append(first_heading)
        # A featured collection is the page's concrete proof. Present its
        # heading and visible members before generic capability/metric
        # headings, otherwise a full walkthrough can consume the Home budget
        # without ever explaining the projects it claims to showcase.
        if featured is not None:
            collection_heading, members = featured
            if collection_heading not in selected:
                selected.append(collection_heading)
            selected.extend(item for item in members if item not in selected)
        # Keep section-level context compact after the featured proof.
        selected.extend(
            item for item in heading_candidates if item.tag == "h2" and item not in selected
        )
        selected.extend(item for item in candidates if item not in selected)
        return selected[:limit]

    @staticmethod
    def _evidence_fallback(
        candidate, context: ProductContext
    ) -> WorkflowProposal:
        """Use a validated candidate, never navigation order, after model failure."""
        if candidate is None:
            raise PlanningValidationError("no evidence-grounded candidate flow is available")
        try:
            return build_page_complete_proposal(context, candidate)
        except ValueError as error:
            raise PlanningValidationError(str(error)) from error

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
            if not item.href or (item.source_url or context.url).rstrip("/") != context.url.rstrip("/"):
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
            words = set(re.findall(r"[a-z0-9]{3,}", f"{item.name} {item.text or ''} {item.href}".lower()))
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
        steps = opening_steps + (targeted_steps or [
            SemanticOperation(
                kind=OperationKind.NAVIGATE,
                intent=f"Open {route.rsplit('/', 1)[-1] or 'dashboard'}",
                value=route,
                postconditions=[Postcondition(kind="url", expected=route)],
            )
            for route in routes[1:]
        ])
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
