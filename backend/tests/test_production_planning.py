import pytest

from app.contracts.models import (
    ActionCapability,
    CandidateDemoFlow,
    FormField,
    FormSchema,
    ObjectiveSpec,
    ObservedElement,
    OperationKind,
    PageKnowledge,
    Postcondition,
    ProductContext,
    SemanticOperation,
    Target,
    WorkflowProposal,
)
from app.planning.candidates import (
    _is_control_chrome,
    build_page_complete_proposal,
    select_candidate_flow,
)
from app.planning.production import PlanningValidationError, ProductionPlanningService


class StubPlanner:
    def __init__(self, proposal: WorkflowProposal):
        self.proposal = proposal

    async def structured(self, prompt: str, schema):
        assert "Observed evidence" in prompt
        assert schema is WorkflowProposal
        return self.proposal


@pytest.mark.parametrize("label", ["Previous", "Next", "Page", "Pagination"])
def test_pagination_controls_are_not_promoted_to_reader_landmarks(label: str):
    assert _is_control_chrome(
        ObservedElement(tag="button", name=label, selector="button", actionable=True)
    )
    assert not _is_control_chrome(
        ObservedElement(tag="h3", name=f"{label} Internship", selector="h3")
    )


def test_consent_banner_controls_are_not_promoted_to_reader_landmarks():
    assert _is_control_chrome(
        ObservedElement(
            tag="button",
            name="Close and reject non-essential cookies",
            selector="button",
            actionable=True,
        )
    )
    assert _is_control_chrome(
        ObservedElement(
            tag="button",
            name="Necessary only",
            selector="button",
            actionable=True,
        )
    )
    assert not _is_control_chrome(
        ObservedElement(
            tag="h2",
            name="Cookie policy",
            selector="h2",
            actionable=False,
        )
    )
    assert _is_control_chrome(
        ObservedElement(
            tag="a",
            name="Skip to content",
            selector="a",
            actionable=True,
        )
    )


def test_placeholder_editor_heading_is_not_selected_as_story_landmark():
    """Template filler must not become a title-only editorial chapter."""
    root = "https://editor.example/"
    context = ProductContext(
        url=root,
        title="Untitled Diagram - editor",
        application_type="web_application",
        elements=[
            ObservedElement(
                tag="h1",
                name="Heading",
                text="Heading",
                selector="h1",
                source_url=root,
                actionable=True,
            ),
            ObservedElement(
                tag="svg", name="svg workspace", selector="svg", source_url=root, actionable=True
            ),
            ObservedElement(
                tag="input",
                name="Type / to search",
                selector="input",
                source_url=root,
                actionable=True,
            ),
        ],
        page_knowledge=[
            PageKnowledge(
                url=root,
                title="Untitled Diagram - editor",
                purpose="Heading",
                visible_sections=["Heading", "svg workspace"],
                scroll_landmarks=["Heading", "svg workspace"],
                visible_facts=[
                    "Heading :: Heading Lorem ipsum dolor sit amet, consectetur adipisicing elit."
                ],
                fingerprint="fixture",
            )
        ],
    )

    selected = ProductionPlanningService._page_exploration_targets(context, root, limit=8)

    assert "Heading" not in [item.name for item in selected]
    assert "svg workspace" in [item.name for item in selected]


def context() -> ProductContext:
    return ProductContext(
        url="https://example.test/app",
        title="Example CRM",
        application_type="crm",
        # Unit proposals below are intentionally provider-shaped snapshots,
        # not completed discovery. A non-empty route list would now correctly
        # require PageKnowledge before planning.
        relevant_routes=[],
        elements=[
            ObservedElement(tag="button", name="New lead", selector='[data-testid="new-lead"]')
        ],
        confidence=0.9,
    )


def test_page_compiler_does_not_turn_table_chrome_into_fake_scroll_scenes():
    """Inputs, filters, and column labels are workflow controls, not chapters."""
    root = "https://example.test/records"
    context = ProductContext(
        url=root,
        title="Example",
        application_type="dashboard",
        confidence=1,
        page_knowledge=[
            PageKnowledge(
                url=root,
                title="Records",
                purpose="Records",
                fingerprint="records",
                visible_sections=["Records"],
                visible_facts=[
                    "Records :: Review the current records and create an isolated example when authorised.",
                ],
            )
        ],
        elements=[
            ObservedElement(tag="h1", name="Records", selector="h1", source_url=root),
            ObservedElement(
                tag="input",
                name="Search records",
                selector="input",
                source_url=root,
                actionable=True,
            ),
            ObservedElement(
                tag="button",
                name="Reminders",
                selector="#reminders",
                source_url=root,
                actionable=True,
            ),
            ObservedElement(
                tag="th", name="CREATION DATE", selector="th:nth-child(1)", source_url=root
            ),
            ObservedElement(
                tag="th", name="RECORD NAME", selector="th:nth-child(2)", source_url=root
            ),
        ],
    )
    proposal = build_page_complete_proposal(
        context,
        CandidateDemoFlow(name="records", page_urls=[root], score=1),
    )
    local = [step for step in proposal.steps if step.page_url == root and step.target]
    assert [step.target.name for step in local] == ["Records", "Search records"]
    assert local[0].kind is OperationKind.VERIFY_STATE
    assert local[1].kind is OperationKind.VERIFY_STATE


def test_navigation_query_state_is_allowed_when_canonical_route_was_observed():
    """Filters and SPA state must not turn a visible same-origin route into a false rejection."""
    root = "https://example.test/app"
    scoped = ProductContext(
        url=root,
        title="Example",
        application_type="dashboard",
        relevant_routes=["https://example.test/leads"],
        navigation=[
            ObservedElement(
                tag="a",
                name="Leads",
                selector="a[href='/leads']",
                href="/leads",
                source_url=root,
                actionable=True,
            )
        ],
        elements=[ObservedElement(tag="h1", name="Leads", selector="h1", source_url=root)],
        confidence=0.9,
    )
    proposal = WorkflowProposal(
        narrative_goal="Show leads",
        selected_workflow="leads",
        steps=[
            SemanticOperation(
                kind=OperationKind.NAVIGATE,
                intent="Open filtered leads",
                value="/leads?status=open&page=1",
                postconditions=[
                    Postcondition(
                        kind="url", expected="https://example.test/leads?status=open&page=1"
                    )
                ],
            )
        ],
        expected_outcomes=["Leads are visible"],
    )
    ProductionPlanningService._validate(proposal, scoped, allow_side_effects=False)


