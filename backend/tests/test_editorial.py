from datetime import UTC, datetime, timedelta

import pytest

from productlens.contracts.models import (
    DemoTrace,
    EditorialBrief,
    EditorialNarrationDraft,
    EditorialNarrationLine,
    EditorialScene,
    EditorialStoryboard,
    InteractionEvent,
    ObjectiveSpec,
    ObservedElement,
    OperationKind,
    PageKnowledge,
    ProductContext,
    SemanticOperation,
    Target,
    WorkflowProposal,
)
from productlens.planning.production import ProductionPlanningService
from productlens.presentation.editorial import (
    _mentions_scene_element,
    _mentions_scene_subject,
    _observed_narration,
    _summary_from_collection,
    _viewer_ready,
    editorial_script,
    narrated_storyboard_scenes,
)


def test_viewer_ready_allows_an_explanatory_named_feature_but_not_a_title_dump():
    assert _viewer_ready(
        "Chit Chat Connect is a real-time messaging product with synchronized presence and typing state.",
        "Chit Chat Connect",
    )


def test_form_narration_explains_the_flow_instead_of_reading_the_input_label():
    context = ProductContext(
        url="https://example.test/leads", title="Example", application_type="dashboard",
    )
    operation = SemanticOperation(
        kind=OperationKind.FILL_PHONE,
        intent="Enter the observed Enter Phone Number value",
        target=Target(name="Enter Phone Number"),
    )

    narration = _observed_narration(context, operation, "Enter Phone Number")

    assert "Enter Phone Number" not in narration
    assert "traceable example" in narration


def test_editorial_script_compresses_repeated_scroll_landmarks_without_losing_navigation_or_closing():
    scenes = [
        EditorialScene(
            id=f"scene-{index}", operation_id=f"operation-{index}", title=f"Detail {index}",
            purpose="Explain observed evidence", narration=f"This observed detail {index} explains why the product flow matters to the viewer.",
            evidence=["page:https://example.test/work"], interaction="scroll",
            required_dwell_seconds=3, completion_criteria=["visible"], page_url="https://example.test/work",
        )
        for index in range(14)
    ]
    scenes.insert(0, EditorialScene(
        id="opening", title="Opening", purpose="Establish", narration="Welcome to the observed product experience.",
        evidence=["page:https://example.test/"], interaction="opening", required_dwell_seconds=5,
        completion_criteria=["stable"],
    ))
    scenes.insert(4, EditorialScene(
        id="navigation", operation_id="navigation", title="Timeline", purpose="Enter career history",
        narration="This timeline connects visible roles into a clear progression of engineering responsibility.",
        evidence=["page:https://example.test/timeline"], interaction="navigate", required_dwell_seconds=4,
        completion_criteria=["visible"], page_url="https://example.test/timeline",
    ))
    scenes.append(EditorialScene(
        id="closing", operation_id="closing", title="Contact", purpose="Close journey",
        narration="The contact path provides a clear next step for continuing the conversation.",
        evidence=["page:https://example.test/contact"], interaction="click", required_dwell_seconds=4,
        completion_criteria=["visible"], page_url="https://example.test/contact", story_phase="close",
    ))
    board = EditorialStoryboard(
        brief=EditorialBrief(title="Example", product_purpose="Example", opening_message="Welcome", facts=[]),
        scenes=scenes, minimum_duration_seconds=60,
    )
    selected = narrated_storyboard_scenes(board)
    assert [scene.id for scene in selected].count("navigation") == 1
    assert [scene.id for scene in selected].count("closing") == 1
    assert sum(scene.interaction == "scroll" for scene in selected) == 14
    script = editorial_script(board, {scene.operation_id: f"event-{scene.id}" for scene in scenes if scene.operation_id})
    assert [line["event_id"] for line in script] == [f"event-{scene.id}" for scene in selected]
    assert not _viewer_ready("Chit Chat Connect", "Chit Chat Connect")
    assert not _viewer_ready(
        "System Designs is a visual exploration of the core system designs.",
        "System Designs",
    )


