from datetime import UTC, datetime, timedelta

import pytest

from productlens.contracts.models import (
    DemoTrace,
    EditorialNarrationDraft,
    EditorialNarrationLine,
    InteractionEvent,
    ObservedElement,
    OperationKind,
    PageKnowledge,
    ProductContext,
    SemanticOperation,
    Target,
    WorkflowProposal,
)
from productlens.planning.production import ProductionPlanningService
from productlens.presentation.editorial import _observed_narration, _summary_from_collection, _viewer_ready


def test_viewer_ready_allows_an_explanatory_named_feature_but_not_a_title_dump():
    assert _viewer_ready(
        "Chit Chat Connect is a real-time messaging product with synchronized presence and typing state.",
        "Chit Chat Connect",
    )
    assert not _viewer_ready("Chit Chat Connect", "Chit Chat Connect")
    assert not _viewer_ready(
        "System Designs is a visual exploration of the core system designs.",
        "System Designs",
    )


def test_collection_fallback_is_domain_neutral_for_non_study_products():
    narration = _summary_from_collection(
        "EVENT-DRIVEN BACKEND AI PRODUCT SYSTEMS FULL STACK PRODUCT DELIVERY",
        "Core Engineering Capabilities",
    )

    assert "Core Engineering Capabilities" in narration
    assert "problem-solving" not in narration


def test_grouped_scroll_narration_explains_each_visible_subject():
    root = "https://example.test/"
    context = ProductContext(
        url=root,
        title="Example portfolio",
        application_type="portfolio",
        page_knowledge=[PageKnowledge(
            url=root,
            title="Example portfolio",
            purpose="featured work",
            visible_facts=[
                "Project Alpha :: A realtime collaboration workspace for distributed teams.",
                "Project Beta :: An analytics dashboard that turns event streams into clear reports.",
            ],
            fingerprint="home",
        )],
        elements=[
            ObservedElement(tag="h3", name="Project Alpha", selector="#alpha", source_url=root, text="A realtime collaboration workspace for distributed teams."),
            ObservedElement(tag="h3", name="Project Beta", selector="#beta", source_url=root, text="An analytics dashboard that turns event streams into clear reports."),
        ],
        confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Explore the featured projects",
        target=Target(name="Project Beta", selector="#beta", source_url=root),
        covered_content_groups=["Project Alpha", "Project Beta"],
    )
    narration = _observed_narration(context, operation, "Project Beta")
    assert "Project Alpha" in narration
    assert "Project Beta" in narration
    assert "realtime collaboration" in narration
    assert "analytics dashboard" in narration


def test_editorial_script_preserves_the_approved_opening_instead_of_rebuilding_it():
    context = ProductContext(
        url="https://example.test/", title="Example", application_type="dashboard",
        visible_text="Example workspace", confidence=1,
        page_knowledge=[PageKnowledge(
            url="https://example.test/", title="Example", purpose="workspace",
            visible_facts=["Workspace :: A shared place to review account activity."], fingerprint="opening",
        )],
    )
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO, intent="Review activity",
        target=Target(name="Activity", text="Account activity", source_url=context.url),
    )
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(
        objective="walkthrough", narrative_goal="explain", audience="prospect", target_duration_seconds=60,
        selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="one", intent=operation.intent, operation=operation)],
        expected_outcomes=["activity"], viewport_strategy="native", stop_conditions=["done"],
    )
    board = build_editorial_storyboard(context, plan)
    approved = "Here is the shared workspace and why it matters for reviewing activity."
    board = board.model_copy(update={"scenes": [board.scenes[0].model_copy(update={"narration": approved}), board.scenes[1]]})
    script = editorial_script(board, {operation.id: "event-1"})
    assert script[0]["text"].startswith(approved)
from productlens.presentation.editorial import (
    _readable_fact,
    build_editorial_storyboard,
    editorial_script,
    enrich_editorial_storyboard,
)
from productlens.quality.editorial import inspect_editorial


class Planner:
    async def structured(self, prompt, schema):
        raise AssertionError("full walkthrough uses deterministic editorial recipe")