def test_visible_navigation_accepts_query_state_postcondition_for_same_route():
    """A visible link may omit transient filters that the planner observes after navigation."""
    root = "https://example.test/app"
    scoped = ProductContext(
        url=root,
        title="Example",
        application_type="dashboard",
        confidence=0.9,
        navigation=[
            ObservedElement(
                tag="a",
                name="Leads",
                selector="a[href='/leads']",
                href="/leads",
                source_url=root,
                actionable=True,
            )
        ],
        elements=[
            # Same accessible name on another discovered page must not win
            # over the operation's source/selector provenance.
            ObservedElement(
                tag="a",
                name="Leads",
                selector="a[href='/leads']",
                href="/wrong",
                source_url="https://example.test/other",
                actionable=True,
            ),
            ObservedElement(
                tag="a",
                name="Leads",
                selector="a[href='/leads']",
                href="/leads",
                source_url=root,
                actionable=True,
            ),
        ],
        relevant_routes=["https://example.test/leads"],
    )
    proposal = WorkflowProposal(
        narrative_goal="Open filtered leads",
        selected_workflow="leads",
        steps=[
            SemanticOperation(
                kind=OperationKind.OPEN_NAVIGATION_ITEM,
                intent="Open filtered leads",
                target=Target(name="Leads", selector="a[href='/leads']", source_url=root),
                postconditions=[
                    Postcondition(kind="url", expected="https://example.test/leads?status=open")
                ],
            )
        ],
        expected_outcomes=["Leads are visible"],
    )
    ProductionPlanningService._validate(proposal, scoped, allow_side_effects=False)


def test_compile_navigation_preserves_query_route_and_regrounds_visible_control():
    """A query-bearing page must not be rewritten through a reused selector."""
    root = "https://example.test/dashboard"
    analytics = "https://example.test/analytics?datePreset=currentMonth"
    scoped = ProductContext(
        url=root,
        title="Example",
        application_type="dashboard",
        relevant_routes=["https://example.test/analytics"],
        navigation=[
            ObservedElement(
                tag="a",
                name="Analytics",
                selector="a",
                href="/analytics",
                source_url=root,
                actionable=True,
            )
        ],
        elements=[
            ObservedElement(
                tag="h1",
                name="No dashboards yet",
                selector="h1",
                source_url=analytics,
                actionable=False,
            )
        ],
        page_knowledge=[
            PageKnowledge(url=root, title="Dashboard", purpose="Dashboard", fingerprint="root"),
            PageKnowledge(
                url=analytics,
                title="Analytics",
                purpose="Analytics",
                visible_sections=["No dashboards yet"],
                scroll_landmarks=["No dashboards yet"],
                fingerprint="analytics",
                evidence_refs=["page:" + analytics],
            ),
        ],
        confidence=0.9,
    )
    proposal = WorkflowProposal(
        narrative_goal="Explain analytics",
        selected_workflow="analytics",
        steps=[
            SemanticOperation(
                kind=OperationKind.NAVIGATE,
                intent="Open analytics",
                value=analytics,
                postconditions=[Postcondition(kind="url", expected=analytics)],
            ),
            SemanticOperation(
                kind=OperationKind.SCROLL_TO,
                intent="Show the analytics state",
                target=Target(name="No dashboards yet", selector="h1", source_url=analytics),
                postconditions=[],
            ),
        ],
        expected_outcomes=["Analytics is visible"],
    )
    compiled = ProductionPlanningService._compile_navigation(proposal, scoped)
    transition = compiled.steps[0]
    assert transition.kind is OperationKind.OPEN_NAVIGATION_ITEM
    assert transition.target is not None
    assert transition.target.name == "Analytics"
    assert transition.postconditions[0].expected == analytics
    assert all(step.value != "https://example.test/forms" for step in compiled.steps)
    ProductionPlanningService._validate(compiled, scoped, allow_side_effects=False)


def test_compile_navigation_grounding_repairs_open_route_without_model_target():
    """A route-only model step is upgraded to the observed visible control."""
    root = "https://example.test/"
    destination = "https://example.test/templates.html"
    scoped = ProductContext(
        url=root,
        title="Example",
        application_type="web_application",
        relevant_routes=[destination],
        navigation=[
            ObservedElement(
                tag="a",
                name="Templates",
                selector="a[href='/templates.html']",
                href="/templates.html",
                source_url=root,
                actionable=True,
            )
        ],
        elements=[],
    )
    proposal = WorkflowProposal(
        narrative_goal="Open templates",
        selected_workflow="templates",
        steps=[
            SemanticOperation(
                kind=OperationKind.OPEN_NAVIGATION_ITEM,
                intent="Open templates",
                postconditions=[Postcondition(kind="url", expected=destination)],
            )
        ],
        expected_outcomes=["Templates are visible"],
    )

    compiled = ProductionPlanningService._compile_navigation(proposal, scoped)
    transition = compiled.steps[0]

    assert transition.kind is OperationKind.OPEN_NAVIGATION_ITEM
    assert transition.target is not None
    assert transition.target.name == "Templates"
    ProductionPlanningService._validate(compiled, scoped, allow_side_effects=False)


def test_compile_navigation_prefers_observed_href_over_stale_model_destination():
    root = "https://example.test/"
    observed = "https://example.test/docs/getting-started"
    stale = "https://example.test/getting-started"
    scoped = ProductContext(
        url=root,
        title="Docs",
        application_type="documentation",
        relevant_routes=[observed],
        navigation=[
            ObservedElement(
                tag="a",
                name="Getting started",
                selector="a.getting-started",
                href="/docs/getting-started",
                source_url=root,
                actionable=True,
            )
        ],
        confidence=0.9,
    )
    proposal = WorkflowProposal(
        narrative_goal="Explain the getting started guide",
        selected_workflow="docs",
        steps=[
            SemanticOperation(
                kind=OperationKind.NAVIGATE,
                intent="Open getting started",
                value=stale,
                postconditions=[Postcondition(kind="url", expected=stale)],
            )
        ],
        expected_outcomes=["The getting started guide is visible"],
    )
    compiled = ProductionPlanningService._compile_navigation(proposal, scoped)
    assert compiled.steps[0].kind is OperationKind.OPEN_NAVIGATION_ITEM
    assert compiled.steps[0].postconditions[0].expected == observed
    ProductionPlanningService._validate(compiled, scoped, allow_side_effects=False)


def test_planner_rejects_candidate_missing_required_objective_context_relationship():
    scoped = context().model_copy(
        update={
            "objective": ObjectiveSpec(
                raw="Explain Appointment Configuration -> Appointment workflow",
                primary_entity="appointment",
                supporting_relationships=[
                    {
                        "source": "Appointment Configuration",
                        "target": "Appointment workflow",
                    }
                ],
            ),
            "page_knowledge": [
                PageKnowledge(
                    url="https://example.test/appointments",
                    title="Appointment workflow",
                    purpose="Appointment workflow",
                    visible_sections=["Schedule appointments"],
                    fingerprint="appointments",
                )
            ],
        }
    )
    candidate = CandidateDemoFlow(
        name="appointments",
        page_urls=["https://example.test/appointments"],
        rationale=["feature"],
        expected_outcomes=["appointment"],
        evidence_coverage=["page"],
        score=0.8,
    )

    with pytest.raises(PlanningValidationError, match="required objective relationship"):
        ProductionPlanningService._validate_objective_grounding(scoped, candidate)