def test_editorial_script_keeps_one_caption_for_one_proven_event():
    opening = EditorialScene(
        id="opening", title="Opening", purpose="Establish",
        narration="Welcome to the observed product.", evidence=["page:https://example.test/"],
        interaction="opening", required_dwell_seconds=4, completion_criteria=["stable"],
    )
    first = EditorialScene(
        id="first", operation_id="same", title="Workspace", purpose="Explain",
        narration="The workspace shows the verified workflow.", evidence=["page:https://example.test/work"],
        interaction="observe", required_dwell_seconds=3, completion_criteria=["visible"],
    )
    duplicate = EditorialScene(
        id="duplicate", operation_id="also-same", title="Repeated state", purpose="Explain",
        narration="This must not become a second caption for the same event.", evidence=["page:https://example.test/work"],
        interaction="observe", required_dwell_seconds=3, completion_criteria=["visible"],
    )
    board = EditorialStoryboard(brief=EditorialBrief(title="Example", product_purpose="Example", opening_message="Welcome", facts=[]), scenes=[opening, first, duplicate], minimum_duration_seconds=20)
    script = editorial_script(board, {"same": "event-1", "also-same": "event-1"})
    assert [line["event_id"] for line in script] == ["event-1"]
    assert script[0]["opening"] is True


def test_editorial_script_rephrases_duplicate_evidence_beats_without_inventing_facts():
    opening = EditorialScene(
        id="opening", title="Opening", purpose="Establish", narration="Welcome to the observed product experience.",
        evidence=["page:https://example.test/"], interaction="opening", required_dwell_seconds=5,
        completion_criteria=["stable"],
    )
    first = EditorialScene(
        id="first", operation_id="op-first", title="Week 1", purpose="Explain the visible week plan",
        narration="The Week 1 view shows the builds planned this week, including implementation practice to review next.",
        evidence=["page:https://example.test/weeks", "section:Week 1", "section:Builds"],
        interaction="scroll", required_dwell_seconds=4, completion_criteria=["visible"], page_url="https://example.test/weeks",
    )
    second = first.model_copy(update={"id": "second", "operation_id": "op-second"})
    board = EditorialStoryboard(
        brief=EditorialBrief(title="Example", product_purpose="Example", opening_message="Welcome", facts=[]),
        scenes=[opening, first, second], minimum_duration_seconds=45,
    )
    lines = editorial_script(board, {"op-first": "event-first", "op-second": "event-second"})
    assert lines[0]["text"] != lines[1]["text"]
    assert "Week" in lines[1]["text"] or "Builds" in lines[1]["text"]


def test_model_narration_must_retain_named_scene_subject():
    scene = EditorialScene(
        id="internship", operation_id="op", title="Software Development Intern",
        purpose="Explain the observed role", narration="Observed role contribution.",
        evidence=["page:https://example.test/resume", "element:Datansh Solutions", "element:Software Development Intern"],
        interaction="scroll", required_dwell_seconds=4, completion_criteria=["visible"],
        page_url="https://example.test/resume",
    )
    assert not _mentions_scene_element(scene, "Engineered workflow automation interfaces and HRMS schemas.")
    assert _mentions_scene_element(scene, "At Datansh Solutions, the internship focused on workflow automation.")
    assert not _mentions_scene_subject(scene, "The internship focused on workflow automation.")
    assert _mentions_scene_subject(scene, "The Software Development Intern role focused on workflow automation.")


def test_collection_fallback_is_domain_neutral_for_non_study_products():
    narration = _summary_from_collection(
        "EVENT-DRIVEN BACKEND AI PRODUCT SYSTEMS FULL STACK PRODUCT DELIVERY",
        "Core Engineering Capabilities",
    )

    assert "Core Engineering Capabilities" in narration
    assert "problem-solving" not in narration


