import pytest

from productlens.contracts.models import (
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
from productlens.planning.candidates import build_page_complete_proposal, select_candidate_flow
from productlens.planning.production import PlanningValidationError, ProductionPlanningService


class StubPlanner:
    def __init__(self, proposal: WorkflowProposal):
        self.proposal = proposal

    async def structured(self, prompt: str, schema):
        assert "Observed evidence" in prompt
        assert schema is WorkflowProposal
        return self.proposal


def context() -> ProductContext:
    return ProductContext(
        url="https://example.test/app",
        title="Example CRM",
        application_type="crm",
        relevant_routes=["https://example.test/leads"],
        elements=[
            ObservedElement(tag="button", name="New lead", selector='[data-testid="new-lead"]')
        ],
        confidence=0.9,
    )


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
        objective="Show lead area", context=context(), audience="sales manager", target_duration_seconds=150
    )
    assert (plan.audience, plan.target_duration_seconds) == ("sales manager", 150)
    assert (plan.minimum_duration_seconds, plan.maximum_duration_seconds) == (105, 210)


@pytest.mark.asyncio
async def test_full_walkthrough_objective_preserves_its_thorough_duration_contract():
    proposal = WorkflowProposal(
        narrative_goal="Show the product", selected_workflow="overview",
        steps=[SemanticOperation(kind=OperationKind.NAVIGATE, intent="Open product", value="https://example.test/app", postconditions=[Postcondition(kind="url", expected="https://example.test/app")])],
        expected_outcomes=["Product is visible"],
    )
    scoped = context().model_copy(update={
        "objective": ObjectiveSpec(
            raw="Create a full walkthrough", demo_type="full_walkthrough", depth="thorough",
            minimum_duration_seconds=110, target_duration_seconds=180, maximum_duration_seconds=240,
        )
    })
    plan = await ProductionPlanningService(StubPlanner(proposal)).plan(
        objective="Create a full walkthrough", context=scoped
    )
    assert (plan.minimum_duration_seconds, plan.target_duration_seconds, plan.maximum_duration_seconds) == (110, 180, 240)


@pytest.mark.asyncio
async def test_production_planner_derives_select_value_only_from_observed_options():
    select = ObservedElement(tag="select", name="Region", selector="#region", options=["", "India", "United States"])
    proposal = WorkflowProposal(
        narrative_goal="Set a region", selected_workflow="configuration",
        steps=[SemanticOperation(
            kind=OperationKind.SELECT_OPTION, intent="Choose a region", target=Target(name="Region", selector="#region"),
            postconditions=[Postcondition(kind="visible", expected=True, target=Target(name="Region", selector="#region"))],
        )], expected_outcomes=["Region is selected"],
    )
    scoped_context = context().model_copy(update={"elements": [select]})
    plan = await ProductionPlanningService(StubPlanner(proposal)).plan(objective="Set a region", context=scoped_context)
    operation = plan.workflow_steps[0].operation
    assert operation.value == "India"
    assert any(condition.kind == "value" and condition.expected == "India" for condition in operation.postconditions)


@pytest.mark.asyncio
async def test_full_walkthrough_covers_each_safe_primary_tab_once():
    local_content = [
        ObservedElement(tag="h2", name="Today overview", selector="#today-overview", source_url="https://example.test/today", text="Today highlights the current lesson and recommended next step."),
        ObservedElement(tag="h2", name="Progress summary", selector="#progress-summary", source_url="https://example.test/progress", text="Progress shows completed work and learning momentum."),
    ]
    scoped = context().model_copy(update={"relevant_routes": ["https://example.test/today", "https://example.test/progress"], "elements": [*context().elements, *local_content], "page_knowledge": [
        PageKnowledge(url="https://example.test/today", title="Today", purpose="Today plan", visible_sections=["Today overview"], scroll_landmarks=["Today overview"], fingerprint="today"),
        PageKnowledge(url="https://example.test/progress", title="Progress", purpose="Progress", visible_sections=["Progress summary"], scroll_landmarks=["Progress summary"], fingerprint="progress"),
    ], "navigation": [
        ObservedElement(tag="a", name="Today", selector="a", href="/today", source_url="https://example.test/app"),
        ObservedElement(tag="a", name="Progress", selector="a", href="/progress", source_url="https://example.test/app"),
        ObservedElement(tag="a", name="Docs", selector="a", href="https://outside.test/docs", source_url="https://example.test/app"),
    ]})
    fallback = WorkflowProposal(narrative_goal="fallback", selected_workflow="fallback", steps=[SemanticOperation(kind=OperationKind.NAVIGATE, intent="Open", value="https://example.test/app", postconditions=[Postcondition(kind="url", expected="https://example.test/app")])], expected_outcomes=["opened"])
    plan = await ProductionPlanningService(StubPlanner(fallback)).plan(objective="Create a full walkthrough of each tab", context=scoped)
    assert plan.workflow_steps[0].operation.value == "https://example.test/app"
    operations = [step.operation for step in plan.workflow_steps[1:]]
    first_navigation = next(index for index, operation in enumerate(operations) if operation.kind is OperationKind.OPEN_NAVIGATION_ITEM)
    assert all(operation.kind is OperationKind.SCROLL_TO for operation in operations[:first_navigation])
    # A page is no longer considered covered by one route click alone. A
    # single-landmark page may satisfy adjacent duties in its one readable
    # local scroll; richer pages receive one scene per required content group.
    assert [operation.target.name for operation in operations if operation.kind is OperationKind.OPEN_NAVIGATION_ITEM] == ["Today", "Progress"]
    for page, section in (("https://example.test/today", "Today overview"), ("https://example.test/progress", "Progress summary")):
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
        ObservedElement(tag="a", name=f"Area {index}", selector=f"#area-{index}", href=f"/area-{index}", source_url=root, navigation_scope="primary")
        for index in range(4)
    ]
    elements = [
        ObservedElement(tag="h2", name=f"Opening section {index}", selector=f"#opening-{index}", source_url=root)
        for index in range(12)
    ]
    for index in range(4):
        page_url = f"https://example.test/area-{index}"
        elements.extend(
            ObservedElement(tag="h2", name=f"Area {index} section {section}", selector=f"#area-{index}-{section}", source_url=page_url)
            for section in range(12)
        )
    scoped = context().model_copy(update={"url": root, "elements": elements, "navigation": navigation})
    candidate = select_candidate_flow(scoped, "Create a full walkthrough")
    assert candidate is not None
    proposal = build_page_complete_proposal(scoped, candidate)
    page_steps = {
        page_url: [step for step in proposal.steps if step.page_url.rstrip("/") == page_url.rstrip("/")]
        for page_url in [root, *(f"https://example.test/area-{index}" for index in range(4))]
    }
    # Candidate compilation produces the full editorial contract per selected
    # page; bounded route/heading coverage is not an accepted substitute.
    assert all(
        {phase for step in steps for phase in step.page_contract_phases}
        >= {"establish", "explore", "explain", "demonstrate", "verify"}
        for steps in page_steps.values()
    )