def test_authenticated_context_satisfies_login_must_show_requirement():
    """Login is proven by the credential boundary after discovery, not DOM copy."""
    root = "https://example.test/app"
    scoped = context().model_copy(
        update={
            "authentication_state": "authenticated",
            "objective": ObjectiveSpec(
                raw="Include login and show the records workspace",
                primary_entity="records",
                must_show=["login"],
            ),
            "page_knowledge": [
                PageKnowledge(
                    url=root,
                    title="Records",
                    purpose="Records workspace",
                    visible_sections=["Records"],
                    visible_facts=["Records :: Review the current records in one workspace."],
                    fingerprint="records",
                )
            ],
        }
    )
    candidate = CandidateDemoFlow(
        name="records",
        page_urls=[root],
        rationale=["workspace"],
        expected_outcomes=["records"],
        evidence_coverage=["page"],
        score=0.8,
    )
    ProductionPlanningService._validate_objective_grounding(scoped, candidate)


def test_planner_rejects_settings_shell_as_relationship_detail_evidence():
    scoped = context().model_copy(
        update={
            "objective": ObjectiveSpec(
                raw="Explain Lead Configuration in the context of Lead Management",
                primary_entity="lead",
                supporting_relationships=[
                    {
                        "source": "Lead Configuration",
                        "target": "Lead Management",
                    }
                ],
            ),
            "page_knowledge": [
                PageKnowledge(
                    url="https://example.test/leads",
                    title="Lead Management",
                    purpose="Lead workspace",
                    visible_sections=["Lead list"],
                    fingerprint="leads",
                ),
                PageKnowledge(
                    url="https://example.test/settings",
                    title="Settings",
                    purpose="Settings dashboard",
                    # A global shell may expose this label, but it is not the
                    # configuration page itself.
                    visible_sections=["Lead Configuration"],
                    fingerprint="settings",
                ),
            ],
        }
    )
    candidate = CandidateDemoFlow(
        name="lead context",
        page_urls=["https://example.test/leads", "https://example.test/settings"],
        rationale=["context"],
        expected_outcomes=["lead"],
        evidence_coverage=["page"],
        score=0.8,
    )
    with pytest.raises(PlanningValidationError, match="semantic detail page"):
        ProductionPlanningService._validate_objective_grounding(scoped, candidate)


def test_planner_accepts_a_semantic_relationship_when_workspace_uses_a_product_label():
    """Operational descriptions need not be copied verbatim from the UI.

    A common product convention is naming a workspace after the entity while
    callers ask for its "management" flow.  The selected detail pages prove
    the entity and the configuration role without relying on a route-specific
    exception.
    """
    scoped = context().model_copy(
        update={
            "objective": ObjectiveSpec(
                raw="Explain Customer Configuration in the context of Customer Management",
                primary_entity="customer management",
                supporting_relationships=[
                    {
                        "source": "Customer Configuration",
                        "target": "Customer Management",
                    }
                ],
            ),
            "page_knowledge": [
                PageKnowledge(
                    url="https://example.test/customers-v3",
                    title="Customers V3",
                    purpose="Customers",
                    visible_sections=["Customer list"],
                    fingerprint="customers",
                ),
                PageKnowledge(
                    url="https://example.test/settings/customer/configuration",
                    title="Settings",
                    purpose="Configuration",
                    visible_sections=["Customer Configuration"],
                    fingerprint="customer-config",
                ),
            ],
        }
    )
    candidate = CandidateDemoFlow(
        name="customer context",
        page_urls=[
            "https://example.test/customers-v3",
            "https://example.test/settings/customer/configuration",
        ],
        rationale=["context"],
        expected_outcomes=["customer"],
        evidence_coverage=["page"],
        score=0.8,
    )

    ProductionPlanningService._validate_objective_grounding(scoped, candidate)


def test_planner_accepts_editorial_must_show_phrases_when_page_evidence_is_terse():
    """Natural requests may describe visible concepts without exact UI labels."""
    root = "https://example.test/"
    timeline = "https://example.test/timeline"
    scoped = context().model_copy(
        update={
            "objective": ObjectiveSpec(
                raw="Walk through the portfolio",
                primary_entity="portfolio",
                must_show=[
                    "home identity and capabilities",
                    "every meaningful featured project on Home",
                ],
                supporting_relationships=[{"source": "Home", "target": "Timeline"}],
            ),
            "page_knowledge": [
                PageKnowledge(
                    url=root,
                    title="Jordan Lee | Product Engineer",
                    purpose="Portfolio overview",
                    visible_sections=["Core Capabilities", "Featured Projects"],
                    visible_facts=["Atlas is a scheduling workspace for distributed teams."],
                    fingerprint="home",
                ),
                PageKnowledge(
                    url=timeline,
                    title="Professional Timeline",
                    purpose="Career history",
                    visible_sections=["Experience"],
                    visible_facts=["Career progression and contributions."],
                    fingerprint="timeline",
                ),
            ],
        }
    )
    candidate = CandidateDemoFlow(
        name="portfolio walkthrough",
        page_urls=[root, timeline],
        score=0.9,
    )
    ProductionPlanningService._validate_objective_grounding(scoped, candidate)


def test_planner_still_rejects_an_entirely_unsupported_must_show_phrase():
    scoped = context().model_copy(
        update={
            "objective": ObjectiveSpec(
                raw="Show the product", must_show=["billing reconciliation"]
            ),
            "page_knowledge": [
                PageKnowledge(
                    url="https://example.test/",
                    title="Product",
                    purpose="Overview",
                    visible_sections=["Dashboard"],
                    fingerprint="home",
                )
            ],
        }
    )
    candidate = CandidateDemoFlow(name="overview", page_urls=["https://example.test/"], score=0.9)
    with pytest.raises(PlanningValidationError, match="must-show requirement"):
        ProductionPlanningService._validate_objective_grounding(scoped, candidate)


def test_full_walkthrough_treats_explanatory_product_purpose_as_story_intent():
    """A model must-show sentence about product purpose is not a literal label."""
    root = "https://example.test/"
    scoped = context().model_copy(
        update={
            "objective": ObjectiveSpec(
                raw="Create a complete walkthrough. Explain what the product helps a learner plan and track.",
                demo_type="full_walkthrough",
                primary_entity="learner",
                must_show=["what the product helps a learner plan and track"],
            ),
            "page_knowledge": [
                PageKnowledge(
                    url=root,
                    title="Study Planner",
                    purpose="Plan study work and track progress",
                    visible_sections=["Today", "Progress"],
                    fingerprint="study-planner",
                ),
                PageKnowledge(
                    url="https://example.test/progress",
                    title="Progress",
                    purpose="Track progress",
                    visible_sections=["Progress"],
                    fingerprint="progress",
                ),
            ],
        }
    )
    candidate = CandidateDemoFlow(
        name="complete walkthrough", page_urls=[root, "https://example.test/progress"], score=0.9
    )
    ProductionPlanningService._validate_objective_grounding(scoped, candidate)