def test_category_landmark_uses_viewer_value_instead_of_route_mechanics():
    root = "https://example.test/resume"
    context = ProductContext(
        url=root,
        title="Example portfolio",
        application_type="portfolio",
        page_knowledge=[PageKnowledge(
            url=root,
            title="Professional History",
            purpose="professional history",
            visible_facts=[
                "DATABASES, TOOLS & CLOUD :: DATABASES, TOOLS & CLOUD MongoDB PostgreSQL Redis AWS (SES, SQS) Prisma Sequelize",
            ],
            fingerprint="resume",
        )],
        confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Review the databases, tools, and cloud stack",
        target=Target(name="DATABASES, TOOLS & CLOUD", source_url=root),
    )

    narration = _observed_narration(context, operation, "DATABASES, TOOLS & CLOUD")

    assert "is shown" not in narration.casefold()
    assert "focused review" not in narration.casefold()
    assert "DATABASES, TOOLS & CLOUD" in narration
    assert _viewer_ready(narration, "DATABASES, TOOLS & CLOUD")


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


def test_editorial_script_does_not_cut_opening_mid_sentence():
    opening = EditorialScene(
        id="opening", title="Opening", purpose="Establish",
        narration=(
            "Welcome to the observed product. Today I will walk through the workspace. "
            "The opening view establishes the context for the workflow before we"
        ),
        evidence=["page:https://example.test/"], interaction="opening", required_dwell_seconds=5,
        completion_criteria=["stable opening"],
    )
    first = EditorialScene(
        id="first", title="Workspace", purpose="Workspace", operation_id="op",
        narration="The workspace keeps the relevant state visible for the viewer.",
        evidence=["page:https://example.test/"], interaction="observe", required_dwell_seconds=4,
        completion_criteria=["workspace visible"],
    )
    board = EditorialStoryboard(
        brief=EditorialBrief(title="Example", product_purpose="Example", opening_message="Welcome", facts=[]),
        scenes=[opening, first], minimum_duration_seconds=20,
    )
    script = editorial_script(board, {"op": "event-1"})
    assert script[0]["text"].endswith("workspace.")
    assert not script[0]["text"].endswith("before we.")
from productlens.presentation.editorial import (
    _readable_fact,
    build_editorial_storyboard,
    enrich_editorial_storyboard,
)
from productlens.quality.editorial import inspect_editorial, inspect_editorial_preflight


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
    # The compact heading is already visible: inspect it instead of
    # manufacturing a zero-distance scroll merely to claim coverage.
    local_step = next(step.operation for step in plan.workflow_steps if step.operation.kind is OperationKind.VERIFY_STATE)
    assert local_step.target.source_url == notes


def test_editorial_storyboard_uses_observed_content_not_route_labels():
    context = ProductContext(url="https://study.test/", title="Study Plan", application_type="planner", visible_text="Today shows the current study plan and next lesson.", elements=[ObservedElement(tag="a", name="Today", selector="a", text="Today shows the current study plan and next lesson.")], confidence=1)
    proposal = WorkflowProposal(narrative_goal="demo", selected_workflow="demo", steps=[SemanticOperation(kind=OperationKind.SCROLL_TO, intent="Explain Today", target=Target(name="Today", text="Today"))], expected_outcomes=["Today"])
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan"]).DemoPlan(objective="Full walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=90, selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="one", intent="Explain Today", operation=proposal.steps[0])], expected_outcomes=["Today"], viewport_strategy="native", stop_conditions=["done"])
    board = build_editorial_storyboard(context, plan)
    assert "current study plan" in board.scenes[1].narration.lower()
    assert board.minimum_duration_seconds >= 45


def test_editorial_storyboard_preserves_the_approved_feature_duration_floor():
    context = ProductContext(
        url="https://example.test/", title="Example", application_type="dashboard",
        visible_text="The requested workspace is ready.", confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.VERIFY_STATE, intent="Establish the workspace", target=Target(name="Workspace"),
    )
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(
        objective="Show the workspace", narrative_goal="demo", audience="prospect",
        target_duration_seconds=120, minimum_duration_seconds=60, maximum_duration_seconds=180,
        selected_workflow="workspace", workflow_steps=[
            __import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one", intent=operation.intent, operation=operation,
            )
        ], expected_outcomes=["Workspace"], viewport_strategy="native", stop_conditions=["done"],
    )

    assert build_editorial_storyboard(context, plan).minimum_duration_seconds == 60


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