@pytest.mark.asyncio
async def test_portfolio_completes_home_before_visible_navigation():
    items = [
        ObservedElement(tag="section", name="Featured Systems", selector="#systems", text="A curated collection of end-to-end product work."),
        ObservedElement(tag="article", name="Chit-Chat Connect", selector="#chat", text="A real-time communication platform with duplex messaging."),
        ObservedElement(tag="a", name="Timeline", selector="a", href="/timeline", source_url="https://portfolio.test/"),
        ObservedElement(tag="a", name="System Designs", selector="a", href="/systems", source_url="https://portfolio.test/"),
        ObservedElement(tag="a", name="Engineering Notes", selector="a", href="/notes", source_url="https://portfolio.test/"),
        ObservedElement(tag="a", name="Contact", selector="a", href="/contact", source_url="https://portfolio.test/"),
        ObservedElement(tag="h1", name="Career history", selector="h1", source_url="https://portfolio.test/timeline"),
        ObservedElement(tag="h1", name="System designs", selector="h1", source_url="https://portfolio.test/systems"),
        ObservedElement(tag="h1", name="Engineering notes", selector="h1", source_url="https://portfolio.test/notes"),
        ObservedElement(tag="h1", name="Contact", selector="h1", source_url="https://portfolio.test/contact"),
    ]
    context = ProductContext(
        url="https://portfolio.test/", title="Dhruv Vedwal", application_type="portfolio",
        visible_text="System architect and engineer. Featured Systems.", elements=items, navigation=items[2:],
        page_knowledge=[
            PageKnowledge(url="https://portfolio.test/", title="Home", purpose="portfolio overview", visible_sections=["Featured Systems", "Chit-Chat Connect"], visible_facts=["Featured Systems :: A curated collection of end-to-end product work.", "Chit-Chat Connect :: A real-time communication platform with duplex messaging."], fingerprint="home"),
            PageKnowledge(url="https://portfolio.test/timeline", title="Timeline", purpose="career history", visible_sections=["Career history"], visible_facts=["Career history :: A chronological view of professional experience and contributions."], fingerprint="timeline"),
            PageKnowledge(url="https://portfolio.test/systems", title="Systems", purpose="system designs", visible_sections=["System designs"], visible_facts=["System designs :: Interactive architecture diagrams explain project decisions."], fingerprint="systems"),
            PageKnowledge(url="https://portfolio.test/notes", title="Notes", purpose="engineering notes", visible_sections=["Engineering notes"], visible_facts=["Engineering notes :: Articles document implementation trade-offs."], fingerprint="notes"),
            PageKnowledge(url="https://portfolio.test/contact", title="Contact", purpose="contact", visible_sections=["Contact"], visible_facts=["Contact :: A message form provides a direct contact path."], fingerprint="contact"),
        ], confidence=1,
    )
    plan = await ProductionPlanningService(Planner()).plan(objective="Create a full walkthrough of each tab", context=context)
    ops = [step.operation for step in plan.workflow_steps]
    first_navigation = next(index for index, op in enumerate(ops) if op.kind is OperationKind.OPEN_NAVIGATION_ITEM)
    assert all(op.kind is not OperationKind.NAVIGATE for op in ops[1:])
    home_targets = [op.target.name for op in ops[1:first_navigation]]
    assert home_targets[:2] == ["Featured Systems", "Chit-Chat Connect"]
    assert {"Featured Systems", "Chit-Chat Connect"} <= set(home_targets)