def test_planner_accepts_generic_editorial_descriptor_for_unfamiliar_canvas_app():
    """Model-added surface words must not reject a grounded new product."""
    root = "https://app.diagrams.net/"
    scoped = context().model_copy(
        update={
            "url": root,
            "title": "Untitled Diagram - draw.io",
            "objective": ObjectiveSpec(
                raw="Explore the public diagram editor and demonstrate a safe drawing interaction",
                primary_entity="public diagram editor",
                must_show=["canvas", "toolbar"],
            ),
            "page_knowledge": [
                PageKnowledge(
                    url=root,
                    title="Untitled Diagram - draw.io",
                    purpose="Diagram canvas",
                    visible_sections=["Canvas", "Toolbar"],
                    actionable_controls=["File", "Edit", "Arrange"],
                    visible_facts=["Drag elements here to create a diagram."],
                    fingerprint="drawio",
                )
            ],
        }
    )
    candidate = CandidateDemoFlow(
        name="diagram editor walkthrough",
        page_urls=[root],
        score=0.85,
        evidence_coverage=["canvas", "toolbar"],
    )
    ProductionPlanningService._validate_objective_grounding(scoped, candidate)


def test_planner_rejects_one_configuration_page_as_proof_of_both_relationship_sides():
    scoped = context().model_copy(
        update={
            "objective": ObjectiveSpec(
                raw="Explain Customer Configuration in the context of Customer Management",
                primary_entity="customer management",
                supporting_relationships=[
                    {
                        "source": "Customer Configuration",
                        "target": "Customer Management",
                    }
                ],
            ),
            "page_knowledge": [
                PageKnowledge(
                    url="https://example.test/settings/customer/configuration",
                    title="Customer Config",
                    purpose="Customer configuration",
                    fingerprint="customer-config",
                ),
            ],
        }
    )
    candidate = CandidateDemoFlow(
        name="configuration only",
        page_urls=["https://example.test/settings/customer/configuration"],
        rationale=["context"],
        expected_outcomes=["customer"],
        evidence_coverage=["page"],
        score=0.8,
    )

    with pytest.raises(PlanningValidationError, match="semantic detail page"):
        ProductionPlanningService._validate_objective_grounding(scoped, candidate)


def test_planner_grounds_context_from_exploration_without_turning_it_into_a_video_page():
    workspace = "https://example.test/customers-v3"
    configuration = "https://example.test/settings/customer/configuration"
    scoped = context().model_copy(
        update={
            "objective": ObjectiveSpec(
                raw="Explain Customer Management in the context of Customer Configuration",
                primary_entity="customer management",
                supporting_relationships=[
                    {
                        "source": "Customer Configuration",
                        "target": "Customer Management",
                    }
                ],
            ),
            "page_knowledge": [
                PageKnowledge(
                    url=workspace,
                    title="Customers",
                    purpose="Customer workspace",
                    fingerprint="customers",
                ),
                PageKnowledge(
                    url=configuration,
                    title="Configuration",
                    purpose="Customer configuration",
                    fingerprint="configuration",
                ),
            ],
        }
    )
    candidate = CandidateDemoFlow(
        name="customer walkthrough",
        page_urls=[workspace],
        supporting_page_urls=[configuration],
        rationale=["context"],
        expected_outcomes=["customer"],
        evidence_coverage=["page"],
        score=0.8,
    )

    ProductionPlanningService._validate_objective_grounding(scoped, candidate)


def test_verified_form_transition_returns_to_the_capability_page_before_opening_it():
    leads = "https://example.test/leads"
    configuration = "https://example.test/settings/lead-config"
    context_with_navigation = ProductContext(
        url=leads,
        title="Example",
        application_type="crm",
        navigation=[
            ObservedElement(
                tag="a",
                name="Leads",
                selector="#leads",
                href="/leads",
                source_url=configuration,
                actionable=True,
                navigation_scope="primary",
            )
        ],
    )
    proposal = WorkflowProposal(
        narrative_goal="context first",
        selected_workflow="lead context",
        expected_outcomes=["configuration"],
        steps=[
            SemanticOperation(
                kind=OperationKind.NAVIGATE,
                intent="Open configuration",
                value=configuration,
                postconditions=[Postcondition(kind="url", expected=configuration)],
                page_url=configuration,
            )
        ],
    )
    transition = ProductionPlanningService._transition_to_capability_source(
        proposal, context_with_navigation, leads
    )
    assert len(transition) == 1
    assert transition[0].kind is OperationKind.OPEN_NAVIGATION_ITEM
    assert transition[0].target and transition[0].target.name == "Leads"
    assert transition[0].postconditions[0].expected == leads


def test_capability_steps_are_inserted_before_leaving_their_opening_page():
    root = "https://example.test/app"
    proposal = WorkflowProposal(
        narrative_goal="context first",
        selected_workflow="walkthrough",
        expected_outcomes=["context"],
        steps=[
            SemanticOperation(
                kind=OperationKind.NAVIGATE,
                intent="Open app",
                value=root,
                postconditions=[Postcondition(kind="url", expected=root)],
                page_url=root,
            ),
            SemanticOperation(
                kind=OperationKind.OPEN_NAVIGATION_ITEM,
                intent="Open details",
                target=Target(name="Details", selector="#details", source_url=root),
                postconditions=[Postcondition(kind="url", expected=f"{root}/details")],
                page_url=f"{root}/details",
            ),
        ],
    )
    extra = SemanticOperation(
        kind=OperationKind.OPEN_MODAL,
        intent="Open observed form",
        target=Target(name="Create", selector="#create", source_url=root),
        page_url=root,
    )
    context_with_root = ProductContext(url=root, title="Example", application_type="app")

    inserted = ProductionPlanningService._insert_at_capability_source(
        proposal, context_with_root, root, [extra]
    )
    assert [step.kind for step in inserted.steps] == [
        OperationKind.NAVIGATE,
        OperationKind.OPEN_MODAL,
        OperationKind.OPEN_NAVIGATION_ITEM,
    ]


@pytest.mark.asyncio
async def test_authorized_generic_record_creation_requires_and_compiles_verified_capability():
    entry = Target(
        name="Create record", selector="#create-record", source_url="https://example.test/app"
    )
    capability = ActionCapability(
        kind="form",
        purpose="record",
        source_url="https://example.test/app",
        entry_target=entry,
        form_schema=FormSchema(
            source_url="https://example.test/app",
            fields=[
                FormField(
                    name="Customer email", selector="#email", control_type="email", required=True
                ),
            ],
        ),
        submit_target=Target(
            name="Save record", selector="#save", source_url="https://example.test/app"
        ),
        outcome_target=Target(
            name="Record created", selector="#created", source_url="https://example.test/app"
        ),
        outcome_evidence=["visible confirmation"],
        verified=True,
    )
    proposal = WorkflowProposal(
        narrative_goal="Show the record workflow",
        selected_workflow="record",
        steps=[
            SemanticOperation(
                kind=OperationKind.CLICK,
                intent="Establish record workspace",
                target=entry,
                postconditions=[Postcondition(kind="visible", expected=True, target=entry)],
            )
        ],
        expected_outcomes=["record workspace"],
    )
    scoped = context().model_copy(
        update={
            "elements": [
                ObservedElement(
                    tag="button",
                    name="Create record",
                    selector="#create-record",
                    source_url="https://example.test/app",
                )
            ],
            "objective": ObjectiveSpec(
                raw="Create an isolated demo record", permitted_mutations=["create_isolated_record"]
            ),
            "capabilities": [capability.model_dump(mode="json")],
        }
    )
    plan = await ProductionPlanningService(StubPlanner(proposal)).plan(
        objective="Create an isolated demo record",
        context=scoped,
        allow_external_side_effects=True,
    )
    kinds = [step.operation.kind for step in plan.workflow_steps]
    assert kinds[-3:] == [OperationKind.OPEN_MODAL, OperationKind.FILL_EMAIL, OperationKind.SUBMIT]
    assert plan.workflow_steps[-1].operation.postconditions[0].target.name == "Record created"
    assert plan.workflow_steps[-2].operation.postconditions[0].expected.endswith(".test")


