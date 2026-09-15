"""Structured LLM planning with ProductLens-owned evidence and safety validation."""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse, urlsplit

from pydantic import ValidationError

from productlens.contracts.models import (
    ActionCapability,
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
from productlens.observability.logging import redact_prompt_text
from productlens.planning.candidates import (
    build_page_complete_proposal,
    navigation_control_for_transition,
    select_candidate_flow,
    validate_flow_scope,
)
from productlens.planning.capabilities import (
    CapabilityCompilationError,
    compile_read_only_form_inspection,
    compile_record_creation,
)
from productlens.planning.capability_resolution import resolve_capabilities
from productlens.planning.rehearsal import CapabilitySelectionError, select_rehearsal_capability
from productlens.planning.side_effects import (
    SideEffectPolicyError,
    authorize_operation,
    side_effect_decision,
)
from productlens.planning.synthetic import hydrate_operations
from productlens.providers.errors import ProviderError
from productlens.providers.interfaces import LLMProvider
from productlens.urls import canonical_product_url


def _canonical_url(value: str) -> str:
    """Normalize equivalent browser URL spellings for planning transitions."""
    return canonical_product_url(value)


def _route_key(value: str) -> tuple[str, str, str]:
    """Return the transport-independent route identity used for navigation.

    Query parameters commonly encode filters, pagination, or SPA state.  They
    must be preserved in evidence and postconditions, but they must not make a
    route look unobserved when the same visible control exposed its canonical
    path without those parameters.
    """
    parsed = urlsplit(_canonical_url(value))
    return parsed.scheme, parsed.netloc, parsed.path


def _semantic_words(value: str) -> set[str]:
    """Return meaningful words for conservative semantic target matching."""
    return {token for token in re.findall(r"[a-z0-9]+", (value or "").casefold()) if len(token) > 2}


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
            objective_spec.minimum_duration_seconds
            if objective_spec
            else max(30, target_duration_seconds - 45)
        )
        approved_maximum = maximum_duration_seconds or (
            objective_spec.maximum_duration_seconds
            if objective_spec
            else min(900, max(180, target_duration_seconds + 60))
        )
        if not approved_minimum <= target_duration_seconds <= approved_maximum:
            raise PlanningValidationError(
                "requested duration is outside the approved objective envelope"
            )
        # A route list is navigation metadata, not product knowledge. Allow a
        # tiny compatibility snapshot to use the structured provider, but if
        # discovery already claims supporting routes it must materialise their
        # page-local evidence before a provider/fallback can propose a story.
        # Otherwise a plausible route tour can bypass the reliability-first
        # PageKnowledge contract entirely.
        if context.relevant_routes and not context.page_knowledge:
            raise PlanningValidationError(
                "page lacks readable local evidence for discovered supporting routes"
            )
        # Persist the generic capability decision alongside the plan.  This is
        # advisory until each selected action is re-grounded and verified, but
        # it gives execution/recovery a common evidence-backed contract rather
        # than making every planner rediscover interaction types independently.
        resolution = resolve_capabilities(objective, context)
        if not any(
            item.intent == resolution.intent and item.source_url == resolution.source_url
            for item in context.capability_resolutions
        ):
            context.capability_resolutions.append(resolution)
        candidate = select_candidate_flow(context, objective)
        self._validate_objective_grounding(context, candidate)
        # A requested duration must be plausible from *captured* production
        # evidence before a fresh browser context is opened. This is not a
        # request to pad a sparse form with holds: discovery must instead add
        # relevant page-local content or select a stronger candidate flow.
        if (
            objective_spec is not None
            and context.page_knowledge
            and candidate is not None
            and candidate.estimated_duration_seconds < approved_minimum
        ):
            raise PlanningValidationError(
                "candidate lacks enough evidence-backed content for the approved minimum duration; "
                "targeted exploration must collect relevant page-local scenes"
            )
        objective_lower = objective.lower()
        # Objective understanding is the authoritative scope classifier.  The
        # previous lexical check only recognised the literal word ``full``;
        # a request such as "create a complete walkthrough" was consequently
        # sent through the focused-flow path and could drop required detail
        # pages (for example the representative week view).  Preserve the
        # lexical signals as compatibility fallbacks for older contexts, but
        # honour the validated ObjectiveSpec whenever it is available.
        complete_objective = (
            bool(objective_spec and objective_spec.demo_type == "full_walkthrough")
            or bool(re.search(r"\bfull\b(?:\s+\w+){0,4}\s+walkthrough\b", objective_lower))
            or "each tab" in objective_lower
            or "every safe primary" in objective_lower
        )
        # Thorough depth can apply to a focused feature workflow. Only an
        # explicit completeness signal (full/complete/entire/every/each)
        # broadens planning to all safe primary sections.
        # Whole-product requests are not a deterministic navigation tour.
        # They must be compiled from the fresh PageKnowledge selected by a
        # scored candidate, so every transition has local evidence and every
        # page receives establish/explore/explain/demonstrate/verify beats.
        if complete_objective:
            if candidate is None:
                raise PlanningValidationError(
                    "no evidence-grounded candidate flow for complete walkthrough"
                )
            try:
                proposal = build_page_complete_proposal(context, candidate)
            except ValueError as error:
                raise PlanningValidationError(str(error)) from error
        else:
            # The evidence candidate is the minimum editorial contract for a
            # focused walkthrough.  A free-form model proposal may be valid
            # JSON yet collapse a rich page to one arbitrary button/route.
            # Compile the grounded candidate first so every selected page and
            # local reading beat survives.  The model remains available for
            # extraction/editorial enrichment, but it cannot replace the
            # workflow proof owned by ProductLens.
            if candidate is not None and context.page_knowledge:
                proposal = self._evidence_fallback(candidate, context)
            else:
                # Compatibility for callers that provide only a live element
                # snapshot (before discovery materialises PageKnowledge).
                # Those requests still use the validated structured planner.
                try:
                    proposal = await self.provider.structured(
                        self._prompt(objective, context, allow_external_side_effects),
                        WorkflowProposal,
                    )
                except (ValidationError, ProviderError):
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
            # Complete walkthrough proposals are compiled from page evidence,
            # but their transitions still need the same semantic re-grounding
            # as focused proposals.  Convert an observed same-origin route to
            # its visible navigation control before validation so a complete
            # tour never falls back to direct URL navigation merely because
            # the page-complete builder emitted a route transition.
            # Fresh discovery carries page-local evidence for every route. In
            # that mode re-ground the complete-tour transitions to visible
            # controls. Sparse compatibility fixtures may only contain a
            # global navigation shell; retain their legacy route semantics
            # until fresh page evidence is available.
            if context.page_knowledge and any(
                page.evidence_refs for page in context.page_knowledge
            ):
                proposal = self._compile_navigation(proposal, context)
            self._validate(proposal, context, allow_external_side_effects)
            scope_failures = validate_flow_scope(proposal, context=context, candidate=candidate)
            if scope_failures:
                raise PlanningValidationError(scope_failures[0])
        specification = context.objective
        if specification and "create_isolated_record" in specification.permitted_mutations:
            if not allow_external_side_effects:
                raise PlanningValidationError(
                    "isolated record creation requires explicit runtime authorization"
                )
            capabilities = []
            for raw in context.capabilities:
                try:
                    capabilities.append(ActionCapability.model_validate(raw))
                except ValidationError:
                    continue
            try:
                creation = compile_record_creation(
                    select_rehearsal_capability(
                        context, capabilities, require_verified_outcome=True
                    )
                )
            except (CapabilitySelectionError, CapabilityCompilationError) as error:
                raise PlanningValidationError(
                    "authorized record creation has no independently verified form capability"
                ) from error
            creation_source = (
                creation[0].target.source_url if creation and creation[0].target else None
            )
            proposal = self._insert_at_capability_source(
                proposal, context, creation_source, creation
            ).model_copy(
                update={
                    "selected_workflow": f"{proposal.selected_workflow} with verified record creation",
                    "expected_outcomes": [*proposal.expected_outcomes, creation[-1].target.name],
                }
            )
            # The appended submit crosses the side-effect boundary after the
            # original read-only candidate has been validated. Validate the
            # full compiled plan again before any target is grounded/executed.
            self._validate(proposal, context, allow_external_side_effects)
            scope_failures = validate_flow_scope(proposal, context=context, candidate=candidate)
            if scope_failures:
                raise PlanningValidationError(scope_failures[0])
        # A read-only prospect objective can still benefit from seeing the
        # actual setup surface.  Discovery's reversible capability probe is
        # sufficient to open and type into the grounded form, but it is never
        # permission to submit.  Compile this only when the request names a
        # setup/detail/form beat and only for the semantically best capability;
        # unrelated forms remain out of scope.
        specification = context.objective
        if specification and not specification.permitted_mutations:
            # ``create a walkthrough/demo/video`` describes the requested
            # deliverable, not a product-side record mutation.  Strip that
            # framing before deciding whether to open and fill a form; doing
            # otherwise made every demo request trigger an unrelated create
            # flow and a return navigation.
            request_text = re.sub(
                r"^\s*(?:please\s+)?create\s+(?:a\s+)?(?:thorough\s+|full\s+|complete\s+)?(?:walkthrough|demo|video)\b",
                "",
                specification.raw.casefold(),
            )
            request_words = set(re.findall(r"[a-z0-9]{3,}", request_text)) | {
                word.casefold()
                for value in specification.must_show
                for word in re.findall(r"[a-z0-9]{3,}", value.casefold())
            }
            wants_setup = bool(
                request_words
                & {
                    "setup",
                    "details",
                    "detail",
                    "form",
                    "fields",
                    "configure",
                    # Merely asking for configuration *context* should show the
                    # observed settings surface, not open a form and navigate back
                    # to the feature. Editing/creation is opt-in through an
                    # explicit action verb or form request.
                    "appointment",
                    "schedule",
                    "create",
                    "add",
                }
            )
            already_has_form = any(
                step.kind
                in {
                    OperationKind.OPEN_MODAL,
                    OperationKind.FILL_TEXT,
                    OperationKind.FILL_EMAIL,
                    OperationKind.FILL_PHONE,
                    OperationKind.SELECT_OPTION,
                    OperationKind.SELECT_DATE,
                }
                for step in proposal.steps
            )
            if wants_setup and not already_has_form:
                capabilities: list[ActionCapability] = []
                for raw in context.capabilities:
                    try:
                        capabilities.append(ActionCapability.model_validate(raw))
                    except ValidationError:
                        continue
                try:
                    capability = select_rehearsal_capability(
                        context, capabilities, require_verified_outcome=False
                    )
                    inspection = compile_read_only_form_inspection(capability)
                    proposal = self._insert_at_capability_source(
                        proposal, context, capability.source_url, inspection
                    ).model_copy(
                        update={
                            "selected_workflow": f"{proposal.selected_workflow} with read-only setup inspection",
                            "expected_outcomes": [
                                *proposal.expected_outcomes,
                                f"visible {capability.purpose} setup fields are explained",
                            ],
                        }
                    )
                    self._validate(proposal, context, allow_external_side_effects)
                except (CapabilitySelectionError, CapabilityCompilationError):
                    # A form probe is an enhancement, not a reason to invent a
                    # control. The base evidence flow remains valid when no
                    # capability can be compiled safely.
                    pass
        steps = self._ground(proposal, context)
        # Isolated demo records must never reuse an observed customer/contact
        # value. Keep this broad and evidence-driven: it is derived from the
        # discovered product text/facts rather than known products or fields.
        observed_values = {
            value
            for value in [
                context.visible_text,
                *(item.text or "" for item in context.elements),
                *(fact for page in context.page_knowledge for fact in page.visible_facts),
            ]
            if value
        }
        steps, synthetic_dataset = hydrate_operations(
            steps,
            product_key=context.url,
            forbidden_values=observed_values,
        )
        side_effects = {
            operation.id: side_effect_decision(operation)
            for operation in steps
            if operation.kind in SIDE_EFFECTING
        }
        # The selected candidate is discovery evidence, while ``proposal`` is
        # the validated executable story.  Reconcile the persisted summary at
        # this boundary so it cannot retain routes/outcomes from a rejected
        # model proposal or an earlier candidate-selection pass.
        selected_candidate = candidate
        if candidate is not None:
            actual_pages: list[str] = []
            evidence: list[str] = []
            semantic_steps: list[str] = []
            for operation in steps:
                page_url = operation.page_url or (
                    operation.target.source_url if operation.target else None
                )
                if page_url and page_url not in actual_pages:
                    actual_pages.append(page_url)
                evidence.extend(operation.evidence_refs)
                label = operation.target.name if operation.target else operation.intent
                semantic_steps.append(f"{operation.kind.value}:{label}")
            selected_candidate = candidate.model_copy(
                update={
                    "name": proposal.selected_workflow,
                    "page_urls": actual_pages or candidate.page_urls,
                    "supporting_page_urls": [
                        page for page in candidate.supporting_page_urls if page in actual_pages
                    ],
                    "expected_outcomes": list(proposal.expected_outcomes),
                    "semantic_steps": semantic_steps,
                    "evidence_coverage": list(
                        dict.fromkeys([*candidate.evidence_coverage, *evidence])
                    ),
                    "estimated_duration_seconds": max(
                        candidate.estimated_duration_seconds,
                        int(
                            sum(max(1, len(operation.intent.split()) // 2) for operation in steps)
                            + 15
                        ),
                    ),
                }
            )
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
                    ]
                    or ["operation completed and the resulting state is readable"],
                    evidence_refs=[
                        *operation.evidence_refs,
                        *([f"page:{operation.page_url}"] if operation.page_url else []),
                        *[
                            reference
                            for reference in (
                                f"element:{operation.target.name}" if operation.target else None,
                                f"page:{operation.target.source_url}"
                                if operation.target and operation.target.source_url
                                else None,
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
                "selected_candidate_flow": selected_candidate.model_dump(mode="json")
                if selected_candidate
                else None,
            },
            expected_outcomes=proposal.expected_outcomes,
            important_elements=proposal.important_elements,
            excluded_areas=proposal.excluded_areas,
            viewport_strategy="focus observed semantic targets; preserve readable context",
            risk_flags=[
                *proposal.risk_flags,
                *[
                    f"side_effect:{operation_id}:{decision}"
                    for operation_id, decision in side_effects.items()
                ],
            ],
            stop_conditions=[
                "all critical postconditions pass",
                "unexpected authentication",
                "budget exhausted",
            ],
        )

    @staticmethod
    def _proposal_active_page(proposal: WorkflowProposal, context: ProductContext) -> str:
        """Derive the browser state after the last planned semantic operation."""
        active = _canonical_url(context.url)
        for operation in proposal.steps:
            if operation.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}:
                destination = next(
                    (
                        str(condition.expected)
                        for condition in operation.postconditions
                        if condition.kind == "url"
                    ),
                    str(operation.value or ""),
                )
                if destination:
                    active = _canonical_url(urljoin(context.url, destination))
            elif operation.page_url:
                active = _canonical_url(operation.page_url)
            elif operation.target and operation.target.source_url:
                active = _canonical_url(operation.target.source_url)
        return active

    @classmethod
    def _transition_to_capability_source(
        cls, proposal: WorkflowProposal, context: ProductContext, source_url: str | None
    ) -> list[SemanticOperation]:
        """Return to the exact state that owns a verified form capability."""
        if not source_url:
            raise PlanningValidationError("verified form capability has no source page")
        source = _canonical_url(urljoin(context.url, source_url))
        current = cls._proposal_active_page(proposal, context)
        if current == source:
            return []
        control = navigation_control_for_transition(context, current, source)
        if control is not None:
            return [
                SemanticOperation(
                    kind=OperationKind.OPEN_NAVIGATION_ITEM,
                    intent=f"Return to the verified form workspace using visible {control.name}",
                    target=Target(
                        name=control.name,
                        selector=control.selector,
                        text=control.text or control.name,
                        source_url=control.source_url,
                        role=control.role,
                    ),
                    postconditions=[Postcondition(kind="url", expected=source)],
                    story_phase="transition",
                    page_url=source,
                    evidence_refs=[
                        f"page:{source}",
                        f"element:{control.name}",
                        "transition:capability-source",
                    ],
                )
            ]
        return [
            SemanticOperation(
                kind=OperationKind.NAVIGATE,
                intent="Return to the verified form workspace (no reliable visible navigation control was discovered)",
                value=source,
                postconditions=[Postcondition(kind="url", expected=source)],
                story_phase="transition",
                page_url=source,
                evidence_refs=[
                    f"page:{source}",
                    "fallback:direct-navigation-no-visible-control",
                    "transition:capability-source",
                ],
            )
        ]

    @classmethod
    def _insert_at_capability_source(
        cls,
        proposal: WorkflowProposal,
        context: ProductContext,
        source_url: str | None,
        steps: list[SemanticOperation],
    ) -> WorkflowProposal:
        """Place a verified capability at its first natural page visit.

        Capability probes used to be appended after the editorial route tour,
        which forced a needless return to Home (or another already-covered
        page).  Track the route state through the proposal and insert the
        probe immediately before leaving its source page.  A transition is
        retained only when the source page was not part of the selected flow.
        """
        if not source_url:
            raise PlanningValidationError("verified capability has no source page")
        source = _canonical_url(urljoin(context.url, source_url))
        active = _canonical_url(context.url)
        insert_at: int | None = None
        for index, operation in enumerate(proposal.steps):
            destination: str | None = None
            if operation.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}:
                destination = next(
                    (
                        str(condition.expected)
                        for condition in operation.postconditions
                        if condition.kind == "url"
                    ),
                    str(operation.value or ""),
                )
            elif operation.page_url:
                destination = operation.page_url
            elif operation.target and operation.target.source_url:
                destination = operation.target.source_url
            if (
                active == source
                and destination
                and _canonical_url(urljoin(context.url, destination)) != source
            ):
                insert_at = index
                break
            if destination:
                active = _canonical_url(urljoin(context.url, destination))
        if insert_at is None and active == source:
            insert_at = len(proposal.steps)
        if insert_at is None:
            transition = cls._transition_to_capability_source(proposal, context, source)
            insert_at = len(proposal.steps)
            steps = [*transition, *steps]
        compiled = [*proposal.steps[:insert_at], *steps, *proposal.steps[insert_at:]]
        return proposal.model_copy(update={"steps": compiled})

    @staticmethod
    def _validate_objective_grounding(context: ProductContext, candidate) -> None:
        """Refuse a polished route tour when required objective evidence is absent.

        The old planner could select a reachable Settings shell and then claim
        to explain its relationship to an operational feature.  A relationship
        is now a planning precondition: both sides must be present in the
        selected candidate's freshly captured page evidence.
        """
        specification = context.objective
        if specification is None:
            return
        # Older/resumable ObjectiveSpecs can describe duration and audience
        # without making a semantic entity/context claim. They remain valid
        # compatibility inputs; only explicit grounding requirements demand
        # PageKnowledge.
        if (
            not specification.primary_entity
            and not specification.supporting_relationships
            and not specification.must_show
        ):
            return
        if candidate is None:
            raise PlanningValidationError(
                "no candidate flow is grounded in the requested objective"
            )
        selected = {
            _canonical_url(page.url): page
            for page in context.page_knowledge
            if _canonical_url(page.url)
            in {
                _canonical_url(url)
                for url in [*candidate.page_urls, *candidate.supporting_page_urls]
            }
        }
        if not selected:
            raise PlanningValidationError("candidate flow has no fresh page knowledge")

        def vocabulary(value: str) -> set[str]:
            # Requests are often editorial prose ("home identity and
            # capabilities"), while page evidence is terse UI language. Do
            # not make harmless function words literal grounding requirements;
            # require the meaningful concept overlap below instead.
            stopwords = {
                "the",
                "and",
                "for",
                "from",
                "with",
                "into",
                "that",
                "this",
                "its",
                "then",
                "than",
                "every",
                "each",
                "all",
                "one",
                "on",
                "in",
                "of",
                "to",
                "a",
                "an",
                "is",
                "are",
                "be",
                "by",
                "or",
                "including",
                "initial",
                "dashboard",
                "view",
                "views",
                "screen",
                "screens",
                "tab",
                "tabs",
                # Editorial qualifiers describe how to present evidence, not
                # a literal DOM label that must appear on the selected page.
                "representative",
                "meaningful",
                "visible",
                "actual",
                "relevant",
                "primary",
                "complete",
                "full",
                "current",
                "requested",
                "details",
                "detail",
                "outcome",
                "result",
                "management",
                "workflow",
                "flow",
                "experience",
                # Generic surface descriptors may be added by objective
                # understanding even when the product uses a different
                # visible label (for example ``public diagram editor`` vs
                # ``Untitled Diagram``).  They are not feature evidence.
                "public",
                "private",
                "app",
                "application",
                "editor",
                "workspace",
                "tool",
                "software",
                "platform",
                "product",
            }
            words = {
                word for word in re.findall(r"[a-z0-9]{3,}", value.lower()) if word not in stopwords
            }
            # Normalize ordinary English inflections so an objective such as
            # "study planning" grounds against a visible "study plan" label
            # without embedding a product-specific synonym table. Keep the
            # original token as well; these are evidence aids, not fuzzy
            # authorization to select an unrelated page.
            for word in tuple(words):
                if word.endswith("s") and len(word) > 3:
                    words.add(word[:-1])
                if word.endswith("ing") and len(word) > 5:
                    base = word[:-3]
                    words.add(base)
                    if len(base) > 2 and base[-1] == base[-2]:
                        words.add(base[:-1])
                if word.endswith("ed") and len(word) > 4:
                    words.add(word[:-2])
            if "configuration" in words:
                words.add("config")
            if "config" in words:
                words.add("configuration")
            # Do not embed product/domain vocabulary here.  Synonyms such as
            # “appointment”/“booking” belong in the model-produced objective
            # concepts (or page evidence), not in a runtime route rule.  This
            # keeps grounding generic for an unseen product while preserving
            # the evidence threshold below.
            if "setup" in words:
                words.update({"config", "configuration", "settings"})
            # DOM semantics can name a drawing surface by its implementation
            # element (SVG) while a request calls it a canvas. Treat the
            # equivalence as an evidence ontology, not a site-specific route
            # rule; both terms still require an observed visual-surface node.
            if "svg" in words:
                words.add("canvas")
            if "canvas" in words:
                words.add("svg")
            return words

        evidence_words = set()
        for page in selected.values():
            path = urlsplit(page.url).path
            evidence_words |= vocabulary(
                " ".join(
                    [
                        page.title,
                        page.purpose,
                        *page.visible_sections,
                        *page.scroll_landmarks,
                        *page.actionable_controls,
                        *page.visible_facts,
                        path.replace("/", " ").replace("-", " "),
                        "home" if path in {"", "/"} else "",
                    ]
                )
            )
        # Login is a requested presentation chapter, not a page-local content
        # landmark. Once discovery has authenticated the fresh browser, the
        # credential boundary itself is the authoritative evidence that this
        # requirement is satisfied; requiring the post-login DOM to repeat
        # the word "login" incorrectly rejects otherwise valid plans.
        if context.authentication_state == "authenticated":
            evidence_words.update({"login", "sign", "authentication", "authenticated"})
        # Objectives describe capabilities in human language, while a UI can
        # label the same workspace with the entity alone ("Customers" versus
        # "Customer Management").  Ground the meaningful entity terms here;
        # the relationship validation below enforces any requested setup or
        # outcome role separately.
        objective_generic = {
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
            # Objective parsers may add a generic product descriptor to
            # an otherwise grounded noun phrase (for example
            # ``public diagram editor``).  These words describe the
            # delivery surface, not the feature being demonstrated;
            # keeping them out of grounding prevents an unfamiliar app
            # from being rejected merely because its title says
            # ``Untitled Diagram`` rather than ``Diagram Editor``.
            "public",
            "private",
            "app",
            "application",
            "editor",
            "workspace",
            "tool",
            "software",
            "platform",
            "product",
        }
        primary_words = vocabulary(specification.primary_entity or "") - objective_generic
        primary_overlap = len(primary_words & evidence_words) / max(1, len(primary_words))
        # Objective understanding may add one descriptive modifier that is
        # not repeated verbatim by the UI (``collaborative drawing workspace``
        # vs. a visible ``Drawing`` surface). Require at least one concrete
        # noun and half of a multi-word entity; single-token requests remain
        # exact. This avoids rejecting unfamiliar products without accepting
        # an unrelated candidate on a generic word alone.
        primary_grounded = (
            not primary_words
            or primary_words.issubset(evidence_words)
            or (len(primary_words) >= 2 and primary_overlap >= 0.5)
        )
        # Model-parsed full-walkthrough requests often use an editorial
        # product description ("the study planning experience") instead of
        # the product's visible brand/title ("Interview Crack"). Do not turn
        # that wording mismatch into a false rejection when concrete
        # must-show requirements are independently grounded across multiple
        # discovered pages. Narrow feature/workflow requests remain strict.
        broad_walkthrough_grounded = (
            specification.demo_type == "full_walkthrough"
            and len(selected) >= 2
            and any(
                vocabulary(requirement) & evidence_words
                for requirement in specification.must_show
                if requirement != specification.primary_entity
            )
        )
        if primary_words and not primary_grounded and not broad_walkthrough_grounded:
            raise PlanningValidationError(
                f"requested entity is not grounded by the selected candidate: {specification.primary_entity}"
            )
        for required in specification.must_show:
            if (
                required == specification.primary_entity
                and not primary_grounded
                and broad_walkthrough_grounded
            ):
                continue
            required_words = vocabulary(required)
            # Objective-understanding models sometimes promote an explanatory
            # sentence (for example, "what the product helps a learner plan
            # and track") into ``must_show``.  That is a story intent, not a
            # literal UI entity that can be grounded by a single label.  For
            # broad walkthroughs, page-purpose and section evidence already
            # enforce this intent; do not reject a valid product merely
            # because its UI uses different wording.  Concrete noun phrases
            # (Today, Progress, invoice export, etc.) remain strict below.
            if (
                specification.demo_type == "full_walkthrough"
                and re.match(r"^(?:what|how|why)\b", required.strip().casefold())
                and re.search(
                    r"\b(?:product|application|system|workspace|feature)\b", required.casefold()
                )
            ):
                continue
            # Preserve hard rejection for an entirely unsupported requirement,
            # but tolerate editorial wording where one descriptor is implicit
            # in the page title/layout (for example "identity" on a named
            # portfolio home page). A 60% meaningful-token threshold prevents
            # a single generic word from laundering an unrelated requirement.
            grounded_words = required_words & evidence_words
            required_threshold = max(1, int(len(required_words) * 0.6 + 0.999))
            editorial_descriptors = {
                "identity",
                "value",
                "proposition",
                "capability",
                "capabilities",
                "meaningful",
                "featured",
                "project",
                "projects",
                "career",
                "role",
                "roles",
                "contribution",
                "contributions",
                "architecture",
                "architectural",
                "work",
                "representative",
                "theme",
                "themes",
                "available",
                "path",
                "visible",
                "page",
                "pages",
                "operational",
                "state",
                "states",
                "sidebar",
                "handled",
                "current",
                "relevant",
                "actual",
            }
            if required_words and required_words.issubset(editorial_descriptors):
                # These words describe how the evidence should be presented,
                # not an additional product entity that can be matched
                # literally (for example, "visible states"). The page-level
                # evidence and scene completion gates still enforce that a
                # readable state was actually captured.
                continue
            if len(grounded_words) < required_threshold:
                # Walkthrough requests are often written as editorial briefs
                # ("Home identity", "Timeline with career roles") while the
                # site exposes concrete labels such as a person's name or
                # "Professional History". Accept the requirement when a
                # structural page/section token is grounded and the remaining
                # words are presentation descriptors, not a hidden product
                # claim. The selected page still has to carry real visible
                # evidence and is independently explored later.
                structural_overlap = set()
                for page in selected.values():
                    path = urlsplit(page.url).path
                    page_structural = vocabulary(
                        " ".join(
                            [
                                page.title,
                                page.purpose,
                                path.replace("/", " ").replace("-", " "),
                                "home" if path in {"", "/"} else "",
                                *page.actionable_controls,
                            ]
                        )
                    )
                    structural_overlap |= required_words & page_structural
                residual = required_words - structural_overlap
                if structural_overlap and residual.issubset(editorial_descriptors):
                    continue
            if required_words and len(grounded_words) < required_threshold:
                raise PlanningValidationError(
                    f"must-show requirement is not grounded by selected pages: {required}"
                )
        relationship_generic = objective_generic
        for relation in specification.supporting_relationships:
            source = vocabulary(relation.source)
            target = vocabulary(relation.target)
            if not relation.required:
                continue
            # Persisted objective artifacts may contain a model-produced
            # relationship that was inferred from ordinary prose (for
            # example, ``Home identity``).  A relationship is a workflow
            # dependency only when the original request explicitly connects
            # both sides; otherwise page-local evidence and the selected flow
            # remain the authority.  This guard also makes older runs safe to
            # resume after the objective-understanding filter is tightened.
            raw_objective = specification.raw.casefold()
            source_phrase = " ".join(re.findall(r"[a-z0-9]{3,}", relation.source.casefold()))
            target_phrase = " ".join(re.findall(r"[a-z0-9]{3,}", relation.target.casefold()))
            connector = r"(?:->|→|configures|explains|supports|in the context of|configured by|with context from|using)"
            if source_phrase and target_phrase:
                explicit_relation = bool(
                    re.search(
                        rf"{re.escape(source_phrase)}\s*{connector}\s*{re.escape(target_phrase)}",
                        raw_objective,
                    )
                    or re.search(
                        rf"{re.escape(target_phrase)}\s*{connector}\s*{re.escape(source_phrase)}",
                        raw_objective,
                    )
                )
                if not explicit_relation:
                    continue

            # A Settings shell can expose a configuration label without ever
            # showing that configuration's own detail state. Relationship
            # context is only useful when selected page *identity* (not its
            # navigation chrome) establishes both sides of the relationship.
            def page_identity_words(page) -> set[str]:
                path = urlsplit(page.url).path
                # The root route is the conventional Home page even when the
                # document title never contains the word "home". Preserve
                # that structural evidence for objective relationships such
                # as Home -> Timeline without introducing a product-specific
                # route exception.
                structural = "home" if path in {"", "/"} else ""
                return vocabulary(
                    " ".join(
                        [
                            page.title,
                            page.purpose,
                            path.replace("/", " ").replace("-", " "),
                            structural,
                        ]
                    )
                )

            shared_entity = (source & target) - relationship_generic
            # A relationship must first be tied to an observed entity.  Do
            # not require every prose descriptor in the request to appear
            # literally: products may call an operational workspace "Leads
            # V3" while the request calls it "Lead Management".  The
            # semantic detail checks below still require a separate concrete
            # setup/context page and an operational page.
            if shared_entity and not shared_entity.issubset(evidence_words):
                raise PlanningValidationError(
                    f"required objective relationship is not grounded by selected pages: {relation.source} -> {relation.target}"
                )

            def subject_pages(subject: set[str], counterpart: set[str]):
                meaningful = subject - relationship_generic
                # The setup side must prove the term that distinguishes it
                # from the operational feature. A shared entity noun alone
                # cannot establish a configuration relationship when it also
                # appears on the feature's workspace.
                distinguishing = meaningful - (counterpart - relationship_generic)
                required_words = distinguishing or meaningful or subject
                # Routes commonly use a plural entity while the request uses
                # a singular feature phrase. Vocabulary normalisation retains
                # both forms, so a concrete non-generic subject is sufficient.
                return [
                    page for page in selected.values() if page_identity_words(page) & required_words
                ]

            source_pages = subject_pages(source, target)
            target_pages = subject_pages(target, source)
            distinct_page_pair = any(
                source_page.url != target_page.url
                for source_page in source_pages
                for target_page in target_pages
            )
            if not source_pages or not target_pages or not distinct_page_pair:
                raise PlanningValidationError(
                    "required objective relationship lacks a selected semantic detail page: "
                    f"{relation.source} -> {relation.target}"
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
            "PointerSequence, and Drag. PointerSequence values must contain observed points; Drag values "
            "must contain an observed semantic destination and optional duration. These are universal browser "
            "gestures, never product-specific record or graph actions. "
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
    def _validate(
        proposal: WorkflowProposal,
        context: ProductContext,
        allow_side_effects: bool,
    ) -> None:
        observed_selectors = {item.selector for item in [*context.elements, *context.navigation]}
        observed_names = {item.name.lower() for item in [*context.elements, *context.navigation]}
        capability_targets: set[tuple[str | None, str]] = set()
        for raw in context.capabilities:
            try:
                capability = ActionCapability.model_validate(raw)
            except ValidationError:
                continue
            for field in capability.form_schema.fields if capability.form_schema else []:
                capability_targets.add((capability.source_url, field.name.casefold()))
            for target in (capability.submit_target, capability.outcome_target):
                if target is not None:
                    capability_targets.add((target.source_url, target.name.casefold()))
        observed_routes = {
            _canonical_url(urljoin(item.source_url or context.url, item.href))
            for item in context.navigation
            if item.href
        }
        # A page-local element can be the only captured witness for a route
        # during an incremental discovery snapshot.  Its source URL is still
        # observed evidence and must be accepted when the compiler inserts the
        # required page entry before a local scroll.
        observed_routes.update(
            _canonical_url(item.source_url)
            for item in context.elements
            if item.source_url and item.source_url != context.url
        )
        observed_route_keys = {_route_key(route) for route in observed_routes}
        allowed_routes = {
            _canonical_url(value) for value in (context.url, *context.relevant_routes)
        } | observed_routes
        allowed_route_keys = {
            _route_key(value) for value in (context.url, *context.relevant_routes)
        } | observed_route_keys
        source_by_selector = {item.selector: item.source_url for item in context.elements}
        navigated_to: set[str] = {_canonical_url(context.url)}
        for operation in proposal.steps:
            if operation.kind in SIDE_EFFECTING:
                try:
                    authorize_operation(operation, allow_side_effects)
                except SideEffectPolicyError as error:
                    raise PlanningValidationError(str(error)) from error
            if operation.kind is OperationKind.NAVIGATE:
                destination = _canonical_url(urljoin(context.url, str(operation.value)))
                if (
                    destination not in allowed_routes
                    and _route_key(destination) not in allowed_route_keys
                ):
                    raise PlanningValidationError(
                        "Navigation target is not observed and same-origin"
                    )
            elif operation.target is None and operation.kind not in {
                OperationKind.READ_VALUE,
                OperationKind.WAIT_FOR_STATE,
                OperationKind.VERIFY_STATE,
                OperationKind.POINTER_SEQUENCE,
            }:
                raise PlanningValidationError(f"Target is required for {operation.kind}")
            elif operation.kind is OperationKind.POINTER_SEQUENCE:
                points = (
                    operation.value.get("points") if isinstance(operation.value, dict) else None
                )
                pattern = (
                    operation.value.get("pattern") if isinstance(operation.value, dict) else None
                )
                generated_surface_pattern = (
                    pattern == "short_reversible_stroke" and operation.target is not None
                )
                if (
                    not isinstance(points, list) or len(points) < 2
                ) and not generated_surface_pattern:
                    raise PlanningValidationError(
                        "PointerSequence requires observed points or an evidence-backed surface pattern"
                    )
                if not operation.evidence_refs:
                    raise PlanningValidationError(
                        "PointerSequence requires evidence references for its observed path"
                    )
            elif operation.kind is OperationKind.DRAG:
                payload = operation.value if isinstance(operation.value, dict) else None
                destination = payload.get("destination") if isinstance(payload, dict) else None
                if not isinstance(destination, dict):
                    raise PlanningValidationError("Drag requires an observed semantic destination")
                try:
                    destination_target = Target.model_validate(destination)
                except ValidationError as error:
                    raise PlanningValidationError(
                        "Drag destination must be a valid semantic target"
                    ) from error
                if not operation.evidence_refs:
                    raise PlanningValidationError(
                        "Drag requires evidence references for source and destination geometry"
                    )
                if not any(
                    destination_target.selector == item.selector
                    or destination_target.name.casefold() == item.name.casefold()
                    for item in [*context.elements, *context.navigation]
                ):
                    raise PlanningValidationError(
                        f"Drag destination is not grounded in current evidence: {destination_target.name}"
                    )
            elif (
                operation.target.selector not in observed_selectors
                and operation.target.name.lower() not in observed_names
                and (operation.target.source_url, operation.target.name.casefold())
                not in capability_targets
                and not (
                    operation.target.selector == "body"
                    and operation.target.source_url
                    and any(
                        _canonical_url(page.url) == _canonical_url(operation.target.source_url)
                        and (page.visible_facts or page.evidence_refs)
                        for page in context.page_knowledge
                    )
                )
            ):
                raise PlanningValidationError(
                    f"Target is not grounded in current evidence: {operation.target.name}"
                )
            if operation.kind is OperationKind.NAVIGATE:
                navigated_to.add(_canonical_url(urljoin(context.url, str(operation.value))))
            elif operation.kind is OperationKind.OPEN_NAVIGATION_ITEM:
                destination = next(
                    (
                        str(condition.expected)
                        for condition in operation.postconditions
                        if condition.kind == "url"
                    ),
                    None,
                )
                if destination:
                    navigated_to.add(_canonical_url(urljoin(context.url, destination)))
            elif operation.target is not None:
                # Prefer provenance carried on the operation target. A
                # selector is not globally unique in SPAs and may otherwise
                # resolve to a later page's repeated card/control.
                source = (
                    operation.target.source_url
                    if operation.target.selector
                    else source_by_selector.get(operation.target.selector or "")
                )
                if source and (
                    _canonical_url(source) not in navigated_to
                    and not any(_route_key(route) == _route_key(source) for route in navigated_to)
                ):
                    raise PlanningValidationError(
                        f"Target {operation.target.name} requires an observed navigation to {source}"
                    )
                # Names and selectors are only unique within a page state.
                # Prefer the operation's source/selector provenance before
                # falling back to a name match; otherwise a repeated sidebar
                # label from another discovered page can make a valid href
                # appear to contradict the intended navigation.
                target_source = operation.target.source_url
                target_selector = operation.target.selector
                item = next(
                    (
                        candidate
                        for candidate in context.elements
                        if target_source
                        and candidate.source_url
                        and _canonical_url(candidate.source_url) == _canonical_url(target_source)
                        and target_selector
                        and candidate.selector == target_selector
                    ),
                    next(
                        (
                            candidate
                            for candidate in context.elements
                            if target_source
                            and candidate.source_url
                            and _canonical_url(candidate.source_url)
                            == _canonical_url(target_source)
                            and candidate.name.casefold() == operation.target.name.casefold()
                        ),
                        next(
                            (
                                candidate
                                for candidate in context.elements
                                if target_selector and candidate.selector == target_selector
                            ),
                            next(
                                (
                                    candidate
                                    for candidate in context.elements
                                    if candidate.name.casefold() == operation.target.name.casefold()
                                ),
                                None,
                            ),
                        ),
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
                    expected_destination = urljoin(context.url, expected_url)
                    matches_expected = (
                        observed_destination.endswith(expected_url.removeprefix("**"))
                        if expected_url.startswith("**/")
                        else (
                            _canonical_url(expected_destination)
                            == _canonical_url(observed_destination)
                            or _route_key(expected_destination) == _route_key(observed_destination)
                        )
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
        capability_target_names: set[tuple[str | None, str]] = set()
        for raw in context.capabilities:
            try:
                capability = ActionCapability.model_validate(raw)
            except ValidationError:
                continue
            for field in capability.form_schema.fields if capability.form_schema else []:
                capability_target_names.add((capability.source_url, field.name.casefold()))
            for target in (capability.submit_target, capability.outcome_target):
                if target is not None:
                    capability_target_names.add((target.source_url, target.name.casefold()))
        by_selector = {item.selector: item for item in observed_items}
        by_name = {item.name.lower(): item for item in observed_items}
        roles = {
            "a": "link",
            "button": "button",
            "select": "combobox",
            "textarea": "textbox",
            "h1": "heading",
            "h2": "heading",
            "h3": "heading",
            "h4": "heading",
        }
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
            # A page-level verified hold is intentionally grounded by the
            # captured PageKnowledge record rather than a DOM element. This
            # is used for dense grids/transient states where every element is
            # a table cell or modal control and selecting one would create a
            # misleading editorial target.
            if (
                selector == "body"
                and operation.target.source_url
                and any(
                    _canonical_url(page.url) == _canonical_url(operation.target.source_url)
                    and (page.visible_facts or page.evidence_refs)
                    for page in context.page_knowledge
                )
            ):
                grounded.append(operation)
                continue
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
                if (
                    operation.target.source_url,
                    operation.target.name.casefold(),
                ) in capability_target_names:
                    # Fields inside a reversible modal are intentionally absent
                    # from the resting page DOM. Preserve their discovery-time
                    # semantic label; execution re-grounds only after the
                    # verified open-modal step has made the form visible.
                    grounded.append(operation)
                    continue
                raise PlanningValidationError(
                    f"Target is not grounded in current evidence: {operation.target.name}"
                )
            test_id = None
            if item.selector.startswith('[data-testid="'):
                test_id = item.selector.removeprefix('[data-testid="').removesuffix('"]')
            # A discovered canvas/SVG surface is often identified only by its
            # semantic tag. Preserve that selector when discovery proves it is
            # a unique actionable surface; stripping it would leave only the
            # synthetic label (for example ``svg workspace``), which cannot be
            # re-grounded after a tool changes the editor state.
            stable_selector = (
                item.selector
                if item.selector.startswith(("#", "["))
                or (
                    item.tag.casefold() in {"canvas", "svg"}
                    and item.selector.casefold() == item.tag.casefold()
                )
                else None
            )
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
                text=item.name
                if item.selector in {"a", "button", "h1", "h2", "h3", "h4"}
                else (None if item.role or item.tag in roles else item.name),
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
                        option
                        for option in item.options
                        if option.strip()
                        and option.strip().lower()
                        not in {"select", "select an option", "choose", "choose an option"}
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
                    if condition.target
                    and condition.target.name.lower() == operation.target.name.lower()
                    else condition
                )
            # The visible anchor's href is the authoritative destination for
            # semantic navigation. Models occasionally normalize away a query,
            # retain a stale SPA route, or copy a destination from another
            # repeated card. Once the target has been grounded to the current
            # evidence, align its URL postcondition with that observed href;
            # this prevents planning-time contradictions without inventing a
            # route or silently switching to direct navigation.
            if operation.kind is OperationKind.OPEN_NAVIGATION_ITEM and item.href:
                observed_destination = urljoin(item.source_url or context.url, item.href)
                postconditions = [
                    condition.model_copy(update={"expected": observed_destination})
                    if condition.kind == "url"
                    else condition
                    for condition in postconditions
                ]
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
            item
            for item in context.elements
            if (item.source_url or context.url).rstrip("/") == canonical and item.name.strip()
        ]
        page = next(
            (item for item in context.page_knowledge if item.url.rstrip("/") == canonical), None
        )
        # Some visual editors ship an untouched starter document whose sample
        # content is literally labelled "Heading"/"Title" and followed by
        # lorem-ipsum copy.  Those labels are implementation filler, not a
        # meaningful page-local story subject.  Filter them only when the
        # same page evidence proves the placeholder pattern; a real product
        # section named Heading remains eligible on all other pages.
        page_evidence_text = " ".join(
            str(value) for value in (getattr(page, "visible_facts", []) if page else [])
        ).casefold()
        placeholder_landmark = bool(
            "lorem ipsum" in page_evidence_text
            and any(token in page_evidence_text for token in ("heading", "title", "description"))
        )
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
            "reason",
            "message",
            "submit",
            "home",
            "back",
            "menu",
            "close",
            # Footer/navigation headings are persistent chrome, not the local
            # chapter content a full walkthrough should spend reading time on.
            "navigation",
            "connect",
            "footer",
        }
        navigation_labels = {" ".join(item.name.split()).lower() for item in context.navigation}
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
                or (
                    placeholder_landmark
                    and (
                        key in {"heading", "title", "description"}
                        or "lorem ipsum" in key
                        or any(
                            key.startswith(f"{value} ")
                            for value in ("heading", "title", "description")
                        )
                        or (
                            item.tag in {"a", "button"}
                            and not item.href
                            and len(re.findall(r"[a-z0-9]+", key)) <= 2
                        )
                    )
                )
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
            # Headings establish a section, but a dashboard/table/form page
            # often exposes only one heading while its actual interaction
            # surface is represented by descriptive rows, filters, or cards.
            # Keep a bounded set of those page-local witnesses as supplemental
            # reading landmarks instead of declaring the page complete after a
            # title-only scroll. This remains DOM/evidence-derived and works
            # for any product shape.
            original_candidates = list(candidates)
            candidates = list(heading_candidates)
            if len(heading_candidates) < 3:
                supplemental = [
                    item
                    for item in original_candidates
                    if item not in heading_candidates
                    and len(" ".join((item.text or item.name).split())) >= 24
                    and not (
                        item.tag == "a" and " ".join(item.name.split()).lower() in navigation_labels
                    )
                ]
                candidates.extend(supplemental[: max(0, 4 - len(candidates))])
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
    def _evidence_fallback(candidate, context: ProductContext) -> WorkflowProposal:
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