@pytest.mark.asyncio
async def test_complete_walkthrough_prefers_observed_primary_navigation_over_footer_links():
    primary = ObservedElement(tag="a", name="Overview", selector="#overview", href="/overview", source_url="https://example.test/", navigation_scope="primary")
    footer = ObservedElement(tag="a", name="Legal", selector="#legal", href="/legal", source_url="https://example.test/", navigation_scope="footer")
    context = ProductContext(
        url="https://example.test/", title="Example", application_type="dashboard", visible_text="Example dashboard",
        elements=[
            primary, footer,
            ObservedElement(tag="h1", name="Dashboard", selector="h1", source_url="https://example.test/"),
            ObservedElement(tag="h1", name="Overview", selector="h1", source_url="https://example.test/overview"),
        ], navigation=[primary, footer],
        page_knowledge=[
            PageKnowledge(url="https://example.test/", title="Example", purpose="dashboard", visible_sections=["Dashboard"], visible_facts=["Dashboard :: An overview of the product workspace."], fingerprint="home"),
            PageKnowledge(url="https://example.test/overview", title="Overview", purpose="overview", visible_sections=["Overview"], visible_facts=["Overview :: The primary workspace summary."], fingerprint="overview"),
        ], confidence=1,
    )

    plan = await ProductionPlanningService(Planner()).plan(objective="Create a full walkthrough", context=context)

    names = [step.operation.target.name for step in plan.workflow_steps if step.operation.target]
    assert "Overview" in names
    assert "Legal" not in names


@pytest.mark.asyncio
async def test_editorial_model_cannot_shift_grounded_copy_between_scene_ids():
    """Array order from a structured model must not corrupt scene ownership."""
    root = "https://example.test/"
    first = SemanticOperation(
        kind=OperationKind.SCROLL_TO, intent="Inspect first", target=Target(name="First", source_url=root)
    )
    second = SemanticOperation(
        kind=OperationKind.SCROLL_TO, intent="Inspect second", target=Target(name="Second", source_url=root)
    )
    context = ProductContext(
        url=root, title="Example", application_type="portfolio", visible_text="Example", confidence=1,
        page_knowledge=[PageKnowledge(
            url=root, title="Example", purpose="portfolio", fingerprint="one",
            visible_facts=[
                "First :: A scheduling workspace that coordinates team availability.",
                "Second :: A monitoring workspace that explains service health.",
            ],
        )],
    )
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(
        objective="walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=60,
        selected_workflow="demo", expected_outcomes=["first", "second"], viewport_strategy="native", stop_conditions=["done"],
        workflow_steps=[
            __import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="one", intent=first.intent, operation=first),
            __import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="two", intent=second.intent, operation=second),
        ],
    )
    board = build_editorial_storyboard(context, plan)
    opening, first_scene, second_scene = board.scenes

    class ReassigningWriter:
        async def structured(self, _prompt, _schema):
            # Both prose lines are grounded at their array positions, but the
            # model falsely assigns the other scene's immutable identifiers.
            return board.model_copy(update={"scenes": [
                opening,
                first_scene.model_copy(update={"id": second_scene.id, "operation_id": second_scene.operation_id}),
                second_scene.model_copy(update={"id": first_scene.id, "operation_id": first_scene.operation_id}),
            ]})

    enriched = await enrich_editorial_storyboard(context, board, ReassigningWriter())
    assert enriched == board


@pytest.mark.asyncio
async def test_complete_walkthrough_explores_h4_card_content_after_opening_a_primary_page():
    root = "https://example.test/"
    systems = "https://example.test/systems"
    tab = ObservedElement(tag="a", name="Systems", selector="a", href="/systems", source_url=root, navigation_scope="primary")
    elements = [
        tab,
        ObservedElement(tag="h1", name="System Architecture", selector="h1", source_url=systems),
        ObservedElement(tag="h4", name="Event Ingestion Pipeline", selector="h4", source_url=systems),
        ObservedElement(tag="h4", name="Realtime Collaboration", selector="h4", source_url=systems),
        ObservedElement(tag="h4", name="Failure Recovery", selector="h4", source_url=systems),
    ]
    context = ProductContext(url=root, title="Example", application_type="portfolio", visible_text="Example", elements=elements, navigation=[tab], confidence=1)

    plan = await ProductionPlanningService(Planner()).plan(objective="Create a full walkthrough", context=context)

    names = [step.operation.target.name for step in plan.workflow_steps if step.operation.target]
    assert "Event Ingestion Pipeline" in names
    assert "Realtime Collaboration" in names