@pytest.mark.asyncio
async def test_authorized_creation_rejects_a_form_without_independent_outcome_proof():
    proposal = WorkflowProposal(
        narrative_goal="Show records",
        selected_workflow="record",
        steps=[
            SemanticOperation(
                kind=OperationKind.CLICK,
                intent="Open records",
                target=Target(name="Create record", selector="#create-record"),
                postconditions=[
                    Postcondition(
                        kind="visible",
                        expected=True,
                        target=Target(name="Create record", selector="#create-record"),
                    )
                ],
            )
        ],
        expected_outcomes=["records"],
    )
    scoped = context().model_copy(
        update={
            "elements": [
                ObservedElement(tag="button", name="Create record", selector="#create-record")
            ],
            "objective": ObjectiveSpec(
                raw="Create a demo record", permitted_mutations=["create_isolated_record"]
            ),
            "capabilities": [
                ActionCapability(
                    kind="form",
                    purpose="record",
                    source_url="https://example.test/app",
                    entry_target=Target(name="Create record", selector="#create-record"),
                    form_schema=FormSchema(
                        source_url="https://example.test/app",
                        fields=[FormField(name="Name", selector="#name", control_type="text")],
                    ),
                    submit_target=Target(name="Save", selector="#save"),
                    verified=True,
                ).model_dump(mode="json")
            ],
        }
    )
    with pytest.raises(PlanningValidationError, match="independently verified form capability"):
        await ProductionPlanningService(StubPlanner(proposal)).plan(
            objective="Create a demo record",
            context=scoped,
            allow_external_side_effects=True,
        )


@pytest.mark.asyncio
async def test_authorized_creation_uses_the_selected_feature_capability_not_first_probe():
    lead_url = "https://example.test/leads"
    template_url = "https://example.test/lead-templates"
    lead = ActionCapability(
        kind="form",
        purpose="New lead",
        source_url=lead_url,
        entry_target=Target(name="New lead", selector="#new-lead", source_url=lead_url),
        form_schema=FormSchema(
            source_url=lead_url,
            fields=[
                FormField(name="Name", selector='[name="name"]', control_type="text", required=True)
            ],
        ),
        submit_target=Target(name="Save lead", selector="#save-lead", source_url=lead_url),
        outcome_target=Target(name="Lead created", selector="#lead-created", source_url=lead_url),
        outcome_evidence=["visible lead confirmation"],
        verified=True,
    )
    template = ActionCapability(
        kind="form",
        purpose="New template",
        source_url=template_url,
        entry_target=Target(name="New template", selector="#new-template", source_url=template_url),
        form_schema=FormSchema(
            source_url=template_url,
            fields=[
                FormField(
                    name="Template name",
                    selector='[name="template"]',
                    control_type="text",
                    required=True,
                )
            ],
        ),
        submit_target=Target(
            name="Save template", selector="#save-template", source_url=template_url
        ),
        outcome_target=Target(
            name="Template created", selector="#template-created", source_url=template_url
        ),
        outcome_evidence=["visible template confirmation"],
        verified=True,
    )
    proposal = WorkflowProposal(
        narrative_goal="Show lead management",
        selected_workflow="lead management",
        steps=[
            SemanticOperation(
                kind=OperationKind.CLICK, intent="Establish leads", target=lead.entry_target
            )
        ],
        expected_outcomes=["lead workspace"],
    )
    scoped = ProductContext(
        url=lead_url,
        title="Example",
        application_type="web_application",
        objective=ObjectiveSpec(
            raw="Create an isolated lead management record",
            primary_entity="lead management",
            permitted_mutations=["create_isolated_record"],
        ),
        elements=[
            ObservedElement(
                tag="button", name="New lead", selector="#new-lead", source_url=lead_url
            )
        ],
        page_knowledge=[
            PageKnowledge(
                url=lead_url,
                title="Leads",
                purpose="Lead management",
                visible_sections=["Lead list and details"],
                fingerprint="leads",
            ),
        ],
        candidate_demo_flows=[
            CandidateDemoFlow(
                name="lead management",
                page_urls=[lead_url],
                rationale=["requested feature"],
                expected_outcomes=["lead workflow"],
                evidence_coverage=["lead page"],
                score=0.95,
            )
        ],
        capabilities=[template.model_dump(mode="json"), lead.model_dump(mode="json")],
    )
    plan = await ProductionPlanningService(StubPlanner(proposal)).plan(
        objective="Create an isolated lead management record",
        context=scoped,
        allow_external_side_effects=True,
    )
    assert plan.workflow_steps[-1].operation.target.name == "Save lead"


@pytest.mark.asyncio
async def test_production_planner_only_accepts_observed_targets():
    proposal = WorkflowProposal(
        narrative_goal="Show lead creation",
        selected_workflow="lead creation",
        steps=[
            SemanticOperation(
                kind=OperationKind.CLICK,
                intent="Open lead form",
                target=Target(name="New lead", selector='[data-testid="new-lead"]'),
                postconditions=[
                    Postcondition(
                        kind="visible",
                        expected=True,
                        target=Target(name="New lead", selector='[data-testid="new-lead"]'),
                    )
                ],
            )
        ],
        expected_outcomes=["Lead form is visible"],
    )
    plan = await ProductionPlanningService(StubPlanner(proposal)).plan(
        objective="Show lead creation", context=context()
    )
    assert plan.workflow_steps[0].operation.target.name == "New lead"
    assert plan.workflow_steps[0].operation.target.test_id == "new-lead"


@pytest.mark.asyncio
async def test_production_planner_rejects_unapproved_submit():
    proposal = WorkflowProposal(
        narrative_goal="Create a lead",
        selected_workflow="lead creation",
        steps=[
            SemanticOperation(
                kind=OperationKind.SUBMIT,
                intent="Submit",
                target=Target(name="New lead", selector='[data-testid="new-lead"]'),
            )
        ],
        expected_outcomes=["Lead exists"],
    )
    with pytest.raises(PlanningValidationError, match="not authorized"):
        await ProductionPlanningService(StubPlanner(proposal)).plan(
            objective="Create a lead", context=context()
        )