def test_opening_keeps_objective_and_exploration_context_when_the_workspace_is_sparse():
    leads = "https://example.test/leads"
    configuration = "https://example.test/settings/lead-configuration"
    context = ProductContext(
        url=leads, title="Example CRM", application_type="crm", confidence=1,
        objective=ObjectiveSpec(
            raw="Show Lead Management in the context of Lead Configuration",
            primary_entity="Lead Management",
            supporting_relationships=[{
                "source": "Lead Configuration", "target": "Lead Management",
            }],
        ),
        page_knowledge=[
            PageKnowledge(url=leads, title="Leads", purpose="Leads", fingerprint="leads"),
            PageKnowledge(url=configuration, title="Lead Configuration", purpose="Lead Configuration", fingerprint="config"),
        ],
    )
    operation = SemanticOperation(
        kind=OperationKind.NAVIGATE, intent="Establish leads", value=leads,
        postconditions=[__import__("productlens.contracts.models", fromlist=["Postcondition"]).Postcondition(kind="url", expected=leads)],
    )
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(
        objective=context.objective.raw, narrative_goal="demo", audience="prospect", target_duration_seconds=60,
        selected_workflow="leads", expected_outcomes=["lead workflow"], viewport_strategy="native", stop_conditions=["done"],
        workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="leads", intent=operation.intent, operation=operation)],
    )
    opening = build_editorial_storyboard(context, plan).scenes[0].narration
    assert "Lead Management" in opening
    assert "Lead Configuration" in opening
    assert opening.startswith("Welcome to Example CRM")


def test_editorial_preflight_rejects_generic_navigation_before_execution():
    root = "https://example.test/"
    operation = SemanticOperation(
        kind=OperationKind.OPEN_NAVIGATION_ITEM, intent="Open records",
        target=Target(name="Records", source_url=root),
    )
    context = ProductContext(url=root, title="Example", application_type="dashboard", confidence=1)
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(
        objective="Show records", narrative_goal="demo", audience="prospect", target_duration_seconds=60,
        selected_workflow="records", expected_outcomes=["records"], viewport_strategy="native", stop_conditions=["done"],
        workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="records", intent=operation.intent, operation=operation)],
    )
    storyboard = EditorialStoryboard(
        brief=EditorialBrief(title="Example walkthrough", product_purpose="Example", opening_message="Welcome"),
        minimum_duration_seconds=45,
        scenes=[
            EditorialScene(
                id="opening", title="Opening", purpose="Establish", narration="Welcome to Example.",
                evidence=[f"page:{root}"], interaction="opening", required_dwell_seconds=5,
                completion_criteria=["stable"],
            ),
            EditorialScene(
                id="records", operation_id=operation.id, title="Records", purpose="Open records",
                narration="The Records workspace opens with various filters and details, ready for management and action.",
                evidence=[f"page:{root}", "element:Records"], interaction="navigate", required_dwell_seconds=4,
                completion_criteria=["visible"],
            ),
        ],
    )
    report = inspect_editorial_preflight(context=context, plan=plan, storyboard=storyboard)
    assert "GENERIC_ROUTE_LABEL_CAPTION" in report["hard_failures"]


def test_editorial_preflight_rejects_contact_or_record_data_in_scene_copy():
    root = "https://example.test/"
    context = ProductContext(url=root, title="Example", application_type="dashboard", confidence=1)
    operation = SemanticOperation(kind=OperationKind.READ_VALUE, intent="Read record context", target=Target(name="Records"))
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(
        objective="Show records", narrative_goal="demo", audience="prospect", target_duration_seconds=60,
        selected_workflow="records", expected_outcomes=["records"], viewport_strategy="native", stop_conditions=["done"],
        workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
            id="records", intent=operation.intent, operation=operation,
        )],
    )
    storyboard = EditorialStoryboard(
        brief=EditorialBrief(title="Example", product_purpose="Example", opening_message="Welcome"),
        minimum_duration_seconds=45,
        scenes=[EditorialScene(
            id="opening", title="Opening", purpose="Establish",
            narration="Welcome to Example. The created record is 985 223 9496.",
            evidence=[f"page:{root}"], interaction="opening", required_dwell_seconds=5,
            completion_criteria=["stable"],
        )],
    )
    report = inspect_editorial_preflight(context=context, plan=plan, storyboard=storyboard)
    assert "EDITORIAL_SENSITIVE_RECORD_DATA" in report["hard_failures"]