@pytest.mark.asyncio
async def test_grounding_keeps_page_local_heading_when_global_navigation_repeats_its_name():
    root = "https://example.test/"
    notes = "https://example.test/notes"
    nav = ObservedElement(tag="a", name="Notes", selector="a", href="/notes", source_url=root, navigation_scope="primary")
    local_heading = ObservedElement(tag="h1", name="Notes", selector="h1", source_url=notes)
    context = ProductContext(url=root, title="Example", application_type="portfolio", visible_text="Example", elements=[nav, local_heading], navigation=[nav], confidence=1)
    plan = await ProductionPlanningService(Planner()).plan(objective="Create a full walkthrough", context=context)
    local_step = next(step.operation for step in plan.workflow_steps if step.operation.kind is OperationKind.SCROLL_TO)
    assert local_step.target.source_url == notes


def test_editorial_storyboard_uses_observed_content_not_route_labels():
    context = ProductContext(url="https://study.test/", title="Study Plan", application_type="planner", visible_text="Today shows the current study plan and next lesson.", elements=[ObservedElement(tag="a", name="Today", selector="a", text="Today shows the current study plan and next lesson.")], confidence=1)
    proposal = WorkflowProposal(narrative_goal="demo", selected_workflow="demo", steps=[SemanticOperation(kind=OperationKind.SCROLL_TO, intent="Explain Today", target=Target(name="Today", text="Today"))], expected_outcomes=["Today"])
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan"]).DemoPlan(objective="Full walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=90, selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="one", intent="Explain Today", operation=proposal.steps[0])], expected_outcomes=["Today"], viewport_strategy="native", stop_conditions=["done"])
    board = build_editorial_storyboard(context, plan)
    assert "current study plan" in board.scenes[1].narration.lower()
    assert board.minimum_duration_seconds >= 45


def test_editorial_storyboard_extracts_a_readable_opening_fact_not_a_dom_dump():
    context = ProductContext(
        url="https://example.test/", title="Example", application_type="portfolio",
        visible_text="HOME WORK PROJECTS CONTACT I engineer resilient software systems for product teams.",
        page_knowledge=[PageKnowledge(
            url="https://example.test/", title="Example", purpose="Example",
            visible_facts=["Identity :: HOME WORK PROJECTS CONTACT I engineer resilient software systems for product teams."],
            fingerprint="opening",
        )], confidence=1,
    )
    operation = SemanticOperation(kind=OperationKind.SCROLL_TO, intent="Explore identity", target=Target(name="Identity", source_url=context.url))
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(objective="walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=60, selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="one", intent=operation.intent, operation=operation)], expected_outcomes=["identity"], viewport_strategy="native", stop_conditions=["done"])
    board = build_editorial_storyboard(context, plan)
    assert "I engineer resilient software systems" in board.scenes[0].narration
    assert "walkthrough pauses" not in board.scenes[1].narration.lower()


def test_editorial_brief_title_is_short_even_when_the_browser_title_is_descriptive():
    context = ProductContext(
        url="https://example.test/", title="Example Platform | Reliable Infrastructure For Distributed Product Teams",
        application_type="dashboard", visible_text="Example", confidence=1,
    )
    operation = SemanticOperation(kind=OperationKind.SCROLL_TO, intent="Explore overview", target=Target(name="Overview"))
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(objective="walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=60, selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="one", intent=operation.intent, operation=operation)], expected_outcomes=["overview"], viewport_strategy="native", stop_conditions=["done"])
    title = build_editorial_storyboard(context, plan).brief.title
    assert title == "Example Platform Reliable Infrastructure For Distributed walkthrough"
    assert len(title.split()) == 7


def test_readable_fact_removes_repeated_card_heading_and_never_clips_a_sentence():
    fact = (
        "Architecture Explorer :: Architecture Explorer Instead of static screenshots or proprietary code, "
        "explore interactive system diagrams that show how projects handle key scaling problems and operational trade-offs."
    )
    prose = _readable_fact(fact)
    assert prose.startswith("Instead of static screenshots")
    assert prose.endswith(".")
    assert "Architecture Explorer Architecture Explorer" not in prose


def test_readable_fact_rejects_label_collections_as_caption_prose():
    fact = "Skills :: LANGUAGES & RUNTIMES JavaScript TypeScript FRONTEND ARCHITECTURE React Next.js BACKEND SERVICES Node.js"
    assert _readable_fact(fact) == ""


def test_readable_fact_keeps_short_observable_status_statement():
    assert _readable_fact("Progress :: Progress Saved in SQLite. Auto-calculated from days, builds, and DSA.") == "Saved in SQLite."


def test_repeated_schedule_is_summarised_instead_of_read_verbatim():
    context = ProductContext(
        url="https://example.test/", title="Planner", application_type="planner",
        visible_text="Planner", page_knowledge=[PageKnowledge(
            url="https://example.test/", title="Planner", purpose="Planner",
            visible_facts=["Weeks :: Weeks 1 Week 1 28% 2 Week 2 5% 3 Week 3 0% 4 Week 4 0%"],
            fingerprint="planner",
        )], confidence=1,
    )
    operation = SemanticOperation(kind=OperationKind.SCROLL_TO, intent="Explore Weeks", target=Target(name="Weeks", source_url=context.url))
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(
        objective="walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=60,
        selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="one", intent=operation.intent, operation=operation)],
        expected_outcomes=["Weeks"], viewport_strategy="native", stop_conditions=["done"],
    )
    narration = build_editorial_storyboard(context, plan).scenes[1].narration
    assert "organizes" in narration
    assert "Week 1 28%" not in narration