@pytest.mark.asyncio
async def test_production_planner_retains_requested_audience_and_duration():
    proposal = WorkflowProposal(
        narrative_goal="Show lead area",
        selected_workflow="leads",
        steps=[
            SemanticOperation(
                kind=OperationKind.NAVIGATE,
                intent="Open leads",
                value="https://example.test/leads",
                postconditions=[Postcondition(kind="url", expected="https://example.test/leads")],
            )
        ],
        expected_outcomes=["Leads are visible"],
    )
    plan = await ProductionPlanningService(StubPlanner(proposal)).plan(
        objective="Show lead area",
        context=context(),
        audience="sales manager",
        target_duration_seconds=150,
    )
    assert (plan.audience, plan.target_duration_seconds) == ("sales manager", 150)
    assert (plan.minimum_duration_seconds, plan.maximum_duration_seconds) == (105, 210)


@pytest.mark.asyncio
async def test_planner_rejects_a_sparse_evidence_candidate_before_production_capture():
    records = "https://example.test/records"
    scoped = ProductContext(
        url=records,
        title="Example",
        application_type="dashboard",
        objective=ObjectiveSpec(
            raw="Create a one minute walkthrough of records",
            primary_entity="records",
            minimum_duration_seconds=60,
            target_duration_seconds=90,
            maximum_duration_seconds=120,
        ),
        page_knowledge=[
            PageKnowledge(
                url=records,
                title="Records",
                purpose="Records",
                visible_sections=["Records"],
                fingerprint="records",
            )
        ],
        candidate_demo_flows=[
            CandidateDemoFlow(
                name="sparse records",
                page_urls=[records],
                expected_outcomes=["Records"],
                estimated_duration_seconds=20,
                evidence_coverage=["page:https://example.test/records"],
                score=0.9,
            )
        ],
    )
    proposal = WorkflowProposal(
        narrative_goal="Show records",
        selected_workflow="records",
        steps=[
            SemanticOperation(
                kind=OperationKind.VERIFY_STATE,
                intent="Establish records",
                target=Target(name="Records"),
            )
        ],
        expected_outcomes=["Records"],
    )

    with pytest.raises(PlanningValidationError, match="lacks enough evidence-backed content"):
        await ProductionPlanningService(StubPlanner(proposal)).plan(
            objective=scoped.objective.raw,
            context=scoped,
            target_duration_seconds=90,
            minimum_duration_seconds=60,
            maximum_duration_seconds=120,
        )


@pytest.mark.asyncio
async def test_full_walkthrough_objective_preserves_its_thorough_duration_contract():
    proposal = WorkflowProposal(
        narrative_goal="Show the product",
        selected_workflow="overview",
        steps=[
            SemanticOperation(
                kind=OperationKind.NAVIGATE,
                intent="Open product",
                value="https://example.test/app",
                postconditions=[Postcondition(kind="url", expected="https://example.test/app")],
            )
        ],
        expected_outcomes=["Product is visible"],
    )
    scoped = context().model_copy(
        update={
            "objective": ObjectiveSpec(
                raw="Create a full walkthrough",
                demo_type="full_walkthrough",
                depth="thorough",
                minimum_duration_seconds=110,
                target_duration_seconds=180,
                maximum_duration_seconds=240,
            )
        }
    )
    plan = await ProductionPlanningService(StubPlanner(proposal)).plan(
        objective="Create a full walkthrough", context=scoped
    )
    assert (
        plan.minimum_duration_seconds,
        plan.target_duration_seconds,
        plan.maximum_duration_seconds,
    ) == (110, 180, 240)


@pytest.mark.asyncio
async def test_persisted_candidate_summary_is_reconciled_to_compiled_operations():
    root = "https://example.test/records"
    scoped = ProductContext(
        url=root,
        title="Example",
        application_type="dashboard",
        objective=ObjectiveSpec(raw="Show records", primary_entity="records"),
        page_knowledge=[
            PageKnowledge(
                url=root,
                title="Records",
                purpose="Records workspace",
                visible_sections=["Records"],
                visible_facts=["Records are visible."],
                fingerprint="records",
            )
        ],
        candidate_demo_flows=[
            CandidateDemoFlow(
                name="stale provider flow",
                page_urls=[root],
                expected_outcomes=["old outcome"],
                evidence_coverage=["old evidence"],
                score=0.9,
            )
        ],
        elements=[ObservedElement(tag="h1", name="Records", selector="h1", source_url=root)],
    )
    proposal = WorkflowProposal(
        narrative_goal="Show records",
        selected_workflow="records evidence",
        steps=[
            SemanticOperation(
                kind=OperationKind.VERIFY_STATE,
                intent="Establish records",
                target=Target(name="Records", source_url=root),
                evidence_refs=["page:records"],
            )
        ],
        expected_outcomes=["Records are visible"],
    )
    plan = await ProductionPlanningService(StubPlanner(proposal)).plan(
        objective="Show records",
        context=scoped,
        target_duration_seconds=90,
    )
    selected = plan.synthetic_data_plan["selected_candidate_flow"]
    assert selected["name"] == plan.selected_workflow
    assert selected["expected_outcomes"] == plan.expected_outcomes
    assert selected["semantic_steps"]
    assert any(
        ref.startswith("page:https://example.test/records") for ref in selected["evidence_coverage"]
    )


@pytest.mark.asyncio
async def test_production_planner_derives_select_value_only_from_observed_options():
    select = ObservedElement(
        tag="select", name="Region", selector="#region", options=["", "India", "United States"]
    )
    proposal = WorkflowProposal(
        narrative_goal="Set a region",
        selected_workflow="configuration",
        steps=[
            SemanticOperation(
                kind=OperationKind.SELECT_OPTION,
                intent="Choose a region",
                target=Target(name="Region", selector="#region"),
                postconditions=[
                    Postcondition(
                        kind="visible",
                        expected=True,
                        target=Target(name="Region", selector="#region"),
                    )
                ],
            )
        ],
        expected_outcomes=["Region is selected"],
    )
    scoped_context = context().model_copy(update={"elements": [select]})
    plan = await ProductionPlanningService(StubPlanner(proposal)).plan(
        objective="Set a region", context=scoped_context
    )
    operation = plan.workflow_steps[0].operation
    assert operation.value == "India"
    assert any(
        condition.kind == "value" and condition.expected == "India"
        for condition in operation.postconditions
    )