def test_editorial_preflight_counts_uppercase_dom_evidence_case_insensitively():
    root = "https://example.test/resume"
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Review the database stack",
        target=Target(name="DATABASES, TOOLS & CLOUD", source_url=root),
    )
    context = ProductContext(
        url=root, title="Example portfolio", application_type="portfolio", confidence=1,
        page_knowledge=[PageKnowledge(
            url=root,
            title="Professional History",
            purpose="professional history",
            visible_facts=[
                "DATABASES, TOOLS & CLOUD :: DATABASES, TOOLS & CLOUD MongoDB PostgreSQL Redis AWS Prisma Sequelize",
            ],
            fingerprint="resume",
        )],
    )
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(
        objective="Review the database stack", narrative_goal="explain", audience="prospect",
        target_duration_seconds=60, selected_workflow="resume", expected_outcomes=["stack"],
        viewport_strategy="native", stop_conditions=["done"],
        workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
            id="stack", intent=operation.intent, operation=operation,
        )],
    )
    storyboard = EditorialStoryboard(
        brief=EditorialBrief(title="Example walkthrough", product_purpose="Example", opening_message="Welcome"),
        minimum_duration_seconds=45,
        scenes=[
            EditorialScene(
                id="opening", title="Opening", purpose="Establish", narration="Welcome to Example.",
                evidence=[f"page:{root}"], interaction="opening", required_dwell_seconds=5,
                completion_criteria=["stable"],
            ),
            EditorialScene(
                id="stack", operation_id=operation.id, title="DATABASES, TOOLS & CLOUD",
                purpose="Explain the visible stack", narration=(
                    "The DATABASES, TOOLS & CLOUD view groups the visible tools into a structured collection for comparison."
                ), evidence=[f"page:{root}", "fact:placeholder"], interaction="scroll",
                required_dwell_seconds=3, completion_criteria=["visible"], page_url=root,
            ),
        ],
    )
    # Use the real evidence id so the preflight source lookup is page-local.
    from productlens.presentation.editorial import _fact_id
    storyboard.scenes[1].evidence[1] = _fact_id(root, context.page_knowledge[0].visible_facts[0])
    report = inspect_editorial_preflight(context=context, plan=plan, storyboard=storyboard)
    assert "SCROLL_SCENE_LACKS_READABLE_LOCAL_EVIDENCE" not in report["hard_failures"]


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


def test_human_sentence_normalizes_joined_heading_punctuation():
    from productlens.presentation.editorial import _human_sentence

    assert _human_sentence("The page opens with rationale,, where the visible design is clear") == (
        "The page opens with rationale, where the visible design is clear."
    )


def test_readable_fact_rejects_label_collections_as_caption_prose():
    fact = "Skills :: LANGUAGES & RUNTIMES JavaScript TypeScript FRONTEND ARCHITECTURE React Next.js BACKEND SERVICES Node.js"
    assert _readable_fact(fact) == ""


def test_readable_fact_keeps_short_observable_status_statement():
    assert _readable_fact("Progress :: Progress Saved in SQLite. Auto-calculated from days, builds, and DSA.") == "Saved in SQLite."


def test_destination_intro_reframes_highlighted_task_as_presenter_copy():
    page = PageKnowledge(
        url="https://example.test/weeks/06", title="Week 6", purpose="Week 6",
        visible_facts=[], fingerprint="week-6",
    )
    from productlens.presentation.editorial import _page_intro_from_fact

    narration = _page_intro_from_fact(
        page,
        "REACT Auth provider Build an auth context holding user + access token in memory with login/logout APIs.",
    )
    assert narration.startswith("The Week 6 page opens with REACT Auth provider")
    assert "visible build" in narration


def test_destination_intro_does_not_narrate_storage_implementation():
    page = PageKnowledge(
        url="https://example.test/progress", title="Progress", purpose="Progress",
        visible_facts=[], fingerprint="progress",
    )
    from productlens.presentation.editorial import _page_intro_from_fact

    narration = _page_intro_from_fact(page, "Saved in SQLite (`data/progress.db`).")
    assert "SQLite" not in narration
    assert "completion state" in narration