def test_short_observed_project_fact_is_viewer_copy_instead_of_a_route_label():
    context = ProductContext(
        url="https://example.test/", title="Example", application_type="portfolio", visible_text="Work",
        page_knowledge=[PageKnowledge(
            url="https://example.test/", title="Example", purpose="work",
            visible_facts=["Atlas :: A routing engine and user dashboard."], fingerprint="work",
        )], confidence=1,
    )
    operation = SemanticOperation(kind=OperationKind.SCROLL_TO, intent="Inspect Atlas", target=Target(name="Atlas", source_url=context.url))
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(objective="walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=60, selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="atlas", intent=operation.intent, operation=operation)], expected_outcomes=["atlas"], viewport_strategy="native", stop_conditions=["done"])
    narration = build_editorial_storyboard(context, plan).scenes[1].narration
    assert narration == "A routing engine and user dashboard."
    assert "section visibly presents" not in narration.lower()


def test_editorial_fallback_uses_destination_page_facts_for_navigation():
    context = ProductContext(
        url="https://portfolio.test/", title="Portfolio", application_type="portfolio", visible_text="A systems portfolio.",
        elements=[ObservedElement(tag="a", name="Timeline", selector="a", href="/timeline", source_url="https://portfolio.test/")],
        page_knowledge=[PageKnowledge(
            url="https://portfolio.test/timeline", title="Timeline", purpose="career progression",
            visible_sections=["Experience"], visible_facts=["Senior Full Stack Developer at Smartsevak, contributing to reliable product systems."], fingerprint="timeline",
        )], confidence=1,
    )
    operation = SemanticOperation(kind=OperationKind.OPEN_NAVIGATION_ITEM, intent="Open Timeline", target=Target(name="Timeline", text="Timeline"), postconditions=[__import__("productlens.contracts.models", fromlist=["Postcondition"]).Postcondition(kind="url", expected="https://portfolio.test/timeline")])
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(objective="Full walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=90, selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="timeline", intent="Open Timeline", operation=operation)], expected_outcomes=["Timeline"], viewport_strategy="native", stop_conditions=["done"])
    board = build_editorial_storyboard(context, plan)
    narration = board.scenes[1].narration.lower()
    assert "smartsevak" in narration and len(narration.split()) >= 10


def test_editorial_navigation_prefers_destination_over_source_page_provenance():
    context = ProductContext(
        url="https://example.test/", title="Home", application_type="portfolio", visible_text="Home introduction.",
        page_knowledge=[
            PageKnowledge(url="https://example.test/", title="Home", purpose="home", visible_facts=["Home :: A concise introduction to the product."], fingerprint="home"),
            PageKnowledge(url="https://example.test/work", title="Work", purpose="work", visible_facts=["Work :: A project collection that explains how the team delivers reliable releases."], fingerprint="work"),
        ], confidence=1,
    )
    operation = SemanticOperation(kind=OperationKind.OPEN_NAVIGATION_ITEM, intent="Open work", target=Target(name="Work", source_url=context.url), postconditions=[__import__("productlens.contracts.models", fromlist=["Postcondition"]).Postcondition(kind="url", expected="https://example.test/work")])
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(objective="walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=60, selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="work", intent=operation.intent, operation=operation)], expected_outcomes=["work"], viewport_strategy="native", stop_conditions=["done"])
    assert "reliable releases" in build_editorial_storyboard(context, plan).scenes[1].narration


def test_editorial_scene_keeps_a_repeated_heading_on_its_own_page():
    context = ProductContext(
        url="https://example.test/", title="Example", application_type="portfolio", visible_text="Work",
        page_knowledge=[
            PageKnowledge(url="https://example.test/first", title="First", purpose="first work", visible_sections=["Project"], visible_facts=["Project :: A scheduling tool that coordinates availability across teams."], fingerprint="first"),
            PageKnowledge(url="https://example.test/second", title="Second", purpose="second work", visible_sections=["Project"], visible_facts=["Project :: A monitoring workspace that explains service health and incidents."], fingerprint="second"),
        ], confidence=1,
    )
    first = SemanticOperation(kind=OperationKind.SCROLL_TO, intent="Inspect first project", target=Target(name="Project", source_url="https://example.test/first"))
    second = SemanticOperation(kind=OperationKind.SCROLL_TO, intent="Inspect second project", target=Target(name="Project", source_url="https://example.test/second"))
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(objective="walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=60, selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="one", intent=first.intent, operation=first), __import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="two", intent=second.intent, operation=second)], expected_outcomes=["projects"], viewport_strategy="native", stop_conditions=["done"])

    board = build_editorial_storyboard(context, plan)

    assert "scheduling tool" in board.scenes[1].narration
    assert "monitoring workspace" in board.scenes[2].narration
    assert board.scenes[1].evidence != board.scenes[2].evidence


def test_editorial_qa_rejects_short_scene_and_generic_caption():
    now = datetime.now(UTC)
    operation = SemanticOperation(kind=OperationKind.CLICK, intent="Open progress", target=Target(name="Progress", text="Progress"))
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(objective="walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=60, selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="one", intent="Open progress", operation=operation)], expected_outcomes=["Progress"], viewport_strategy="native", stop_conditions=["done"])
    context = ProductContext(url="https://study.test/", title="Study", application_type="planner", visible_text="Today", elements=[ObservedElement(tag="a", name="Progress", selector="a", text="Review learning momentum.")], navigation=[ObservedElement(tag="a", name="Progress", selector="a", href="/progress")], confidence=1)
    board = build_editorial_storyboard(context, plan)
    event = InteractionEvent(operation_id=operation.id, kind=operation.kind, intent=operation.intent, occurred_at=now + timedelta(seconds=1), success=True, duration_ms=500)
    trace = DemoTrace(run_id="run", objective="walkthrough", started_at=now, recording_started_at=now, events=[event], outcome_verified=True)
    report = inspect_editorial(context=context, plan=plan, trace=trace, storyboard=board, script=editorial_script(board, {operation.id: event.id}))
    assert "OPENING_PAGE_ADVANCED_TOO_EARLY" in report["hard_failures"]
    assert "SCENE_ADVANCED_BEFORE_REQUIRED_DWELL" in report["hard_failures"]


def test_editorial_qa_rejects_mechanical_context_boilerplate():
    now = datetime.now(UTC)
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO, intent="Explore activity", target=Target(name="Activity", text="Activity")
    )
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(
        objective="walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=60,
        selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="one", intent="Explore activity", operation=operation)],
        expected_outcomes=["Activity"], viewport_strategy="native", stop_conditions=["done"],
    )
    context = ProductContext(url="https://example.test/", title="Example", application_type="dashboard", visible_text="Activity", elements=[ObservedElement(tag="h2", name="Activity", selector="#activity", text="Activity is displayed in a chronological list.")], confidence=1)
    board = build_editorial_storyboard(context, plan)
    event = InteractionEvent(operation_id=operation.id, kind=operation.kind, intent=operation.intent, occurred_at=now + timedelta(seconds=8), success=True, duration_ms=6_000)
    trace = DemoTrace(run_id="run", objective="walkthrough", started_at=now, recording_started_at=now, events=[event], outcome_verified=True)
    report = inspect_editorial(context=context, plan=plan, trace=trace, storyboard=board, script=[{"event_id": event.id, "text": "Activity is explored in context: Activity. This gives the viewer concrete evidence for the next part of the walkthrough."}])
    assert "GENERIC_ROUTE_LABEL_CAPTION" in report["hard_failures"]


def test_editorial_qa_rejects_raw_dom_caption_even_when_evidence_words_match():
    now = datetime.now(UTC)
    operation = SemanticOperation(kind=OperationKind.SCROLL_TO, intent="Inspect skills", target=Target(name="Skills Stack", text="Skills Stack"))
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(
        objective="walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=60,
        selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="one", intent=operation.intent, operation=operation)],
        expected_outcomes=["skills"], viewport_strategy="native", stop_conditions=["done"],
    )
    context = ProductContext(
        url="https://example.test/", title="Example", application_type="portfolio", visible_text="Skills",
        page_knowledge=[PageKnowledge(
            url="https://example.test/", title="Example", purpose="skills",
            visible_facts=["Skills Stack :: LANGUAGES JavaScript TypeScript FRONTEND React BACKEND Node"], fingerprint="skills",
        )], confidence=1,
    )
    board = build_editorial_storyboard(context, plan)
    event = InteractionEvent(operation_id=operation.id, kind=operation.kind, intent=operation.intent, occurred_at=now + timedelta(seconds=8), success=True, duration_ms=8_000)
    trace = DemoTrace(run_id="run", objective="walkthrough", started_at=now, recording_started_at=now, events=[event], outcome_verified=True)
    report = inspect_editorial(
        context=context, plan=plan, trace=trace, storyboard=board,
        script=[{"event_id": event.id, "text": "Skills Stack LANGUAGES JavaScript TypeScript FRONTEND React BACKEND Node."}],
    )
    assert "GENERIC_ROUTE_LABEL_CAPTION" in report["hard_failures"]