@pytest.mark.asyncio
async def test_full_walkthrough_covers_each_safe_primary_tab_once():
    local_content = [
        ObservedElement(
            tag="h2",
            name="Today overview",
            selector="#today-overview",
            source_url="https://example.test/today",
            text="Today highlights the current lesson and recommended next step.",
        ),
        ObservedElement(
            tag="h2",
            name="Progress summary",
            selector="#progress-summary",
            source_url="https://example.test/progress",
            text="Progress shows completed work and learning momentum.",
        ),
    ]
    scoped = context().model_copy(
        update={
            "relevant_routes": ["https://example.test/today", "https://example.test/progress"],
            "elements": [*context().elements, *local_content],
            "page_knowledge": [
                PageKnowledge(
                    url="https://example.test/today",
                    title="Today",
                    purpose="Today plan",
                    visible_sections=["Today overview"],
                    scroll_landmarks=["Today overview"],
                    fingerprint="today",
                ),
                PageKnowledge(
                    url="https://example.test/progress",
                    title="Progress",
                    purpose="Progress",
                    visible_sections=["Progress summary"],
                    scroll_landmarks=["Progress summary"],
                    fingerprint="progress",
                ),
            ],
            "navigation": [
                ObservedElement(
                    tag="a",
                    name="Today",
                    selector="a",
                    href="/today",
                    source_url="https://example.test/app",
                ),
                ObservedElement(
                    tag="a",
                    name="Progress",
                    selector="a",
                    href="/progress",
                    source_url="https://example.test/app",
                ),
                ObservedElement(
                    tag="a",
                    name="Docs",
                    selector="a",
                    href="https://outside.test/docs",
                    source_url="https://example.test/app",
                ),
            ],
        }
    )
    fallback = WorkflowProposal(
        narrative_goal="fallback",
        selected_workflow="fallback",
        steps=[
            SemanticOperation(
                kind=OperationKind.NAVIGATE,
                intent="Open",
                value="https://example.test/app",
                postconditions=[Postcondition(kind="url", expected="https://example.test/app")],
            )
        ],
        expected_outcomes=["opened"],
    )
    plan = await ProductionPlanningService(StubPlanner(fallback)).plan(
        objective="Create a full walkthrough of each tab", context=scoped
    )
    assert plan.workflow_steps[0].operation.value == "https://example.test/app"
    operations = [step.operation for step in plan.workflow_steps[1:]]
    first_navigation = next(
        index
        for index, operation in enumerate(operations)
        if operation.kind is OperationKind.OPEN_NAVIGATION_ITEM
    )
    assert all(
        operation.kind is OperationKind.SCROLL_TO for operation in operations[:first_navigation]
    )
    # A page is no longer considered covered by one route click alone. A
    # single-landmark page may satisfy adjacent duties in its one readable
    # local scroll; richer pages receive one scene per required content group.
    assert [
        operation.target.name
        for operation in operations
        if operation.kind is OperationKind.OPEN_NAVIGATION_ITEM
    ] == ["Today", "Progress"]
    for page, section in (
        ("https://example.test/today", "Today overview"),
        ("https://example.test/progress", "Progress summary"),
    ):
        local = [operation for operation in operations if operation.page_url == page]
        assert len(local) >= 2
        covered = {phase for operation in local for phase in operation.page_contract_phases}
        assert covered >= {"establish", "explore", "explain", "demonstrate", "verify"}
        assert any(operation.target and operation.target.name == section for operation in local)
    # Page-local reading targets retain their discovered selectors.  A later
    # grounding pass must not replace a same-named heading with the navbar link.
    assert operations[first_navigation + 1].target.selector == "#today-overview"


def test_page_complete_candidate_compilation_replaces_route_sweep_budgeting():
    root = "https://example.test/"
    navigation = [
        ObservedElement(
            tag="a",
            name=f"Area {index}",
            selector=f"#area-{index}",
            href=f"/area-{index}",
            source_url=root,
            navigation_scope="primary",
        )
        for index in range(4)
    ]
    elements = [
        ObservedElement(
            tag="h2", name=f"Opening section {index}", selector=f"#opening-{index}", source_url=root
        )
        for index in range(12)
    ]
    for index in range(4):
        page_url = f"https://example.test/area-{index}"
        elements.extend(
            ObservedElement(
                tag="h2",
                name=f"Area {index} section {section}",
                selector=f"#area-{index}-{section}",
                source_url=page_url,
            )
            for section in range(12)
        )
    scoped = context().model_copy(
        update={"url": root, "elements": elements, "navigation": navigation}
    )
    candidate = select_candidate_flow(scoped, "Create a full walkthrough")
    assert candidate is not None
    proposal = build_page_complete_proposal(scoped, candidate)
    page_steps = {
        page_url: [
            step for step in proposal.steps if step.page_url.rstrip("/") == page_url.rstrip("/")
        ]
        for page_url in [root, *(f"https://example.test/area-{index}" for index in range(4))]
    }
    # Candidate compilation produces the full editorial contract per selected
    # page; bounded route/heading coverage is not an accepted substitute.
    assert all(
        {phase for step in steps for phase in step.page_contract_phases}
        >= {"establish", "explore", "explain", "demonstrate", "verify"}
        for steps in page_steps.values()
    )


def test_complete_home_keeps_all_meaningful_project_and_outcome_landmarks():
    """A rich opening page must not silently lose late projects to a small cap."""
    root = "https://example.test/"
    names = [
        "Identity",
        "Capabilities",
        "Realtime Chat",
        "PromptRouter",
        "CalmPrep",
        "CookPlan AI",
        "No-show reduction",
        "Dashboard speedup",
        "Revenue growth",
        "Processing throughput",
        "Architecture",
    ]
    scoped = context().model_copy(
        update={
            "url": root,
            "page_knowledge": [
                PageKnowledge(
                    url=root,
                    title="Example",
                    purpose="Example product",
                    scroll_landmarks=names,
                    visible_facts=[
                        f"{name} :: {name} is an observed product capability or outcome with useful context."
                        for name in names
                    ],
                    fingerprint="example-rich",
                )
            ],
            "elements": [
                ObservedElement(
                    tag="h2" if index < 2 else "h3",
                    name=name,
                    selector=f"#{index}",
                    source_url=root,
                    text=f"{name} is an observed product capability or outcome with useful context.",
                )
                for index, name in enumerate(names)
            ],
        }
    )
    candidate = select_candidate_flow(scoped, "Create a complete walkthrough")
    assert candidate is not None
    proposal = build_page_complete_proposal(scoped, candidate)
    root_steps = [step for step in proposal.steps if step.page_url == root]
    selected_evidence = {ref for step in root_steps for ref in step.evidence_refs}
    # Several adjacent cards may share one smooth scroll beat, but every
    # meaningful landmark must remain in that beat's immutable evidence.
    assert all(f"element:{name}" in selected_evidence for name in names)