def test_repeated_numbered_series_keeps_one_representative_detail():
    root = "https://example.test/"
    scoped = context().model_copy(update={
        "url": root,
        "elements": [
            ObservedElement(tag="h2", name="Weeks", selector="#weeks", source_url=root),
            *[
                ObservedElement(tag="h3", name=f"Week {index}", selector=f"#week-{index}", source_url=root)
                for index in range(1, 5)
            ],
        ],
    })
    selected = ProductionPlanningService._page_exploration_targets(scoped, root, limit=8)
    assert [item.name for item in selected].count("Weeks") == 1
    assert [item.name for item in selected if item.name.startswith("Week ")] == ["Week 1"]


def test_home_featured_collection_is_selected_before_generic_sections():
    root = "https://example.test/"
    projects = ["Realtime Chat", "Knowledge Hub", "Prompt Router", "Interview Coach", "Meal Planner"]
    scoped = context().model_copy(update={
        "url": root,
        "elements": [
            ObservedElement(tag="h1", name="Alex Example", selector="#home", source_url=root),
            ObservedElement(tag="h2", name="Capabilities", selector="#capabilities", source_url=root),
            ObservedElement(tag="h3", name="Reliability", selector="#reliability", source_url=root),
            ObservedElement(tag="h3", name="Performance", selector="#performance", source_url=root),
            ObservedElement(tag="h2", name="Featured systems", selector="#projects", source_url=root),
            *[ObservedElement(tag="h3", name=name, selector=f"#project-{index}", source_url=root) for index, name in enumerate(projects)],
            ObservedElement(tag="h2", name="Impact metrics", selector="#metrics", source_url=root),
        ],
    })
    selected = ProductionPlanningService._page_exploration_targets(scoped, root, limit=7)

    assert [item.name for item in selected][:2] == ["Alex Example", "Featured systems"]
    assert [item.name for item in selected][2:] == projects


@pytest.mark.asyncio
async def test_page_local_heading_keeps_its_discovery_provenance():
    home = ObservedElement(tag="h2", name="Engineering Notes", selector="h2", source_url="https://example.test/", text="Home summary")
    notes = ObservedElement(tag="h1", name="Engineering Notes", selector="h1", source_url="https://example.test/notes", text="Notes on resilient systems")
    scoped = context().model_copy(update={"elements": [home, notes]})
    proposal = WorkflowProposal(narrative_goal="notes", selected_workflow="notes", expected_outcomes=["notes"], steps=[SemanticOperation(kind=OperationKind.SCROLL_TO, intent="Explore notes", target=Target(name="Engineering Notes", text="Engineering Notes", source_url="https://example.test/notes"))])
    plan = await ProductionPlanningService(StubPlanner(proposal)).plan(objective="Show notes", context=scoped)
    target = plan.workflow_steps[0].operation.target
    assert target and target.source_url == "https://example.test/notes" and target.role == "heading"


@pytest.mark.asyncio
async def test_evidence_fallback_never_selects_a_deep_link_from_an_unopened_page():
    opening = ObservedElement(tag="a", name="Notes", selector="#notes", href="/notes", source_url="https://example.test/")
    deep = ObservedElement(tag="a", name="Deep article", selector="#article", href="/notes/article", source_url="https://example.test/notes")
    scoped = context().model_copy(update={
        "url": "https://example.test/",
        "elements": [opening, deep],
        "navigation": [opening, deep],
        "relevant_routes": ["https://example.test/notes", "https://example.test/notes/article"],
    })
    candidate = select_candidate_flow(scoped, "Show the deep article")
    # A sparse navigation-only snapshot cannot be upgraded into a video story:
    # it has no readable opening-page proof and the deep article was discovered
    # only on an unopened page. The planner must request re-exploration rather
    # than revive the old route-tour fallback.
    with pytest.raises(PlanningValidationError, match="lacks readable local evidence"):
        ProductionPlanningService._evidence_fallback(candidate, scoped)