def test_destination_intro_explains_numbered_item_instead_of_title_only():
    page = PageKnowledge(
        url="https://example.test/problems", title="Your problem list", purpose="Your problem list",
        visible_facts=[], fingerprint="problems",
    )
    from productlens.presentation.editorial import _page_intro_from_fact

    narration = _page_intro_from_fact(page, "#141 Linked List Cycle.")
    assert "representative practice item" in narration
    assert "highlights #141" not in narration


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
    assert narration == "Atlas is a routing engine and user dashboard."
    assert "section visibly presents" not in narration.lower()


def test_scroll_scene_recovers_page_local_content_fact_when_heading_differs():
    """A landmark heading must not collapse into a generic scroll caption."""
    root = "https://example.test/"
    page = PageKnowledge(
        url=root, title="Portfolio", purpose="featured work",
        visible_sections=["Selected work"],
        visible_facts=[
            "Selected work :: Atlas connects scheduling, availability, and team handoffs in one workflow."
        ], fingerprint="portfolio",
    )
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO, intent="Explain the selected work cards",
        target=Target(name="Selected work cards", source_url=root),
        covered_content_groups=["Selected work cards"],
    )
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(
        objective="Full walkthrough", narrative_goal="demo", audience="prospect",
        target_duration_seconds=60, selected_workflow="portfolio",
        workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
            id="work", intent=operation.intent, operation=operation,
        )], expected_outcomes=["selected work"], viewport_strategy="native", stop_conditions=["done"],
    )
    context = ProductContext(
        url=root, title="Portfolio", application_type="portfolio", visible_text="Portfolio",
        page_knowledge=[page], confidence=1,
    )
    board = build_editorial_storyboard(context, plan)
    scene = board.scenes[1]
    assert "scheduling" in scene.narration.lower()
    assert any(ref.startswith("fact:") for ref in scene.evidence)


def test_category_landmark_prefers_descriptive_fact_over_structural_fallback():
    """Dense pages must narrate a landmark's description when one is captured."""
    root = "https://example.test/systems"
    page = PageKnowledge(
        url=root, title="System Architecture", purpose="system architecture",
        visible_sections=["The Challenge & Bottlenecks"],
        visible_facts=[
            "The Challenge & Bottlenecks :: Ingesting call audio files directly in single request cycles caused server timeouts and out-of-memory errors when processing multi-hour files during high traffic peaks.",
        ], fingerprint="systems",
    )
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO, intent="Explain the challenge and its solution",
        target=Target(name="The Challenge & Bottlenecks", source_url=root),
        covered_content_groups=["The Challenge & Bottlenecks"],
    )
    step = __import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
        id="challenge", intent=operation.intent, operation=operation,
    )
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan"]).DemoPlan(
        objective="Full walkthrough", narrative_goal="demo", audience="prospect",
        target_duration_seconds=90, selected_workflow="systems", workflow_steps=[step],
        expected_outcomes=["challenge"], viewport_strategy="native", stop_conditions=["done"],
    )
    context = ProductContext(
        url=root, title="System Architecture", application_type="portfolio",
        visible_text="System Architecture The Challenge & Bottlenecks", page_knowledge=[page], confidence=1,
    )
    narration = build_editorial_storyboard(context, plan).scenes[1].narration
    assert "timeouts" in narration.lower()
    assert "traffic peaks" in narration.lower()
    assert "group brings the visible tools together" not in narration.lower()


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