def test_full_walkthrough_keeps_each_distinct_role_on_a_multi_role_page():
    root = "https://example.test/"
    timeline = "https://example.test/timeline"
    scoped = context().model_copy(
        update={
            "url": root,
            "page_knowledge": [
                PageKnowledge(
                    url=root,
                    title="Home",
                    purpose="Home",
                    visible_sections=["Home"],
                    fingerprint="home",
                ),
                PageKnowledge(
                    url=timeline,
                    title="Timeline",
                    purpose="Professional History",
                    scroll_landmarks=[
                        "Professional History",
                        "Current Role",
                        "Previous Internship",
                        "Education",
                    ],
                    visible_sections=[
                        "Professional History",
                        "Current Role",
                        "Previous Internship",
                        "Education",
                    ],
                    visible_facts=[
                        "Current Role :: I lead platform engineering with measurable reliability improvements.",
                        "Previous Internship :: I built workflow automation interfaces and improved query performance.",
                        "Education :: Computer science foundations.",
                    ],
                    fingerprint="timeline",
                ),
            ],
            "elements": [
                ObservedElement(
                    tag="a",
                    name="Timeline",
                    selector="#timeline",
                    href="/timeline",
                    source_url=root,
                    actionable=True,
                    navigation_scope="primary",
                ),
                *[
                    ObservedElement(
                        tag="h2", name=name, selector=f"#{index}", source_url=timeline, text=name
                    )
                    for index, name in enumerate(
                        ["Professional History", "Current Role", "Previous Internship", "Education"]
                    )
                ],
            ],
        }
    )
    candidate = CandidateDemoFlow(
        name="complete evidence walkthrough", page_urls=[root, timeline], score=1
    )
    proposal = build_page_complete_proposal(scoped, candidate)
    timeline_steps = [step for step in proposal.steps if step.page_url == timeline]
    evidence = {ref for step in timeline_steps for ref in step.evidence_refs}
    assert any("Previous Internship" in ref for ref in evidence)


def test_repeated_numbered_series_keeps_one_representative_detail():
    root = "https://example.test/"
    scoped = context().model_copy(
        update={
            "url": root,
            "elements": [
                ObservedElement(tag="h2", name="Weeks", selector="#weeks", source_url=root),
                *[
                    ObservedElement(
                        tag="h3", name=f"Week {index}", selector=f"#week-{index}", source_url=root
                    )
                    for index in range(1, 5)
                ],
            ],
        }
    )
    selected = ProductionPlanningService._page_exploration_targets(scoped, root, limit=8)
    assert [item.name for item in selected].count("Weeks") == 1
    assert [item.name for item in selected if item.name.startswith("Week ")] == ["Week 1"]


def test_focused_page_compilation_does_not_turn_repeated_cards_into_route_sweep():
    root = "https://example.test/"
    page = PageKnowledge(
        url=root,
        title="Store",
        purpose="catalog",
        scroll_landmarks=["Catalog", "Item One", "Item Two", "Item Three", "Item Four"],
        visible_facts=["The catalog presents items with descriptions for comparison."],
        evidence_refs=["page:store"],
        fingerprint="store",
    )
    elements = [
        ObservedElement(tag="h2", name=name, selector=f"#{index}", source_url=root)
        for index, name in enumerate(page.scroll_landmarks)
    ]
    scoped = ProductContext(
        url=root,
        title="Store",
        application_type="web_application",
        page_knowledge=[page],
        elements=elements,
        objective=ObjectiveSpec(
            raw="Show a focused catalog walkthrough",
            demo_type="feature_walkthrough",
            primary_entity="catalog",
        ),
    )
    candidate = CandidateDemoFlow(
        name="catalog", page_urls=[root], estimated_duration_seconds=90, score=1
    )
    proposal = build_page_complete_proposal(scoped, candidate)
    local = [
        step
        for step in proposal.steps
        if step.page_url == root and step.kind.value in {"ScrollTo", "VerifyState"}
    ]
    assert len(local) <= 2


def test_home_featured_collection_is_selected_before_generic_sections():
    root = "https://example.test/"
    projects = [
        "Realtime Chat",
        "Knowledge Hub",
        "Prompt Router",
        "Interview Coach",
        "Meal Planner",
    ]
    scoped = context().model_copy(
        update={
            "url": root,
            "elements": [
                ObservedElement(tag="h1", name="Alex Example", selector="#home", source_url=root),
                ObservedElement(
                    tag="h2", name="Capabilities", selector="#capabilities", source_url=root
                ),
                ObservedElement(
                    tag="h3", name="Reliability", selector="#reliability", source_url=root
                ),
                ObservedElement(
                    tag="h3", name="Performance", selector="#performance", source_url=root
                ),
                ObservedElement(
                    tag="h2", name="Featured systems", selector="#projects", source_url=root
                ),
                *[
                    ObservedElement(
                        tag="h3", name=name, selector=f"#project-{index}", source_url=root
                    )
                    for index, name in enumerate(projects)
                ],
                ObservedElement(
                    tag="h2", name="Impact metrics", selector="#metrics", source_url=root
                ),
            ],
        }
    )
    selected = ProductionPlanningService._page_exploration_targets(scoped, root, limit=7)

    assert [item.name for item in selected][:2] == ["Alex Example", "Featured systems"]
    assert [item.name for item in selected][2:] == projects


@pytest.mark.asyncio
async def test_page_local_heading_keeps_its_discovery_provenance():
    home = ObservedElement(
        tag="h2",
        name="Engineering Notes",
        selector="h2",
        source_url="https://example.test/",
        text="Home summary",
    )
    notes = ObservedElement(
        tag="h1",
        name="Engineering Notes",
        selector="h1",
        source_url="https://example.test/notes",
        text="Notes on resilient systems",
    )
    scoped = context().model_copy(update={"elements": [home, notes]})
    proposal = WorkflowProposal(
        narrative_goal="notes",
        selected_workflow="notes",
        expected_outcomes=["notes"],
        steps=[
            SemanticOperation(
                kind=OperationKind.SCROLL_TO,
                intent="Explore notes",
                target=Target(
                    name="Engineering Notes",
                    text="Engineering Notes",
                    source_url="https://example.test/notes",
                ),
            )
        ],
    )
    plan = await ProductionPlanningService(StubPlanner(proposal)).plan(
        objective="Show notes", context=scoped
    )
    # With page-state validation enabled, the planner first establishes the
    # notes page (a direct fallback here because no visible navigation was
    # discovered) and then retains the exact page-local heading provenance.
    target = next(
        step.operation.target
        for step in plan.workflow_steps
        if step.operation.kind in {OperationKind.SCROLL_TO, OperationKind.VERIFY_STATE}
        and step.operation.target is not None
        and step.operation.target.source_url == "https://example.test/notes"
    )
    assert target and target.source_url == "https://example.test/notes" and target.role == "heading"


@pytest.mark.asyncio
async def test_evidence_fallback_never_selects_a_deep_link_from_an_unopened_page():
    opening = ObservedElement(
        tag="a", name="Notes", selector="#notes", href="/notes", source_url="https://example.test/"
    )
    deep = ObservedElement(
        tag="a",
        name="Deep article",
        selector="#article",
        href="/notes/article",
        source_url="https://example.test/notes",
    )
    scoped = context().model_copy(
        update={
            "url": "https://example.test/",
            "elements": [opening, deep],
            "navigation": [opening, deep],
            "relevant_routes": ["https://example.test/notes", "https://example.test/notes/article"],
        }
    )
    candidate = select_candidate_flow(scoped, "Show the deep article")
    # A sparse navigation-only snapshot cannot be upgraded into a video story:
    # it has no readable opening-page proof and the deep article was discovered
    # only on an unopened page. The planner must request re-exploration rather
    # than revive the old route-tour fallback.
    with pytest.raises(PlanningValidationError, match="lacks readable local evidence"):
        ProductionPlanningService._evidence_fallback(candidate, scoped)