def test_editorial_qa_rejects_caption_borrowed_from_a_different_page():
    now = datetime.now(UTC)
    operation = SemanticOperation(kind=OperationKind.SCROLL_TO, intent="Inspect scheduling", target=Target(name="Scheduling", source_url="https://example.test/scheduling"))
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(objective="walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=60, selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="one", intent=operation.intent, operation=operation)], expected_outcomes=["scheduling"], viewport_strategy="native", stop_conditions=["done"])
    context = ProductContext(url="https://example.test/", title="Example", application_type="dashboard", visible_text="Work", page_knowledge=[PageKnowledge(url="https://example.test/scheduling", title="Scheduling", purpose="scheduling", visible_facts=["Scheduling :: A calendar workspace that coordinates availability."], fingerprint="scheduling")], confidence=1)
    board = build_editorial_storyboard(context, plan)
    event = InteractionEvent(operation_id=operation.id, kind=operation.kind, intent=operation.intent, occurred_at=now + timedelta(seconds=8), success=True, duration_ms=8_000)
    trace = DemoTrace(run_id="run", objective="walkthrough", started_at=now, recording_started_at=now, events=[event], outcome_verified=True)
    report = inspect_editorial(context=context, plan=plan, trace=trace, storyboard=board, script=[{"event_id": event.id, "text": "A monitoring workspace explains incidents and service health."}])

    assert "UNSUPPORTED_OR_WRONG_SCENE_CAPTION" in report["hard_failures"]


def test_editorial_qa_rejects_trace_that_leaves_a_tab_without_local_exploration():
    now = datetime.now(UTC)
    nav = SemanticOperation(kind=OperationKind.OPEN_NAVIGATION_ITEM, intent="Open Timeline", target=Target(name="Timeline", text="Timeline"))
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(objective="walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=60, selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="nav", intent="Open Timeline", operation=nav)], expected_outcomes=["Timeline"], viewport_strategy="native", stop_conditions=["done"])
    context = ProductContext(url="https://study.test/", title="Study", application_type="planner", visible_text="Today", elements=[ObservedElement(tag="a", name="Timeline", selector="a", text="Career timeline.")], confidence=1)
    board = build_editorial_storyboard(context, plan)
    event = InteractionEvent(operation_id=nav.id, kind=nav.kind, intent=nav.intent, occurred_at=now + timedelta(seconds=8), success=True, duration_ms=6_000)
    trace = DemoTrace(run_id="run", objective="walkthrough", started_at=now, recording_started_at=now, events=[event], outcome_verified=True)
    report = inspect_editorial(context=context, plan=plan, trace=trace, storyboard=board, script=editorial_script(board, {nav.id: event.id}))
    assert "TRACE_NAVIGATED_PAGE_NOT_EXPLORED" in report["hard_failures"]


@pytest.mark.asyncio
async def test_editorial_writer_can_change_only_grounded_prose_not_scene_contract():
    context = ProductContext(
        url="https://example.test/", title="Example", application_type="dashboard",
        visible_text="A dashboard that presents account activity.",
        elements=[ObservedElement(tag="h2", name="Activity", selector="#activity", text="Account activity is displayed in a chronological list.")], confidence=1,
    )
    operation = SemanticOperation(kind=OperationKind.SCROLL_TO, intent="Explore activity", target=Target(name="Activity", text="Activity"))
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(objective="walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=60, selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="one", intent="Explore activity", operation=operation)], expected_outcomes=["activity"], viewport_strategy="native", stop_conditions=["done"])
    board = build_editorial_storyboard(context, plan)

    class Writer:
        async def structured(self, prompt, schema):
            assert schema is EditorialNarrationDraft
            return EditorialNarrationDraft(lines=[
                EditorialNarrationLine(
                    id=scene.id,
                    narration="Account activity is displayed in a chronological list, making recent changes easy to review.",
                )
                for scene in board.scenes
                if scene.operation_id is not None
            ])

    enriched = await enrich_editorial_storyboard(context, board, Writer())
    assert enriched.scenes[1].narration.startswith("Account activity")
    assert enriched.scenes[1].required_dwell_seconds == board.scenes[1].required_dwell_seconds
    assert enriched.scenes[1].evidence == board.scenes[1].evidence


@pytest.mark.asyncio
async def test_editorial_writer_rejects_generic_claim_despite_small_word_overlap():
    context = ProductContext(
        url="https://example.test/", title="Example", application_type="dashboard",
        visible_text="Account activity is shown in a chronological list.",
        elements=[ObservedElement(tag="h2", name="Activity", selector="#activity", text="Account activity is shown in a chronological list.")], confidence=1,
    )
    operation = SemanticOperation(kind=OperationKind.SCROLL_TO, intent="Explore activity", target=Target(name="Activity", text="Activity"))
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(objective="walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=60, selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="one", intent=operation.intent, operation=operation)], expected_outcomes=["activity"], viewport_strategy="native", stop_conditions=["done"])
    board = build_editorial_storyboard(context, plan)

    class GenericWriter:
        async def structured(self, prompt, schema):
            return board.model_copy(update={"scenes": [scene.model_copy(update={"narration": "This bespoke command interface makes activity visible before moving on."}) for scene in board.scenes]})

    assert await enrich_editorial_storyboard(context, board, GenericWriter()) == board
