"""Core plan() orchestration and capability insertion helpers."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from pydantic import ValidationError

from app.contracts.models import (
    ActionCapability,
    CertifiedDemoScript,
    DemoPlan,
    OperationKind,
    OutcomeSpec,
    Postcondition,
    ProductContext,
    SemanticOperation,
    Target,
    WorkflowProposal,
    WorkflowStep,
)
from app.planning.candidates import (
    build_page_complete_proposal,
    navigation_control_for_transition,
    select_candidate_flow,
    validate_flow_scope,
)
from app.planning.capabilities import (
    CapabilityCompilationError,
    compile_read_only_form_inspection,
    compile_record_creation,
)
from app.planning.capability_resolution import resolve_capabilities
from app.planning.production.shared import *
from app.planning.rehearsal import CapabilitySelectionError, select_rehearsal_capability
from app.planning.side_effects import (
    side_effect_decision,
)
from app.planning.synthetic import hydrate_operations
from app.providers.errors import ProviderError
from app.providers.interfaces import LLMProvider


def _certify_operations(
    operations: list[SemanticOperation],
    *,
    expected_outcomes: list[str],
    minimum_duration_seconds: int,
    target_duration_seconds: int,
    maximum_duration_seconds: int,
) -> CertifiedDemoScript:
    """Compile semantic steps into a stable, evidence-bearing outcome certificate."""
    predicate_by_kind = {
        OperationKind.NAVIGATE: "url",
        OperationKind.OPEN_NAVIGATION_ITEM: "url",
        OperationKind.FILL_TEXT: "value",
        OperationKind.FILL_EMAIL: "value",
        OperationKind.FILL_PHONE: "value",
        OperationKind.SELECT_OPTION: "value",
        OperationKind.SELECT_DATE: "value",
        OperationKind.SELECT_DATE_RANGE: "value",
        OperationKind.OPEN_MODAL: "visible",
        OperationKind.CLOSE_MODAL: "overlay_clear",
        OperationKind.SUBMIT: "state",
        OperationKind.CREATE_RECORD: "state",
        OperationKind.DRAG: "changed",
        OperationKind.POINTER_SEQUENCE: "changed",
        OperationKind.VERIFY_STATE: "state",
    }
    mutating = {OperationKind.SUBMIT, OperationKind.CREATE_RECORD}
    outcomes: list[OutcomeSpec] = []
    for operation in operations:
        if not operation.critical and not operation.postconditions:
            continue
        evidence = list(operation.evidence_refs)
        if operation.page_url:
            evidence.append(f"page:{operation.page_url}")
        if not evidence:
            evidence.append(f"operation:{operation.id}")
        outcomes.append(
            OutcomeSpec(
                id=f"outcome-{operation.id}",
                intent=operation.intent,
                success_predicate=predicate_by_kind.get(operation.kind, "visible"),
                evidence_refs=list(dict.fromkeys(evidence)),
                mutation_class=("authorized_mutation" if operation.kind in mutating else "read_only"),
                required=operation.critical,
                max_attempts=2 if operation.critical else 1,
            )
        )
    if not outcomes:
        outcomes.append(
            OutcomeSpec(
                id="outcome-opening-state",
                intent="Establish the requested product state for the viewer",
                success_predicate="visible",
                evidence_refs=["opening:state"],
            )
        )
    return CertifiedDemoScript(
        outcomes=outcomes,
        stop_conditions=[*expected_outcomes, "all required outcomes have verified evidence"],
        minimum_duration_seconds=minimum_duration_seconds,
        target_duration_seconds=target_duration_seconds,
        maximum_duration_seconds=maximum_duration_seconds,
    )


class PlanningCoreMixin:
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
        proposal = self._filter_unrequested_visual_gestures(proposal, objective)
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
        certified_script = _certify_operations(
            steps,
            expected_outcomes=proposal.expected_outcomes,
            minimum_duration_seconds=approved_minimum,
            target_duration_seconds=target_duration_seconds,
            maximum_duration_seconds=approved_maximum,
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
            certified_script=certified_script,
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

__all__ = [
    "PlanningCoreMixin",
]