def test_editorial_qa_rejects_workspace_chapter_without_product_value():
    """A successful click must not make route-mechanics narration deliverable."""
    now = datetime.now(UTC)
    operation = SemanticOperation(
        kind=OperationKind.VERIFY_STATE,
        intent="Explain bookings",
        target=Target(name="Bookings", text="Bookings"),
        page_url="https://example.test/bookings",
    )
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(
        objective="walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=60,
        selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="one", intent=operation.intent, operation=operation)],
        expected_outcomes=["Bookings"], viewport_strategy="native", stop_conditions=["done"],
    )
    context = ProductContext(
        url="https://example.test/", title="Example", application_type="dashboard",
        visible_text="Bookings appointments can be filtered by status and date.",
        elements=[ObservedElement(tag="h1", name="Bookings", selector="#bookings", text="Bookings")], confidence=1,
        page_knowledge=[PageKnowledge(url="https://example.test/bookings", title="Bookings", purpose="Appointments", fingerprint="bookings-v1", visible_facts=["Bookings appointments can be filtered by status and date."])],
    )
    board = build_editorial_storyboard(context, plan)
    event = InteractionEvent(operation_id=operation.id, kind=operation.kind, intent=operation.intent, occurred_at=now + timedelta(seconds=8), success=True, duration_ms=6_000)
    trace = DemoTrace(run_id="run", objective="walkthrough", started_at=now, recording_started_at=now, events=[event], outcome_verified=True)
    report = inspect_editorial(
        context=context, plan=plan, trace=trace, storyboard=board,
        script=[{"event_id": event.id, "text": "This workspace establishes the current working view before we demonstrate the visible workflow."}],
    )
    assert "GENERIC_ROUTE_LABEL_CAPTION" in report["hard_failures"]


def test_editorial_qa_rejects_a_scroll_trace_with_no_actual_motion():
    now = datetime.now(UTC)
    operation = SemanticOperation(kind=OperationKind.SCROLL_TO, intent="Explore activity", target=Target(name="Activity", text="Activity"))
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(
        objective="walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=60,
        selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(id="one", intent=operation.intent, operation=operation)],
        expected_outcomes=["activity"], viewport_strategy="native", stop_conditions=["done"],
    )
    context = ProductContext(url="https://example.test/", title="Example", application_type="dashboard", visible_text="Activity", elements=[ObservedElement(tag="h2", name="Activity", selector="#activity", text="Activity is displayed in a chronological list.")], confidence=1)
    board = build_editorial_storyboard(context, plan)
    event = InteractionEvent(
        operation_id=operation.id, kind=operation.kind, intent=operation.intent,
        occurred_at=now + timedelta(seconds=8), success=True, duration_ms=6_000,
        after={"scroll_motion": {"start_y": 0, "target_y": 0, "path": []}},
    )
    trace = DemoTrace(run_id="run", objective="walkthrough", started_at=now, recording_started_at=now, events=[event], outcome_verified=True)
    report = inspect_editorial(context=context, plan=plan, trace=trace, storyboard=board, script=editorial_script(board, {operation.id: event.id}))
    assert "SCROLL_SCENE_HAS_NO_CONTINUOUS_MOTION" in report["hard_failures"]


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


@pytest.mark.asyncio
async def test_editorial_writer_rejects_reading_dwell_boilerplate():
    """A timing explanation must not replace a scene's product takeaway."""
    context = ProductContext(
        url="https://example.test/", title="Example", application_type="dashboard",
        visible_text="Engineering notes explain queue retries and failure recovery.",
        elements=[ObservedElement(
            tag="h2", name="Engineering Notes", selector="#notes",
            text="Engineering notes explain queue retries and failure recovery.",
        )], confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO, intent="Explore Engineering Notes",
        target=Target(name="Engineering Notes", text="Engineering Notes"),
    )
    plan = __import__("productlens.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]).DemoPlan(
        objective="walkthrough", narrative_goal="demo", audience="prospect", target_duration_seconds=60,
        selected_workflow="demo", workflow_steps=[__import__("productlens.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
            id="one", intent=operation.intent, operation=operation,
        )], expected_outcomes=["notes"], viewport_strategy="native", stop_conditions=["done"],
    )
    board = build_editorial_storyboard(context, plan)

    class Writer:
        async def structured(self, _prompt, schema):
            assert schema is EditorialNarrationDraft
            return EditorialNarrationDraft(lines=[
                EditorialNarrationLine(
                    id=scene.id,
                    narration="This Engineering Notes workspace keeps the section in view while observed details are read before the walkthrough continues.",
                )
                for scene in board.scenes if scene.operation_id is not None
            ])

    enriched = await enrich_editorial_storyboard(context, board, Writer())
    assert enriched == board
