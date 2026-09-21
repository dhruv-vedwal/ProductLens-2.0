from datetime import UTC, datetime, timedelta

import pytest

from app.contracts.models import (
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
    Postcondition,
    ProductContext,
    SemanticOperation,
    Target,
    WorkflowProposal,
)
from app.narration.script import bind_opening_to_first_event
from app.planning.production import ProductionPlanningService
from app.presentation.editorial import (
    _caption_length_bound,
    _distinct_editorial_narration,
    _label_reference,
    _looks_like_screen_transcript,
    _mentions_scene_element,
    _mentions_scene_subject,
    _observed_narration,
    _repair_fragmented_editorial_copy,
    _safe_editorial_text,
    _scene_source,
    _summary_from_categories,
    _summary_from_collection,
    _summary_from_schedule,
    _target_fact_narration,
    _viewer_ready,
    editorial_script,
    narrated_storyboard_scenes,
)
from app.providers.errors import ProviderError


def test_placeholder_copy_is_not_used_as_editorial_evidence():
    assert (
        _safe_editorial_text(
            "Heading Lorem ipsum dolor sit amet, consectetur adipisicing elit, sed do eiusmod tempor."
        )
        == ""
    )


def test_placeholder_editor_verify_uses_document_identity_not_starter_heading():
    root = "https://editor.example/"
    page = PageKnowledge(
        url=root,
        title="Untitled Diagram - editor",
        purpose="Heading",
        visible_sections=["Heading", "svg workspace"],
        visible_facts=[
            "Heading :: Heading Lorem ipsum dolor sit amet, consectetur adipisicing elit."
        ],
        fingerprint="fixture",
    )
    context = ProductContext(
        url=root,
        title=page.title,
        application_type="web_application",
        objective=ObjectiveSpec(raw="complete walkthrough", demo_type="full_walkthrough"),
        page_knowledge=[page],
        confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.VERIFY_STATE,
        intent="Hold on the evidenced workspace",
        target=Target(name=page.title, selector="body", source_url=root),
        page_url=root,
    )

    narration = _observed_narration(context, operation, page.title)

    assert "Heading" not in narration
    assert "lorem ipsum" not in narration.lower()
    assert "control in focus" in narration.lower()
    assert "untitled diagram" in narration.lower()
    assert "next view" not in narration.lower()
    assert "activates" not in narration.lower()


def test_generic_accessibility_labels_are_not_presented_as_opening_sections():
    from app.presentation.editorial import _meaningful_section_label

    assert _meaningful_section_label("Heading") == ""
    assert _meaningful_section_label("Description") == ""
    assert _meaningful_section_label("svg workspace") == ""
    assert _meaningful_section_label("File") == ""
    assert _meaningful_section_label("Projects") == "Projects"
    legitimate = "This guide explains why lorem ipsum is useful as temporary layout copy."
    assert _safe_editorial_text(legitimate) == legitimate
    assert _safe_editorial_text("List Item 1 Item 2 Item 3") == ""


def test_category_summary_handles_missing_page_fact():
    assert _summary_from_categories(None, "Categories") == ""


def test_viewer_ready_rejects_fragmented_evidence_inventory():
    assert not _viewer_ready("Here, Excalidraw Shapes Canvas actions svg workspace P.", "Shapes")


def test_screen_transcript_gate_rejects_grounded_label_concatenation():
    evidence = (
        "Week 5 :: Week 5 Next.js patterns + queues + FastAPI CRUD. "
        "Builds this week :: Builds this week Check off when finished without AI "
        "Next RSC demo Queue + worker FastAPI CRUD"
    )
    transcript = "Week 5 highlights builds this week Check off when finished without AI Next RSC demo Queue + worker FastAPI CRUD."
    summary = "The Week 5 view groups the visible builds for this stage and keeps the next items ready to review."
    assert _looks_like_screen_transcript(transcript, evidence)
    assert not _looks_like_screen_transcript(summary, evidence)


def test_screen_transcript_gate_keeps_multi_predicate_technical_explanation():
    evidence = (
        "Resiliency & Failure Recovery :: failed transcription tasks are insulated with "
        "exponential backoff and placed in an AWS SQS Dead Letter Queue for engineering analysis."
    )
    narration = (
        "The Resiliency & Failure Recovery highlights failed transcription tasks are insulated "
        "with exponential backoff and placed in an AWS SQS Dead Letter Queue for engineering analysis."
    )
    assert not _looks_like_screen_transcript(narration, evidence)


def test_target_fact_narration_skips_dense_inventory_evidence():
    page = PageKnowledge(
        url="https://example.test/demo",
        title="Demo",
        purpose="Live preview",
        fingerprint="demo-v1",
        visible_facts=[
            "Website :: A calendar of 21 appointments across three stylists Each stylist with their own rota and services The service menu customers book from, with real timings See site and dashboard Website Dashboard.",
            "A neighbourhood restaurant :: A neighbourhood restaurant with online ordering and a booking diary.",
        ],
    )
    narration = _target_fact_narration(page, "Website")
    assert not _looks_like_screen_transcript(narration, " ".join(page.visible_facts))


def test_fragmented_heading_copy_is_rewritten_as_viewer_prose():
    scene = EditorialScene(
        id="scene-pricing",
        operation_id="op-pricing",
        title="Free",
        purpose="Inspect pricing",
        narration="Free highlights everything you need to explore the builder and launch a project.",
        evidence=["page:https://example.test/pricing"],
        interaction="observe",
        required_dwell_seconds=4,
        completion_criteria=["pricing is readable"],
        page_url="https://example.test/pricing",
    )
    repaired = _repair_fragmented_editorial_copy(
        ProductContext(
            url="https://example.test/pricing", title="Demo", application_type="web_application"
        ),
        [scene],
    )[0]
    assert repaired.narration.startswith("The Free option includes")


def test_malformed_provider_heading_is_repaired_as_grounded_prose():
    scene = EditorialScene(
        id="malformed",
        operation_id="op-malformed",
        title="Redis Caching",
        purpose="Explain the cache view",
        narration=(
            "The Redis Caching option includes we examine the visible cache strategy and fallbacks."
        ),
        evidence=["section:Redis Caching"],
        interaction="scroll",
        required_dwell_seconds=4,
        completion_criteria=["cache is readable"],
    )
    repaired = _repair_fragmented_editorial_copy(
        ProductContext(
            url="https://example.test/", title="Demo", application_type="web_application"
        ),
        [scene],
    )[0]
    assert repaired.narration == (
        "The Redis Caching highlights the visible cache strategy and fallbacks."
    )


def test_category_summary_uses_viewer_oriented_structure_not_inventory_dump():
    summary = _summary_from_categories(
        "BACKEND SERVICES & REAL-TIME LANGUAGES & RUNTIMES FastAPI React Node",
        "Core Engineering",
    )
    # Stub: deterministic category recipes no longer invent presenter prose.
    assert summary == ""


def test_click_narration_explains_observed_state_change_when_page_inventory_is_noisy():
    page = PageKnowledge(
        url="https://example.test/",
        title="Whiteboard",
        purpose="Whiteboard",
        visible_facts=["Whiteboard Shapes Canvas actions svg workspace P"],
        fingerprint="whiteboard",
    )
    context = ProductContext(
        url=page.url,
        title="Whiteboard",
        application_type="web_application",
        page_knowledge=[page],
        objective=ObjectiveSpec(raw="show the drawing workflow", demo_type="feature_walkthrough"),
        confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.CLICK,
        intent="Activate the observed Draw drawing tool",
        target=Target(name="Draw", role="button"),
        page_url=page.url,
    )
    narration = _observed_narration(context, operation, "Draw")
    assert "draw" in narration.lower()
    assert "control in focus" in narration.lower()
    assert "activates" not in narration.lower()
    assert "next product state" not in narration.lower()


def test_visual_operation_narration_precedes_repeated_editor_page_fact():
    page = PageKnowledge(
        url="https://example.test/",
        title="Diagram editor",
        purpose="Diagram editor",
        visible_facts=["Diagram editor :: Your drawings are saved in your browser's storage."],
        fingerprint="diagram-editor",
    )
    context = ProductContext(
        url=page.url,
        title=page.title,
        application_type="web_application",
        page_knowledge=[page],
        objective=ObjectiveSpec(raw="draw a system diagram", demo_type="feature_walkthrough"),
        confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.POINTER_SEQUENCE,
        intent="Draw the 'Database' rectangle on the canvas.",
        target=Target(name="canvas workspace", selector="body", source_url=page.url),
        page_url=page.url,
    )
    narration = _observed_narration(context, operation, "canvas workspace")
    assert "database" in narration.lower()
    assert "drawn" in narration.lower()
    assert "saved in your browser" not in narration.lower()


def test_visual_operation_narration_handles_planner_variants_without_generic_filler():
    rectangle = SemanticOperation(
        kind=OperationKind.POINTER_SEQUENCE,
        intent="Draw a rectangle for the 'User Interface'.",
        target=Target(name="canvas workspace", selector="canvas"),
        value={"pattern": "shape_box"},
    )
    label = SemanticOperation(
        kind=OperationKind.POINTER_SEQUENCE,
        intent="Place the text cursor for the 'User Interface' label.",
        target=Target(name="canvas workspace", selector="canvas"),
        value={"pattern": "text_placement"},
    )
    from app.presentation.editorial.narrative import _semantic_operation_narration

    assert "user interface" in _semantic_operation_narration(rectangle).casefold()
    assert "user interface" in _semantic_operation_narration(label).casefold()


def test_scene_source_keeps_semantic_target_evidence_without_an_element_record():
    scene = EditorialScene(
        id="scene-draw",
        operation_id="op-draw",
        title="Draw",
        purpose="Activate Draw",
        narration="The Draw control activates the drawing tool.",
        evidence=["page:https://example.test/", "element:Draw"],
        interaction="click",
        required_dwell_seconds=3,
        completion_criteria=["target is visible"],
        page_url="https://example.test/",
    )
    context = ProductContext(
        url="https://example.test/",
        title="Whiteboard",
        application_type="web_application",
        page_knowledge=[
            PageKnowledge(
                url="https://example.test/",
                title="Whiteboard",
                purpose="Whiteboard",
                fingerprint="x",
            )
        ],
        objective=ObjectiveSpec(raw="show the drawing workflow"),
        confidence=1,
    )
    assert "Draw" in _scene_source(context, scene)


def test_verify_scene_uses_page_local_sections_when_no_sentence_fact_exists():
    page = PageKnowledge(
        url="https://example.test/elements",
        title="Elements",
        purpose="Elements",
        visible_sections=["Buttons", "Text Box", "Upload and Download"],
        visible_facts=["Buttons Text Box Upload and Download"],
        fingerprint="elements",
    )
    context = ProductContext(
        url="https://example.test/",
        title="Demo",
        application_type="demo",
        page_knowledge=[page],
        objective=ObjectiveSpec(raw="complete walkthrough", demo_type="full_walkthrough"),
        confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.VERIFY_STATE,
        intent="Verify the page",
        target=Target(name="Demo", selector="body"),
        page_url=page.url,
    )
    narration = _observed_narration(context, operation, "Demo")
    assert "control in focus" in narration.lower()
    assert "next view" not in narration.lower()
    assert "activates" not in narration.lower()
    assert "next part of the walkthrough" not in narration.lower()


def test_editorial_script_does_not_replace_distinct_navigation_payloads_with_checkpoint_copy():
    first = EditorialScene(
        id="scene-a",
        operation_id="op-a",
        title="Alerts",
        purpose="Open Alerts",
        narration="The Alerts page brings dialogs and frames into view, showing how this area is organized.",
        evidence=["page:https://example.test/alerts", "element:Alerts"],
        interaction="navigate",
        required_dwell_seconds=4,
        completion_criteria=["page is readable"],
        page_url="https://example.test/alerts",
    )
    second = first.model_copy(
        update={
            "id": "scene-b",
            "operation_id": "op-b",
            "title": "Widgets",
            "narration": "The Widgets page brings sliders and accordions into view, showing how this area is organized.",
            "evidence": ["page:https://example.test/widgets", "element:Widgets"],
            "page_url": "https://example.test/widgets",
        }
    )
    result = _distinct_editorial_narration(
        second, second.narration, [first.narration], evidence="Widgets sliders accordions"
    )
    assert result == second.narration
    assert "checkpoint" not in result.casefold()


def test_schedule_summary_requires_observed_schedule_semantics_not_timestamps_alone():
    table = "Created 3:37 AM, 9th Sep 2026 Updated 3:45 AM, 9th Sep 2026"
    assert _summary_from_schedule(table, "Records") == ""
    schedule = "Upcoming appointments schedule 9:00 AM and 10:30 AM"
    # Stub: deterministic schedule recipes no longer invent presenter prose.
    assert _summary_from_schedule(schedule, "Appointments") == ""


def test_caption_length_bound_keeps_the_longest_informative_sentence():
    text = "Ready to plan? Create a roadmap now. Runs in your browser, saves to your device. No account, no setup."
    result = _caption_length_bound(text, maximum_words=10)
    assert result == "Runs in your browser, saves to your device."


def test_ui_label_reference_keeps_possessive_labels_grammatical_and_exact():
    assert _label_reference("YOUR NAME") == "“YOUR NAME”"
    assert _label_reference("Email address") == "the email address"


def test_viewer_ready_allows_an_explanatory_named_feature_but_not_a_title_dump():
    assert _viewer_ready(
        "Chit Chat Connect is a real-time messaging product with synchronized presence and typing state.",
        "Chit Chat Connect",
    )


def test_viewer_ready_accepts_grounded_editorial_predicates():
    assert _viewer_ready(
        "The workspace highlights the active projects and the outcomes they support.",
        "Featured work",
    )
    assert _viewer_ready(
        "This section presents the observed career progression and contributions.",
        "Career timeline",
    )


def test_viewer_ready_accepts_articles_and_includes_after_named_ui_title():
    """Natural captions must not fail because a UI label contains grammar words."""
    assert _viewer_ready(
        "The Lead Management option includes capture, qualify, and manage leads.",
        "Lead Management",
    )
    assert _viewer_ready(
        "The add a remark input field preserves the context that helps a teammate understand this record.",
        "Add a remark input",
    )


def test_form_narration_explains_the_flow_instead_of_reading_the_input_label():
    context = ProductContext(
        url="https://example.test/leads",
        title="Example",
        application_type="dashboard",
    )
    operation = SemanticOperation(
        kind=OperationKind.FILL_PHONE,
        intent="Enter the observed Enter Phone Number value",
        target=Target(name="Enter Phone Number"),
    )

    narration = _observed_narration(context, operation, "Enter Phone Number")

    assert "Enter Phone Number" not in narration
    assert "phone" in narration.casefold()
    assert "control in focus" in narration.casefold()
    assert "traceable example" not in narration.casefold()
    assert "lead" not in narration.casefold()


def test_navigation_narration_introduces_destination_purpose_from_local_evidence():
    context = ProductContext(
        url="https://example.test/",
        title="Example",
        application_type="app",
        page_knowledge=[
            PageKnowledge(
                url="https://example.test/help",
                title="Help",
                purpose="Plan locally and share only when you choose",
                fingerprint="help",
                visible_facts=[
                    "Plan locally and share only when you choose. Start in this browser, export portable backups, and enable sync when collaboration is needed."
                ],
            )
        ],
    )
    operation = SemanticOperation(
        kind=OperationKind.OPEN_NAVIGATION_ITEM,
        intent="Open Help",
        target=Target(name="Help", source_url="https://example.test/"),
        page_url="https://example.test/help",
    )
    narration = _observed_narration(context, operation, "Help")
    lowered = narration.casefold()
    assert "browser" in lowered or "backup" in lowered or "sync" in lowered or "share" in lowered
    assert "next part of the walkthrough" not in lowered
    assert "next view" not in lowered
    assert "activates" not in lowered


def test_text_field_narration_keeps_the_field_role_grounded():
    context = ProductContext(
        url="https://example.test/roadmap",
        title="Example",
        application_type="web_application",
    )
    operation = SemanticOperation(
        kind=OperationKind.FILL_TEXT,
        intent="Enter the observed Roadmap title value",
        target=Target(name="Roadmap title"),
    )

    narration = _observed_narration(context, operation, "Roadmap title")

    assert "roadmap title" in narration.casefold()
    assert "control in focus" in narration.casefold()
    assert "recognizable identity" not in narration.casefold()


def test_editorial_script_compresses_repeated_scroll_landmarks_without_losing_navigation_or_closing():
    scenes = [
        EditorialScene(
            id=f"scene-{index}",
            operation_id=f"operation-{index}",
            title=f"Detail {index}",
            purpose="Explain observed evidence",
            narration=f"This observed detail {index} explains why the product flow matters to the viewer.",
            evidence=["page:https://example.test/work"],
            interaction="scroll",
            required_dwell_seconds=3,
            completion_criteria=["visible"],
            page_url="https://example.test/work",
        )
        for index in range(14)
    ]
    scenes.insert(
        0,
        EditorialScene(
            id="opening",
            title="Opening",
            purpose="Establish",
            narration="Welcome to the observed product experience.",
            evidence=["page:https://example.test/"],
            interaction="opening",
            required_dwell_seconds=5,
            completion_criteria=["stable"],
        ),
    )
    scenes.insert(
        4,
        EditorialScene(
            id="navigation",
            operation_id="navigation",
            title="Timeline",
            purpose="Enter career history",
            narration="This timeline connects visible roles into a clear progression of engineering responsibility.",
            evidence=["page:https://example.test/timeline"],
            interaction="navigate",
            required_dwell_seconds=4,
            completion_criteria=["visible"],
            page_url="https://example.test/timeline",
        ),
    )
    scenes.append(
        EditorialScene(
            id="closing",
            operation_id="closing",
            title="Contact",
            purpose="Close journey",
            narration="The contact path provides a clear next step for continuing the conversation.",
            evidence=["page:https://example.test/contact"],
            interaction="click",
            required_dwell_seconds=4,
            completion_criteria=["visible"],
            page_url="https://example.test/contact",
            story_phase="close",
        )
    )
    board = EditorialStoryboard(
        brief=EditorialBrief(
            title="Example", product_purpose="Example", opening_message="Welcome", facts=[]
        ),
        scenes=scenes,
        minimum_duration_seconds=60,
    )
    selected = narrated_storyboard_scenes(board)
    assert [scene.id for scene in selected].count("navigation") == 1
    assert [scene.id for scene in selected].count("closing") == 1
    assert sum(scene.interaction == "scroll" for scene in selected) == 14
    script = editorial_script(
        board, {scene.operation_id: f"event-{scene.id}" for scene in scenes if scene.operation_id}
    )
    assert [line["event_id"] for line in script] == [f"event-{scene.id}" for scene in selected]
    assert not _viewer_ready("Chit Chat Connect", "Chit Chat Connect")
    assert not _viewer_ready(
        "System Designs is a visual exploration of the core system designs.",
        "System Designs",
    )


def test_editorial_script_keeps_one_caption_for_one_proven_event():
    opening = EditorialScene(
        id="opening",
        title="Opening",
        purpose="Establish",
        narration="Welcome to the observed product.",
        evidence=["page:https://example.test/"],
        interaction="opening",
        required_dwell_seconds=4,
        completion_criteria=["stable"],
    )
    first = EditorialScene(
        id="first",
        operation_id="same",
        title="Workspace",
        purpose="Explain",
        narration="The workspace shows the verified workflow.",
        evidence=["page:https://example.test/work"],
        interaction="observe",
        required_dwell_seconds=3,
        completion_criteria=["visible"],
    )
    duplicate = EditorialScene(
        id="duplicate",
        operation_id="also-same",
        title="Repeated state",
        purpose="Explain",
        narration="This must not become a second caption for the same event.",
        evidence=["page:https://example.test/work"],
        interaction="observe",
        required_dwell_seconds=3,
        completion_criteria=["visible"],
    )
    board = EditorialStoryboard(
        brief=EditorialBrief(
            title="Example", product_purpose="Example", opening_message="Welcome", facts=[]
        ),
        scenes=[opening, first, duplicate],
        minimum_duration_seconds=20,
    )
    script = editorial_script(board, {"same": "event-1", "also-same": "event-1"})
    assert [line["event_id"] for line in script] == ["event-1"]
    assert script[0]["opening"] is True


def test_editorial_script_combines_welcome_and_page_takeaway_when_one_event_exists():
    opening = EditorialScene(
        id="opening",
        title="Opening",
        purpose="Establish",
        narration="Welcome to the diagram workspace. Today I will show how its visible tools fit together.",
        evidence=["page:https://example.test/"],
        interaction="opening",
        required_dwell_seconds=5,
        completion_criteria=["stable"],
    )
    first = EditorialScene(
        id="first",
        operation_id="verify",
        title="Diagram workspace",
        purpose="Explain the opening surface",
        narration="The diagram workspace keeps the canvas and visible editing tools together for a clear starting point.",
        evidence=["page:https://example.test/", "element:Diagram workspace"],
        interaction="observe",
        required_dwell_seconds=4,
        completion_criteria=["visible"],
    )
    board = EditorialStoryboard(
        brief=EditorialBrief(title="Diagram", product_purpose="Diagram", opening_message="Welcome"),
        scenes=[opening, first],
        minimum_duration_seconds=20,
    )
    script = editorial_script(board, {"verify": "event-1"})
    assert len(script) == 1
    assert script[0]["opening"] is True
    assert "welcome to the diagram workspace" in str(script[0]["text"]).lower()
    assert "canvas" in str(script[0]["text"]).lower()


def test_editorial_script_preserves_dotted_product_identity_in_opening():
    opening = EditorialScene(
        id="opening",
        title="Opening",
        purpose="Establish",
        narration="Welcome to draw.io. Today I will walk through the diagram workspace.",
        evidence=["page:https://example.test/"],
        interaction="opening",
        required_dwell_seconds=5,
        completion_criteria=["stable"],
    )
    scene = EditorialScene(
        id="first",
        operation_id="verify",
        title="Diagram workspace",
        purpose="Explain",
        narration="The diagram workspace keeps the canvas visible for review.",
        evidence=["page:https://example.test/", "element:Diagram workspace"],
        interaction="observe",
        required_dwell_seconds=4,
        completion_criteria=["visible"],
    )
    board = EditorialStoryboard(
        brief=EditorialBrief(title="draw.io", product_purpose="draw.io", opening_message="Welcome"),
        scenes=[opening, scene],
        minimum_duration_seconds=20,
    )
    line = editorial_script(board, {"verify": "event-1"})[0]["text"]
    assert "draw.io" in str(line)
    assert "draw. io" not in str(line)


def test_opening_line_binds_to_first_successful_readiness_event():
    opening = {"event_id": "navigation", "opening": True, "text": "Welcome to the product."}
    readiness = InteractionEvent(
        id="readiness",
        operation_id="verify",
        kind=OperationKind.VERIFY_STATE,
        intent="Establish the opening page",
        success=True,
        duration_ms=100,
    )
    navigation = InteractionEvent(
        id="navigation",
        operation_id="nav",
        kind=OperationKind.OPEN_NAVIGATION_ITEM,
        intent="Open the next page",
        success=True,
        duration_ms=100,
    )
    trace = DemoTrace(
        run_id="run",
        objective="walkthrough",
        started_at=datetime.now(UTC),
        events=[readiness, navigation],
    )
    bound = bind_opening_to_first_event([opening], trace)
    assert bound[0]["event_id"] == "readiness"
    assert bound[0]["opening"] is True


def test_editorial_script_keeps_first_narrated_scene_when_readiness_precedes_it():
    opening = EditorialScene(
        id="opening",
        title="Opening",
        purpose="Establish",
        narration="Welcome to the observed product experience.",
        evidence=["page:https://example.test/"],
        interaction="opening",
        required_dwell_seconds=5,
        completion_criteria=["stable"],
    )
    scene = EditorialScene(
        id="scene-1",
        operation_id="nav",
        title="Workspace",
        purpose="Explain workspace",
        narration="The workspace organizes the visible controls for review.",
        evidence=["page:https://example.test/workspace", "element:Workspace"],
        interaction="navigate",
        required_dwell_seconds=3,
        completion_criteria=["visible"],
    )
    board = EditorialStoryboard(
        brief=EditorialBrief(title="Example", product_purpose="Example", opening_message="Welcome"),
        scenes=[opening, scene],
        minimum_duration_seconds=20,
    )
    lines = editorial_script(board, {"nav": "event-nav"}, opening_event_id="event-ready")
    assert [line["event_id"] for line in lines] == ["event-ready", "event-nav"]
    assert lines[0]["opening"] is True
    assert "workspace" in str(lines[1]["text"]).lower()


def test_editorial_script_rephrases_duplicate_evidence_beats_without_inventing_facts():
    opening = EditorialScene(
        id="opening",
        title="Opening",
        purpose="Establish",
        narration="Welcome to the observed product experience.",
        evidence=["page:https://example.test/"],
        interaction="opening",
        required_dwell_seconds=5,
        completion_criteria=["stable"],
    )
    first = EditorialScene(
        id="first",
        operation_id="op-first",
        title="Week 1",
        purpose="Explain the visible week plan",
        narration="The Week 1 view shows the builds planned this week, including implementation practice to review next.",
        evidence=["page:https://example.test/weeks", "section:Week 1", "section:Builds"],
        interaction="scroll",
        required_dwell_seconds=4,
        completion_criteria=["visible"],
        page_url="https://example.test/weeks",
    )
    second = first.model_copy(update={"id": "second", "operation_id": "op-second"})
    board = EditorialStoryboard(
        brief=EditorialBrief(
            title="Example", product_purpose="Example", opening_message="Welcome", facts=[]
        ),
        scenes=[opening, first, second],
        minimum_duration_seconds=45,
    )
    lines = editorial_script(board, {"op-first": "event-first", "op-second": "event-second"})
    assert lines[0]["text"] != lines[1]["text"]
    assert "Week" in lines[1]["text"] or "Builds" in lines[1]["text"]


def test_model_narration_must_retain_named_scene_subject():
    scene = EditorialScene(
        id="internship",
        operation_id="op",
        title="Software Development Intern",
        purpose="Explain the observed role",
        narration="Observed role contribution.",
        evidence=[
            "page:https://example.test/resume",
            "element:Datansh Solutions",
            "element:Software Development Intern",
        ],
        interaction="scroll",
        required_dwell_seconds=4,
        completion_criteria=["visible"],
        page_url="https://example.test/resume",
    )
    assert not _mentions_scene_element(
        scene, "Engineered workflow automation interfaces and HRMS schemas."
    )
    assert _mentions_scene_element(
        scene, "At Datansh Solutions, the internship focused on workflow automation."
    )
    assert not _mentions_scene_subject(scene, "The internship focused on workflow automation.")
    assert _mentions_scene_subject(
        scene, "The Software Development Intern role focused on workflow automation."
    )


def test_collection_fallback_is_domain_neutral_for_non_study_products():
    narration = _summary_from_collection(
        "EVENT-DRIVEN BACKEND AI PRODUCT SYSTEMS FULL STACK PRODUCT DELIVERY",
        "Core Engineering Capabilities",
    )

    # Stub: deterministic collection recipes no longer invent presenter prose.
    assert narration == ""
    assert "problem-solving" not in narration


def test_category_landmark_uses_viewer_value_instead_of_route_mechanics():
    root = "https://example.test/resume"
    context = ProductContext(
        url=root,
        title="Example portfolio",
        application_type="portfolio",
        page_knowledge=[
            PageKnowledge(
                url=root,
                title="Professional History",
                purpose="professional history",
                visible_facts=[
                    "DATABASES, TOOLS & CLOUD :: DATABASES, TOOLS & CLOUD MongoDB PostgreSQL Redis AWS (SES, SQS) Prisma Sequelize",
                ],
                fingerprint="resume",
            )
        ],
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
    assert "control in focus" in narration.casefold()
    assert "databases" in narration.casefold() and "cloud" in narration.casefold()
    assert "next view" not in narration.casefold()
    assert "activates" not in narration.casefold()
    assert "next part of the walkthrough" not in narration.casefold()


def test_category_landmark_without_exact_fact_uses_safe_fallback_instead_of_crashing():
    root = "https://example.test/dashboard"
    context = ProductContext(
        url=root,
        title="OnlyDash",
        application_type="dashboard",
        page_knowledge=[
            PageKnowledge(
                url=root,
                title="OnlyDash",
                purpose="analytics workspace",
                visible_facts=["Users 213 42 19.7%"],
                fingerprint="onlydash",
            )
        ],
        confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Review integrations",
        target=Target(name="Integrations", source_url=root),
    )
    narration = _observed_narration(context, operation, "Integrations")
    assert narration
    assert "Integrations" in narration


def test_grouped_scroll_narration_explains_each_visible_subject():
    root = "https://example.test/"
    context = ProductContext(
        url=root,
        title="Example portfolio",
        application_type="portfolio",
        page_knowledge=[
            PageKnowledge(
                url=root,
                title="Example portfolio",
                purpose="featured work",
                visible_facts=[
                    "Project Alpha :: A realtime collaboration workspace for distributed teams.",
                    "Project Beta :: An analytics dashboard that turns event streams into clear reports.",
                ],
                fingerprint="home",
            )
        ],
        elements=[
            ObservedElement(
                tag="h3",
                name="Project Alpha",
                selector="#alpha",
                source_url=root,
                text="A realtime collaboration workspace for distributed teams.",
            ),
            ObservedElement(
                tag="h3",
                name="Project Beta",
                selector="#beta",
                source_url=root,
                text="An analytics dashboard that turns event streams into clear reports.",
            ),
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
    lowered = narration.casefold()
    # Drafts are fact-or-skeleton for the primary target; LLM enrich owns multi-subject polish.
    assert "project beta" in lowered
    assert (
        "analytics" in lowered
        or "dashboard" in lowered
        or "control in focus" in lowered
        or "realtime" in lowered
    )
    assert "next view" not in lowered
    assert "activates" not in lowered
    assert "next part of the walkthrough" not in lowered


def test_editorial_script_preserves_the_approved_opening_instead_of_rebuilding_it():
    context = ProductContext(
        url="https://example.test/",
        title="Example",
        application_type="dashboard",
        visible_text="Example workspace",
        confidence=1,
        page_knowledge=[
            PageKnowledge(
                url="https://example.test/",
                title="Example",
                purpose="workspace",
                visible_facts=["Workspace :: A shared place to review account activity."],
                fingerprint="opening",
            )
        ],
    )
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Review activity",
        target=Target(name="Activity", text="Account activity", source_url=context.url),
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="explain",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one", intent=operation.intent, operation=operation
            )
        ],
        expected_outcomes=["activity"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    board = build_editorial_storyboard(context, plan)
    approved = "Here is the shared workspace and why it matters for reviewing activity."
    board = board.model_copy(
        update={
            "scenes": [board.scenes[0].model_copy(update={"narration": approved}), board.scenes[1]]
        }
    )
    script = editorial_script(board, {operation.id: "event-1"})
    assert script[0]["text"].startswith(approved)


def test_editorial_script_does_not_cut_opening_mid_sentence():
    opening = EditorialScene(
        id="opening",
        title="Opening",
        purpose="Establish",
        narration=(
            "Welcome to the observed product. Today I will walk through the workspace. "
            "The opening view establishes the context for the workflow before we"
        ),
        evidence=["page:https://example.test/"],
        interaction="opening",
        required_dwell_seconds=5,
        completion_criteria=["stable opening"],
    )
    first = EditorialScene(
        id="first",
        title="Workspace",
        purpose="Workspace",
        operation_id="op",
        narration="The workspace keeps the relevant state visible for the viewer.",
        evidence=["page:https://example.test/"],
        interaction="observe",
        required_dwell_seconds=4,
        completion_criteria=["workspace visible"],
    )
    board = EditorialStoryboard(
        brief=EditorialBrief(
            title="Example", product_purpose="Example", opening_message="Welcome", facts=[]
        ),
        scenes=[opening, first],
        minimum_duration_seconds=20,
    )
    script = editorial_script(board, {"op": "event-1"})
    assert script[0]["text"].endswith("workspace.")
    assert not script[0]["text"].endswith("before we.")


from app.presentation.editorial import (
    _readable_fact,
    build_editorial_storyboard,
    enrich_editorial_storyboard,
)
from app.quality.editorial import inspect_editorial, inspect_editorial_preflight


class Planner:
    async def structured(self, prompt, schema):
        raise AssertionError("full walkthrough uses deterministic editorial recipe")


@pytest.mark.asyncio
async def test_portfolio_completes_home_before_visible_navigation():
    items = [
        ObservedElement(
            tag="section",
            name="Featured Systems",
            selector="#systems",
            text="A curated collection of end-to-end product work.",
        ),
        ObservedElement(
            tag="article",
            name="Chit-Chat Connect",
            selector="#chat",
            text="A real-time communication platform with duplex messaging.",
        ),
        ObservedElement(
            tag="a",
            name="Timeline",
            selector="a",
            href="/timeline",
            source_url="https://portfolio.test/",
        ),
        ObservedElement(
            tag="a",
            name="System Designs",
            selector="a",
            href="/systems",
            source_url="https://portfolio.test/",
        ),
        ObservedElement(
            tag="a",
            name="Engineering Notes",
            selector="a",
            href="/notes",
            source_url="https://portfolio.test/",
        ),
        ObservedElement(
            tag="a",
            name="Contact",
            selector="a",
            href="/contact",
            source_url="https://portfolio.test/",
        ),
        ObservedElement(
            tag="h1",
            name="Career history",
            selector="h1",
            source_url="https://portfolio.test/timeline",
        ),
        ObservedElement(
            tag="h1",
            name="System designs",
            selector="h1",
            source_url="https://portfolio.test/systems",
        ),
        ObservedElement(
            tag="h1",
            name="Engineering notes",
            selector="h1",
            source_url="https://portfolio.test/notes",
        ),
        ObservedElement(
            tag="h1", name="Contact", selector="h1", source_url="https://portfolio.test/contact"
        ),
    ]
    context = ProductContext(
        url="https://portfolio.test/",
        title="Dhruv Vedwal",
        application_type="portfolio",
        visible_text="System architect and engineer. Featured Systems.",
        elements=items,
        navigation=items[2:],
        page_knowledge=[
            PageKnowledge(
                url="https://portfolio.test/",
                title="Home",
                purpose="portfolio overview",
                visible_sections=["Featured Systems", "Chit-Chat Connect"],
                visible_facts=[
                    "Featured Systems :: A curated collection of end-to-end product work.",
                    "Chit-Chat Connect :: A real-time communication platform with duplex messaging.",
                ],
                fingerprint="home",
            ),
            PageKnowledge(
                url="https://portfolio.test/timeline",
                title="Timeline",
                purpose="career history",
                visible_sections=["Career history"],
                visible_facts=[
                    "Career history :: A chronological view of professional experience and contributions."
                ],
                fingerprint="timeline",
            ),
            PageKnowledge(
                url="https://portfolio.test/systems",
                title="Systems",
                purpose="system designs",
                visible_sections=["System designs"],
                visible_facts=[
                    "System designs :: Interactive architecture diagrams explain project decisions."
                ],
                fingerprint="systems",
            ),
            PageKnowledge(
                url="https://portfolio.test/notes",
                title="Notes",
                purpose="engineering notes",
                visible_sections=["Engineering notes"],
                visible_facts=["Engineering notes :: Articles document implementation trade-offs."],
                fingerprint="notes",
            ),
            PageKnowledge(
                url="https://portfolio.test/contact",
                title="Contact",
                purpose="contact",
                visible_sections=["Contact"],
                visible_facts=["Contact :: A message form provides a direct contact path."],
                fingerprint="contact",
            ),
        ],
        confidence=1,
    )
    plan = await ProductionPlanningService(Planner()).plan(
        objective="Create a full walkthrough of each tab", context=context
    )
    ops = [step.operation for step in plan.workflow_steps]
    first_navigation = next(
        index for index, op in enumerate(ops) if op.kind is OperationKind.OPEN_NAVIGATION_ITEM
    )
    assert all(op.kind is not OperationKind.NAVIGATE for op in ops[1:])
    home_targets = [op.target.name for op in ops[1:first_navigation]]
    assert home_targets[:2] == ["Featured Systems", "Chit-Chat Connect"]
    assert {"Featured Systems", "Chit-Chat Connect"} <= set(home_targets)


@pytest.mark.asyncio
async def test_complete_walkthrough_prefers_observed_primary_navigation_over_footer_links():
    primary = ObservedElement(
        tag="a",
        name="Overview",
        selector="#overview",
        href="/overview",
        source_url="https://example.test/",
        navigation_scope="primary",
    )
    footer = ObservedElement(
        tag="a",
        name="Legal",
        selector="#legal",
        href="/legal",
        source_url="https://example.test/",
        navigation_scope="footer",
    )
    context = ProductContext(
        url="https://example.test/",
        title="Example",
        application_type="dashboard",
        visible_text="Example dashboard",
        elements=[
            primary,
            footer,
            ObservedElement(
                tag="h1", name="Dashboard", selector="h1", source_url="https://example.test/"
            ),
            ObservedElement(
                tag="h1", name="Overview", selector="h1", source_url="https://example.test/overview"
            ),
        ],
        navigation=[primary, footer],
        page_knowledge=[
            PageKnowledge(
                url="https://example.test/",
                title="Example",
                purpose="dashboard",
                visible_sections=["Dashboard"],
                visible_facts=["Dashboard :: An overview of the product workspace."],
                fingerprint="home",
            ),
            PageKnowledge(
                url="https://example.test/overview",
                title="Overview",
                purpose="overview",
                visible_sections=["Overview"],
                visible_facts=["Overview :: The primary workspace summary."],
                fingerprint="overview",
            ),
        ],
        confidence=1,
    )

    plan = await ProductionPlanningService(Planner()).plan(
        objective="Create a full walkthrough", context=context
    )

    names = [step.operation.target.name for step in plan.workflow_steps if step.operation.target]
    assert "Overview" in names
    assert "Legal" not in names


@pytest.mark.asyncio
async def test_editorial_model_cannot_shift_grounded_copy_between_scene_ids():
    """Array order from a structured model must not corrupt scene ownership."""
    root = "https://example.test/"
    first = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Inspect first",
        target=Target(name="First", source_url=root),
    )
    second = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Inspect second",
        target=Target(name="Second", source_url=root),
    )
    context = ProductContext(
        url=root,
        title="Example",
        application_type="portfolio",
        visible_text="Example",
        confidence=1,
        page_knowledge=[
            PageKnowledge(
                url=root,
                title="Example",
                purpose="portfolio",
                fingerprint="one",
                visible_facts=[
                    "First :: A scheduling workspace that coordinates team availability.",
                    "Second :: A monitoring workspace that explains service health.",
                ],
            )
        ],
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        expected_outcomes=["first", "second"],
        viewport_strategy="native",
        stop_conditions=["done"],
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one", intent=first.intent, operation=first
            ),
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="two", intent=second.intent, operation=second
            ),
        ],
    )
    board = build_editorial_storyboard(context, plan)
    opening, first_scene, second_scene = board.scenes

    class ReassigningWriter:
        async def structured(self, _prompt, _schema):
            # Both prose lines are grounded at their array positions, but the
            # model falsely assigns the other scene's immutable identifiers.
            return board.model_copy(
                update={
                    "scenes": [
                        opening,
                        first_scene.model_copy(
                            update={
                                "id": second_scene.id,
                                "operation_id": second_scene.operation_id,
                            }
                        ),
                        second_scene.model_copy(
                            update={"id": first_scene.id, "operation_id": first_scene.operation_id}
                        ),
                    ]
                }
            )

    enriched = await enrich_editorial_storyboard(context, board, ReassigningWriter())
    # Cross-id assignment is rejected; evidence drafts are retained.
    assert enriched.scenes[1].narration == first_scene.narration
    assert enriched.scenes[2].narration == second_scene.narration


@pytest.mark.asyncio
async def test_complete_walkthrough_explores_h4_card_content_after_opening_a_primary_page():
    root = "https://example.test/"
    systems = "https://example.test/systems"
    tab = ObservedElement(
        tag="a",
        name="Systems",
        selector="a",
        href="/systems",
        source_url=root,
        navigation_scope="primary",
    )
    elements = [
        tab,
        ObservedElement(tag="h1", name="System Architecture", selector="h1", source_url=systems),
        ObservedElement(
            tag="h4", name="Event Ingestion Pipeline", selector="h4", source_url=systems
        ),
        ObservedElement(tag="h4", name="Realtime Collaboration", selector="h4", source_url=systems),
        ObservedElement(tag="h4", name="Failure Recovery", selector="h4", source_url=systems),
    ]
    context = ProductContext(
        url=root,
        title="Example",
        application_type="portfolio",
        visible_text="Example",
        elements=elements,
        navigation=[tab],
        confidence=1,
    )

    plan = await ProductionPlanningService(Planner()).plan(
        objective="Create a full walkthrough", context=context
    )

    names = [step.operation.target.name for step in plan.workflow_steps if step.operation.target]
    assert "Event Ingestion Pipeline" in names
    assert "Realtime Collaboration" in names


@pytest.mark.asyncio
async def test_grounding_keeps_page_local_heading_when_global_navigation_repeats_its_name():
    root = "https://example.test/"
    notes = "https://example.test/notes"
    nav = ObservedElement(
        tag="a",
        name="Notes",
        selector="a",
        href="/notes",
        source_url=root,
        navigation_scope="primary",
    )
    local_heading = ObservedElement(tag="h1", name="Notes", selector="h1", source_url=notes)
    context = ProductContext(
        url=root,
        title="Example",
        application_type="portfolio",
        visible_text="Example",
        elements=[nav, local_heading],
        navigation=[nav],
        confidence=1,
    )
    plan = await ProductionPlanningService(Planner()).plan(
        objective="Create a full walkthrough", context=context
    )
    # The compact heading is already visible: inspect it instead of
    # manufacturing a zero-distance scroll merely to claim coverage.
    local_step = next(
        step.operation
        for step in plan.workflow_steps
        if step.operation.kind is OperationKind.VERIFY_STATE
    )
    assert local_step.target.source_url == notes


def test_editorial_storyboard_uses_observed_content_not_route_labels():
    context = ProductContext(
        url="https://study.test/",
        title="Study Plan",
        application_type="planner",
        visible_text="Today shows the current study plan and next lesson.",
        elements=[
            ObservedElement(
                tag="a",
                name="Today",
                selector="a",
                text="Today shows the current study plan and next lesson.",
            )
        ],
        confidence=1,
    )
    proposal = WorkflowProposal(
        narrative_goal="demo",
        selected_workflow="demo",
        steps=[
            SemanticOperation(
                kind=OperationKind.SCROLL_TO,
                intent="Explain Today",
                target=Target(name="Today", text="Today"),
            )
        ],
        expected_outcomes=["Today"],
    )
    plan = __import__("app.contracts.models", fromlist=["DemoPlan"]).DemoPlan(
        objective="Full walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=90,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one", intent="Explain Today", operation=proposal.steps[0]
            )
        ],
        expected_outcomes=["Today"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    board = build_editorial_storyboard(context, plan)
    assert "current study plan" in board.scenes[1].narration.lower()
    assert board.minimum_duration_seconds >= 45


def test_editorial_storyboard_preserves_the_approved_feature_duration_floor():
    context = ProductContext(
        url="https://example.test/",
        title="Example",
        application_type="dashboard",
        visible_text="The requested workspace is ready.",
        confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.VERIFY_STATE,
        intent="Establish the workspace",
        target=Target(name="Workspace"),
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="Show the workspace",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=120,
        minimum_duration_seconds=60,
        maximum_duration_seconds=180,
        selected_workflow="workspace",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one",
                intent=operation.intent,
                operation=operation,
            )
        ],
        expected_outcomes=["Workspace"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )

    assert build_editorial_storyboard(context, plan).minimum_duration_seconds == 60


def test_editorial_storyboard_extracts_a_readable_opening_fact_not_a_dom_dump():
    context = ProductContext(
        url="https://example.test/",
        title="Example",
        application_type="portfolio",
        visible_text="HOME WORK PROJECTS CONTACT I engineer resilient software systems for product teams.",
        page_knowledge=[
            PageKnowledge(
                url="https://example.test/",
                title="Example",
                purpose="Example",
                visible_facts=[
                    "Identity :: HOME WORK PROJECTS CONTACT I engineer resilient software systems for product teams."
                ],
                fingerprint="opening",
            )
        ],
        confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Explore identity",
        target=Target(name="Identity", source_url=context.url),
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one", intent=operation.intent, operation=operation
            )
        ],
        expected_outcomes=["identity"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    board = build_editorial_storyboard(context, plan)
    assert "Welcome to Example" in board.scenes[0].narration
    # Draft opening is fact-or-skeleton; LLM enrich owns polished welcome copy.
    assert (
        "I engineer resilient software systems" in board.scenes[0].narration
        or "control in focus" in board.scenes[0].narration.lower()
    )
    assert "walkthrough pauses" not in board.scenes[1].narration.lower()
    assert "next view" not in board.scenes[0].narration.lower()


def test_opening_keeps_objective_and_exploration_context_when_the_workspace_is_sparse():
    leads = "https://example.test/leads"
    configuration = "https://example.test/settings/lead-configuration"
    context = ProductContext(
        url=leads,
        title="Example CRM",
        application_type="crm",
        confidence=1,
        objective=ObjectiveSpec(
            raw="Show Lead Management in the context of Lead Configuration",
            primary_entity="Lead Management",
            supporting_relationships=[
                {
                    "source": "Lead Configuration",
                    "target": "Lead Management",
                }
            ],
        ),
        page_knowledge=[
            PageKnowledge(url=leads, title="Leads", purpose="Leads", fingerprint="leads"),
            PageKnowledge(
                url=configuration,
                title="Lead Configuration",
                purpose="Lead Configuration",
                fingerprint="config",
            ),
        ],
    )
    operation = SemanticOperation(
        kind=OperationKind.NAVIGATE,
        intent="Establish leads",
        value=leads,
        postconditions=[
            __import__("app.contracts.models", fromlist=["Postcondition"]).Postcondition(
                kind="url", expected=leads
            )
        ],
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective=context.objective.raw,
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="leads",
        expected_outcomes=["lead workflow"],
        viewport_strategy="native",
        stop_conditions=["done"],
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="leads", intent=operation.intent, operation=operation
            )
        ],
    )
    opening = build_editorial_storyboard(context, plan).scenes[0].narration
    assert opening.startswith("Welcome to Example CRM")
    # Sparse workspace drafts stay thin; exploration bridges come from enrich.
    assert "lead" in opening.casefold()
    assert "control in focus" in opening.casefold() or "lead management" in opening.casefold()
    assert "next view" not in opening.casefold()
    assert "next part of the walkthrough" not in opening.casefold()


def test_editorial_preflight_rejects_generic_navigation_before_execution():
    root = "https://example.test/"
    operation = SemanticOperation(
        kind=OperationKind.OPEN_NAVIGATION_ITEM,
        intent="Open records",
        target=Target(name="Records", source_url=root),
    )
    context = ProductContext(url=root, title="Example", application_type="dashboard", confidence=1)
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="Show records",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="records",
        expected_outcomes=["records"],
        viewport_strategy="native",
        stop_conditions=["done"],
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="records", intent=operation.intent, operation=operation
            )
        ],
    )
    storyboard = EditorialStoryboard(
        brief=EditorialBrief(
            title="Example walkthrough", product_purpose="Example", opening_message="Welcome"
        ),
        minimum_duration_seconds=45,
        scenes=[
            EditorialScene(
                id="opening",
                title="Opening",
                purpose="Establish",
                narration="Welcome to Example.",
                evidence=[f"page:{root}"],
                interaction="opening",
                required_dwell_seconds=5,
                completion_criteria=["stable"],
            ),
            EditorialScene(
                id="records",
                operation_id=operation.id,
                title="Records",
                purpose="Open records",
                narration="The Records workspace opens with various filters and details, ready for management and action.",
                evidence=[f"page:{root}", "element:Records"],
                interaction="navigate",
                required_dwell_seconds=4,
                completion_criteria=["visible"],
            ),
        ],
    )
    report = inspect_editorial_preflight(context=context, plan=plan, storyboard=storyboard)
    assert "GENERIC_ROUTE_LABEL_CAPTION" in report["hard_failures"]


def test_editorial_preflight_does_not_treat_other_page_link_as_direct_route_failure():
    """Visible navigation alternatives must be page-local, not global."""
    root = "https://example.test/start"
    destination = "https://example.test/target"
    operation = SemanticOperation(
        kind=OperationKind.NAVIGATE,
        intent="Open the target workspace",
        value=destination,
        postconditions=[Postcondition(kind="url", expected=destination)],
    )
    context = ProductContext(
        url=root,
        title="Example",
        application_type="dashboard",
        confidence=1,
        navigation=[
            ObservedElement(
                tag="a",
                name="Target",
                selector="a-target",
                href="/target",
                source_url="https://example.test/other",
                actionable=True,
            )
        ],
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="Show the target workspace",
        narrative_goal="explain",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="target",
        expected_outcomes=["target"],
        viewport_strategy="native",
        stop_conditions=["done"],
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="target", intent=operation.intent, operation=operation
            )
        ],
    )
    storyboard = EditorialStoryboard(
        brief=EditorialBrief(
            title="Example walkthrough", product_purpose="Example", opening_message="Welcome"
        ),
        minimum_duration_seconds=45,
        scenes=[
            EditorialScene(
                id="opening",
                title="Opening",
                purpose="Establish",
                narration="Welcome to Example.",
                evidence=[f"page:{root}"],
                interaction="opening",
                required_dwell_seconds=5,
                completion_criteria=["stable"],
            )
        ],
    )

    report = inspect_editorial_preflight(context=context, plan=plan, storyboard=storyboard)

    assert "DIRECT_ROUTE_USED_WHERE_VISIBLE_NAVIGATION_EXISTS" not in report["hard_failures"]


def test_editorial_preflight_rejects_contact_or_record_data_in_scene_copy():
    root = "https://example.test/"
    context = ProductContext(url=root, title="Example", application_type="dashboard", confidence=1)
    operation = SemanticOperation(
        kind=OperationKind.READ_VALUE, intent="Read record context", target=Target(name="Records")
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="Show records",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="records",
        expected_outcomes=["records"],
        viewport_strategy="native",
        stop_conditions=["done"],
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="records",
                intent=operation.intent,
                operation=operation,
            )
        ],
    )
    storyboard = EditorialStoryboard(
        brief=EditorialBrief(title="Example", product_purpose="Example", opening_message="Welcome"),
        minimum_duration_seconds=45,
        scenes=[
            EditorialScene(
                id="opening",
                title="Opening",
                purpose="Establish",
                narration="Welcome to Example. The created record is 985 223 9496.",
                evidence=[f"page:{root}"],
                interaction="opening",
                required_dwell_seconds=5,
                completion_criteria=["stable"],
            )
        ],
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
        url=root,
        title="Example portfolio",
        application_type="portfolio",
        confidence=1,
        page_knowledge=[
            PageKnowledge(
                url=root,
                title="Professional History",
                purpose="professional history",
                visible_facts=[
                    "DATABASES, TOOLS & CLOUD :: DATABASES, TOOLS & CLOUD MongoDB PostgreSQL Redis AWS Prisma Sequelize",
                ],
                fingerprint="resume",
            )
        ],
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="Review the database stack",
        narrative_goal="explain",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="resume",
        expected_outcomes=["stack"],
        viewport_strategy="native",
        stop_conditions=["done"],
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="stack",
                intent=operation.intent,
                operation=operation,
            )
        ],
    )
    storyboard = EditorialStoryboard(
        brief=EditorialBrief(
            title="Example walkthrough", product_purpose="Example", opening_message="Welcome"
        ),
        minimum_duration_seconds=45,
        scenes=[
            EditorialScene(
                id="opening",
                title="Opening",
                purpose="Establish",
                narration="Welcome to Example.",
                evidence=[f"page:{root}"],
                interaction="opening",
                required_dwell_seconds=5,
                completion_criteria=["stable"],
            ),
            EditorialScene(
                id="stack",
                operation_id=operation.id,
                title="DATABASES, TOOLS & CLOUD",
                purpose="Explain the visible stack",
                narration=(
                    "The DATABASES, TOOLS & CLOUD view groups the visible tools into a structured collection for comparison."
                ),
                evidence=[f"page:{root}", "fact:placeholder"],
                interaction="scroll",
                required_dwell_seconds=3,
                completion_criteria=["visible"],
                page_url=root,
            ),
        ],
    )
    # Use the real evidence id so the preflight source lookup is page-local.
    from app.presentation.editorial import _fact_id

    storyboard.scenes[1].evidence[1] = _fact_id(root, context.page_knowledge[0].visible_facts[0])
    report = inspect_editorial_preflight(context=context, plan=plan, storyboard=storyboard)
    assert "SCROLL_SCENE_LACKS_READABLE_LOCAL_EVIDENCE" not in report["hard_failures"]


def test_editorial_brief_title_is_short_even_when_the_browser_title_is_descriptive():
    context = ProductContext(
        url="https://example.test/",
        title="Example Platform | Reliable Infrastructure For Distributed Product Teams",
        application_type="dashboard",
        visible_text="Example",
        confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO, intent="Explore overview", target=Target(name="Overview")
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one", intent=operation.intent, operation=operation
            )
        ],
        expected_outcomes=["overview"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
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
    from app.presentation.editorial import _human_sentence

    assert _human_sentence("The page opens with rationale,, where the visible design is clear") == (
        "The page opens with rationale, where the visible design is clear."
    )


def test_readable_fact_rejects_label_collections_as_caption_prose():
    fact = "Skills :: LANGUAGES & RUNTIMES JavaScript TypeScript FRONTEND ARCHITECTURE React Next.js BACKEND SERVICES Node.js"
    assert _readable_fact(fact) == ""


def test_readable_fact_keeps_short_observable_status_statement():
    assert (
        _readable_fact(
            "Progress :: Progress Saved in SQLite. Auto-calculated from days, builds, and DSA."
        )
        == "Saved in SQLite."
    )


def test_destination_intro_reframes_highlighted_task_as_presenter_copy():
    page = PageKnowledge(
        url="https://example.test/weeks/06",
        title="Week 6",
        purpose="Week 6",
        visible_facts=[],
        fingerprint="week-6",
    )
    from app.presentation.editorial import _page_intro_from_fact

    narration = _page_intro_from_fact(
        page,
        "REACT Auth provider Build an auth context holding user + access token in memory with login/logout APIs.",
    )
    lowered = narration.casefold()
    assert "week 6" in lowered
    assert (
        "control in focus" in lowered
        or "auth" in lowered
        or "login" in lowered
        or "token" in lowered
    )
    assert "opens with react" not in lowered
    assert "next view" not in lowered
    assert "activates" not in lowered


def test_destination_intro_does_not_narrate_storage_implementation():
    page = PageKnowledge(
        url="https://example.test/progress",
        title="Progress",
        purpose="Progress",
        visible_facts=[],
        fingerprint="progress",
    )
    from app.presentation.editorial import _page_intro_from_fact

    narration = _page_intro_from_fact(page, "Saved in SQLite (`data/progress.db`).")
    assert "SQLite" not in narration
    assert "completion state" not in narration
    assert "control in focus" in narration.casefold() or "progress" in narration.casefold()
    assert "next view" not in narration.casefold()
    assert "activates" not in narration.casefold()


def test_destination_intro_explains_numbered_item_instead_of_title_only():
    page = PageKnowledge(
        url="https://example.test/problems",
        title="Your problem list",
        purpose="Your problem list",
        visible_facts=[],
        fingerprint="problems",
    )
    from app.presentation.editorial import _page_intro_from_fact

    narration = _page_intro_from_fact(page, "#141 Linked List Cycle.")
    lowered = narration.casefold()
    assert "representative practice item" not in lowered
    assert "highlights #141" not in lowered
    assert "problem list" in lowered
    assert "control in focus" in lowered
    assert "next view" not in lowered
    assert "activates" not in lowered


def test_repeated_schedule_is_summarised_instead_of_read_verbatim():
    context = ProductContext(
        url="https://example.test/",
        title="Planner",
        application_type="planner",
        visible_text="Planner",
        page_knowledge=[
            PageKnowledge(
                url="https://example.test/",
                title="Planner",
                purpose="Planner",
                visible_facts=["Weeks :: Weeks 1 Week 1 28% 2 Week 2 5% 3 Week 3 0% 4 Week 4 0%"],
                fingerprint="planner",
            )
        ],
        confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Explore Weeks",
        target=Target(name="Weeks", source_url=context.url),
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one", intent=operation.intent, operation=operation
            )
        ],
        expected_outcomes=["Weeks"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    narration = build_editorial_storyboard(context, plan).scenes[1].narration
    assert "organizes" not in narration
    assert "Week 1 28%" not in narration
    assert "control in focus" in narration.casefold()
    assert "weeks" in narration.casefold()
    assert "next view" not in narration.casefold()
    assert "activates" not in narration.casefold()
    assert "next part of the walkthrough" not in narration.casefold()


def test_short_observed_project_fact_is_viewer_copy_instead_of_a_route_label():
    context = ProductContext(
        url="https://example.test/",
        title="Example",
        application_type="portfolio",
        visible_text="Work",
        page_knowledge=[
            PageKnowledge(
                url="https://example.test/",
                title="Example",
                purpose="work",
                visible_facts=["Atlas :: A routing engine and user dashboard."],
                fingerprint="work",
            )
        ],
        confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Inspect Atlas",
        target=Target(name="Atlas", source_url=context.url),
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="atlas", intent=operation.intent, operation=operation
            )
        ],
        expected_outcomes=["atlas"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    narration = build_editorial_storyboard(context, plan).scenes[1].narration
    assert narration == "Atlas is a routing engine and user dashboard."
    assert "section visibly presents" not in narration.lower()


def test_scroll_scene_recovers_page_local_content_fact_when_heading_differs():
    """A landmark heading must not collapse into a generic scroll caption."""
    root = "https://example.test/"
    page = PageKnowledge(
        url=root,
        title="Portfolio",
        purpose="featured work",
        visible_sections=["Selected work"],
        visible_facts=[
            "Selected work :: Atlas connects scheduling, availability, and team handoffs in one workflow."
        ],
        fingerprint="portfolio",
    )
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Explain the selected work cards",
        target=Target(name="Selected work cards", source_url=root),
        covered_content_groups=["Selected work cards"],
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="Full walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="portfolio",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="work",
                intent=operation.intent,
                operation=operation,
            )
        ],
        expected_outcomes=["selected work"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    context = ProductContext(
        url=root,
        title="Portfolio",
        application_type="portfolio",
        visible_text="Portfolio",
        page_knowledge=[page],
        confidence=1,
    )
    board = build_editorial_storyboard(context, plan)
    scene = board.scenes[1]
    assert "scheduling" in scene.narration.lower()
    assert any(ref.startswith("fact:") for ref in scene.evidence)


def test_category_landmark_prefers_descriptive_fact_over_structural_fallback():
    """Dense pages must narrate a landmark's description when one is captured."""
    root = "https://example.test/systems"
    page = PageKnowledge(
        url=root,
        title="System Architecture",
        purpose="system architecture",
        visible_sections=["The Challenge & Bottlenecks"],
        visible_facts=[
            "The Challenge & Bottlenecks :: Ingesting call audio files directly in single request cycles caused server timeouts and out-of-memory errors when processing multi-hour files during high traffic peaks.",
        ],
        fingerprint="systems",
    )
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Explain the challenge and its solution",
        target=Target(name="The Challenge & Bottlenecks", source_url=root),
        covered_content_groups=["The Challenge & Bottlenecks"],
    )
    step = __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
        id="challenge",
        intent=operation.intent,
        operation=operation,
    )
    plan = __import__("app.contracts.models", fromlist=["DemoPlan"]).DemoPlan(
        objective="Full walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=90,
        selected_workflow="systems",
        workflow_steps=[step],
        expected_outcomes=["challenge"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    context = ProductContext(
        url=root,
        title="System Architecture",
        application_type="portfolio",
        visible_text="System Architecture The Challenge & Bottlenecks",
        page_knowledge=[page],
        confidence=1,
    )
    narration = build_editorial_storyboard(context, plan).scenes[1].narration
    assert "timeouts" in narration.lower()
    assert "traffic peaks" in narration.lower()
    assert "group brings the visible tools together" not in narration.lower()


def test_editorial_fallback_uses_destination_page_facts_for_navigation():
    context = ProductContext(
        url="https://portfolio.test/",
        title="Portfolio",
        application_type="portfolio",
        visible_text="A systems portfolio.",
        elements=[
            ObservedElement(
                tag="a",
                name="Timeline",
                selector="a",
                href="/timeline",
                source_url="https://portfolio.test/",
            )
        ],
        page_knowledge=[
            PageKnowledge(
                url="https://portfolio.test/timeline",
                title="Timeline",
                purpose="career progression",
                visible_sections=["Experience"],
                visible_facts=[
                    "Senior Full Stack Developer at Smartsevak, contributing to reliable product systems."
                ],
                fingerprint="timeline",
            )
        ],
        confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.OPEN_NAVIGATION_ITEM,
        intent="Open Timeline",
        target=Target(name="Timeline", text="Timeline"),
        postconditions=[
            __import__("app.contracts.models", fromlist=["Postcondition"]).Postcondition(
                kind="url", expected="https://portfolio.test/timeline"
            )
        ],
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="Full walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=90,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="timeline", intent="Open Timeline", operation=operation
            )
        ],
        expected_outcomes=["Timeline"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    board = build_editorial_storyboard(context, plan)
    narration = board.scenes[1].narration.lower()
    assert "timeline" in narration
    assert (
        "smartsevak" in narration
        or "control in focus" in narration
        or "full stack" in narration
    )
    assert "next view" not in narration
    assert "activates" not in narration
    assert "next part of the walkthrough" not in narration


def test_editorial_navigation_prefers_destination_over_source_page_provenance():
    context = ProductContext(
        url="https://example.test/",
        title="Home",
        application_type="portfolio",
        visible_text="Home introduction.",
        page_knowledge=[
            PageKnowledge(
                url="https://example.test/",
                title="Home",
                purpose="home",
                visible_facts=["Home :: A concise introduction to the product."],
                fingerprint="home",
            ),
            PageKnowledge(
                url="https://example.test/work",
                title="Work",
                purpose="work",
                visible_facts=[
                    "Work :: A project collection that explains how the team delivers reliable releases."
                ],
                fingerprint="work",
            ),
        ],
        confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.OPEN_NAVIGATION_ITEM,
        intent="Open work",
        target=Target(name="Work", source_url=context.url),
        postconditions=[
            __import__("app.contracts.models", fromlist=["Postcondition"]).Postcondition(
                kind="url", expected="https://example.test/work"
            )
        ],
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="work", intent=operation.intent, operation=operation
            )
        ],
        expected_outcomes=["work"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    assert "reliable releases" in build_editorial_storyboard(context, plan).scenes[1].narration


def test_editorial_scene_keeps_a_repeated_heading_on_its_own_page():
    context = ProductContext(
        url="https://example.test/",
        title="Example",
        application_type="portfolio",
        visible_text="Work",
        page_knowledge=[
            PageKnowledge(
                url="https://example.test/first",
                title="First",
                purpose="first work",
                visible_sections=["Project"],
                visible_facts=[
                    "Project :: A scheduling tool that coordinates availability across teams."
                ],
                fingerprint="first",
            ),
            PageKnowledge(
                url="https://example.test/second",
                title="Second",
                purpose="second work",
                visible_sections=["Project"],
                visible_facts=[
                    "Project :: A monitoring workspace that explains service health and incidents."
                ],
                fingerprint="second",
            ),
        ],
        confidence=1,
    )
    first = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Inspect first project",
        target=Target(name="Project", source_url="https://example.test/first"),
    )
    second = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Inspect second project",
        target=Target(name="Project", source_url="https://example.test/second"),
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one", intent=first.intent, operation=first
            ),
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="two", intent=second.intent, operation=second
            ),
        ],
        expected_outcomes=["projects"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )

    board = build_editorial_storyboard(context, plan)

    assert "scheduling tool" in board.scenes[1].narration
    assert "monitoring workspace" in board.scenes[2].narration
    assert board.scenes[1].evidence != board.scenes[2].evidence


def test_editorial_qa_rejects_short_scene_and_generic_caption():
    now = datetime.now(UTC)
    operation = SemanticOperation(
        kind=OperationKind.CLICK,
        intent="Open progress",
        target=Target(name="Progress", text="Progress"),
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one", intent="Open progress", operation=operation
            )
        ],
        expected_outcomes=["Progress"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    context = ProductContext(
        url="https://study.test/",
        title="Study",
        application_type="planner",
        visible_text="Today",
        elements=[
            ObservedElement(
                tag="a", name="Progress", selector="a", text="Review learning momentum."
            )
        ],
        navigation=[ObservedElement(tag="a", name="Progress", selector="a", href="/progress")],
        confidence=1,
    )
    board = build_editorial_storyboard(context, plan)
    event = InteractionEvent(
        operation_id=operation.id,
        kind=operation.kind,
        intent=operation.intent,
        occurred_at=now + timedelta(seconds=1),
        success=True,
        duration_ms=500,
    )
    trace = DemoTrace(
        run_id="run",
        objective="walkthrough",
        started_at=now,
        recording_started_at=now,
        events=[event],
        outcome_verified=True,
    )
    report = inspect_editorial(
        context=context,
        plan=plan,
        trace=trace,
        storyboard=board,
        script=editorial_script(board, {operation.id: event.id}),
    )
    assert "OPENING_PAGE_ADVANCED_TOO_EARLY" in report["hard_failures"]
    assert "SCENE_ADVANCED_BEFORE_REQUIRED_DWELL" in report["hard_failures"]


def test_editorial_qa_rejects_mechanical_context_boilerplate():
    now = datetime.now(UTC)
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Explore activity",
        target=Target(name="Activity", text="Activity"),
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one", intent="Explore activity", operation=operation
            )
        ],
        expected_outcomes=["Activity"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    context = ProductContext(
        url="https://example.test/",
        title="Example",
        application_type="dashboard",
        visible_text="Activity",
        elements=[
            ObservedElement(
                tag="h2",
                name="Activity",
                selector="#activity",
                text="Activity is displayed in a chronological list.",
            )
        ],
        confidence=1,
    )
    board = build_editorial_storyboard(context, plan)
    event = InteractionEvent(
        operation_id=operation.id,
        kind=operation.kind,
        intent=operation.intent,
        occurred_at=now + timedelta(seconds=8),
        success=True,
        duration_ms=6_000,
    )
    trace = DemoTrace(
        run_id="run",
        objective="walkthrough",
        started_at=now,
        recording_started_at=now,
        events=[event],
        outcome_verified=True,
    )
    report = inspect_editorial(
        context=context,
        plan=plan,
        trace=trace,
        storyboard=board,
        script=[
            {
                "event_id": event.id,
                "text": "Activity is explored in context: Activity. This gives the viewer concrete evidence for the next part of the walkthrough.",
            }
        ],
    )
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
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one", intent=operation.intent, operation=operation
            )
        ],
        expected_outcomes=["Bookings"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    context = ProductContext(
        url="https://example.test/",
        title="Example",
        application_type="dashboard",
        visible_text="Bookings appointments can be filtered by status and date.",
        elements=[
            ObservedElement(tag="h1", name="Bookings", selector="#bookings", text="Bookings")
        ],
        confidence=1,
        page_knowledge=[
            PageKnowledge(
                url="https://example.test/bookings",
                title="Bookings",
                purpose="Appointments",
                fingerprint="bookings-v1",
                visible_facts=["Bookings appointments can be filtered by status and date."],
            )
        ],
    )
    board = build_editorial_storyboard(context, plan)
    event = InteractionEvent(
        operation_id=operation.id,
        kind=operation.kind,
        intent=operation.intent,
        occurred_at=now + timedelta(seconds=8),
        success=True,
        duration_ms=6_000,
    )
    trace = DemoTrace(
        run_id="run",
        objective="walkthrough",
        started_at=now,
        recording_started_at=now,
        events=[event],
        outcome_verified=True,
    )
    report = inspect_editorial(
        context=context,
        plan=plan,
        trace=trace,
        storyboard=board,
        script=[
            {
                "event_id": event.id,
                "text": "This workspace establishes the current working view before we demonstrate the visible workflow.",
            }
        ],
    )
    assert "GENERIC_ROUTE_LABEL_CAPTION" in report["hard_failures"]


def test_editorial_qa_rejects_a_scroll_trace_with_no_actual_motion():
    now = datetime.now(UTC)
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Explore activity",
        target=Target(name="Activity", text="Activity"),
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one", intent=operation.intent, operation=operation
            )
        ],
        expected_outcomes=["activity"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    context = ProductContext(
        url="https://example.test/",
        title="Example",
        application_type="dashboard",
        visible_text="Activity",
        elements=[
            ObservedElement(
                tag="h2",
                name="Activity",
                selector="#activity",
                text="Activity is displayed in a chronological list.",
            )
        ],
        confidence=1,
    )
    board = build_editorial_storyboard(context, plan)
    event = InteractionEvent(
        operation_id=operation.id,
        kind=operation.kind,
        intent=operation.intent,
        occurred_at=now + timedelta(seconds=8),
        success=True,
        duration_ms=6_000,
        after={"scroll_motion": {"start_y": 0, "target_y": 0, "path": []}},
    )
    trace = DemoTrace(
        run_id="run",
        objective="walkthrough",
        started_at=now,
        recording_started_at=now,
        events=[event],
        outcome_verified=True,
    )
    report = inspect_editorial(
        context=context,
        plan=plan,
        trace=trace,
        storyboard=board,
        script=editorial_script(board, {operation.id: event.id}),
    )
    assert "SCROLL_SCENE_HAS_NO_CONTINUOUS_MOTION" in report["hard_failures"]


def test_editorial_qa_rejects_raw_dom_caption_even_when_evidence_words_match():
    now = datetime.now(UTC)
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Inspect skills",
        target=Target(name="Skills Stack", text="Skills Stack"),
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one", intent=operation.intent, operation=operation
            )
        ],
        expected_outcomes=["skills"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    context = ProductContext(
        url="https://example.test/",
        title="Example",
        application_type="portfolio",
        visible_text="Skills",
        page_knowledge=[
            PageKnowledge(
                url="https://example.test/",
                title="Example",
                purpose="skills",
                visible_facts=[
                    "Skills Stack :: LANGUAGES JavaScript TypeScript FRONTEND React BACKEND Node"
                ],
                fingerprint="skills",
            )
        ],
        confidence=1,
    )
    board = build_editorial_storyboard(context, plan)
    event = InteractionEvent(
        operation_id=operation.id,
        kind=operation.kind,
        intent=operation.intent,
        occurred_at=now + timedelta(seconds=8),
        success=True,
        duration_ms=8_000,
    )
    trace = DemoTrace(
        run_id="run",
        objective="walkthrough",
        started_at=now,
        recording_started_at=now,
        events=[event],
        outcome_verified=True,
    )
    report = inspect_editorial(
        context=context,
        plan=plan,
        trace=trace,
        storyboard=board,
        script=[
            {
                "event_id": event.id,
                "text": "Skills Stack LANGUAGES JavaScript TypeScript FRONTEND React BACKEND Node.",
            }
        ],
    )
    assert "GENERIC_ROUTE_LABEL_CAPTION" in report["hard_failures"]


def test_editorial_qa_rejects_caption_borrowed_from_a_different_page():
    now = datetime.now(UTC)
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Inspect scheduling",
        target=Target(name="Scheduling", source_url="https://example.test/scheduling"),
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one", intent=operation.intent, operation=operation
            )
        ],
        expected_outcomes=["scheduling"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    context = ProductContext(
        url="https://example.test/",
        title="Example",
        application_type="dashboard",
        visible_text="Work",
        page_knowledge=[
            PageKnowledge(
                url="https://example.test/scheduling",
                title="Scheduling",
                purpose="scheduling",
                visible_facts=["Scheduling :: A calendar workspace that coordinates availability."],
                fingerprint="scheduling",
            )
        ],
        confidence=1,
    )
    board = build_editorial_storyboard(context, plan)
    event = InteractionEvent(
        operation_id=operation.id,
        kind=operation.kind,
        intent=operation.intent,
        occurred_at=now + timedelta(seconds=8),
        success=True,
        duration_ms=8_000,
    )
    trace = DemoTrace(
        run_id="run",
        objective="walkthrough",
        started_at=now,
        recording_started_at=now,
        events=[event],
        outcome_verified=True,
    )
    report = inspect_editorial(
        context=context,
        plan=plan,
        trace=trace,
        storyboard=board,
        script=[
            {
                "event_id": event.id,
                "text": "A monitoring workspace explains incidents and service health.",
            }
        ],
    )

    assert "UNSUPPORTED_OR_WRONG_SCENE_CAPTION" in report["hard_failures"]


def test_editorial_qa_rejects_trace_that_leaves_a_tab_without_local_exploration():
    now = datetime.now(UTC)
    nav = SemanticOperation(
        kind=OperationKind.OPEN_NAVIGATION_ITEM,
        intent="Open Timeline",
        target=Target(name="Timeline", text="Timeline"),
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="nav", intent="Open Timeline", operation=nav
            )
        ],
        expected_outcomes=["Timeline"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    context = ProductContext(
        url="https://study.test/",
        title="Study",
        application_type="planner",
        visible_text="Today",
        elements=[ObservedElement(tag="a", name="Timeline", selector="a", text="Career timeline.")],
        confidence=1,
    )
    board = build_editorial_storyboard(context, plan)
    event = InteractionEvent(
        operation_id=nav.id,
        kind=nav.kind,
        intent=nav.intent,
        occurred_at=now + timedelta(seconds=8),
        success=True,
        duration_ms=6_000,
    )
    trace = DemoTrace(
        run_id="run",
        objective="walkthrough",
        started_at=now,
        recording_started_at=now,
        events=[event],
        outcome_verified=True,
    )
    report = inspect_editorial(
        context=context,
        plan=plan,
        trace=trace,
        storyboard=board,
        script=editorial_script(board, {nav.id: event.id}),
    )
    assert "TRACE_NAVIGATED_PAGE_NOT_EXPLORED" in report["hard_failures"]


def test_editorial_qa_accepts_grounded_required_group_without_element_prefix():
    """Section/fact evidence plus a required group is a real page inspection."""
    now = datetime.now(UTC)
    nav = SemanticOperation(
        kind=OperationKind.OPEN_NAVIGATION_ITEM,
        intent="Open documentation",
        target=Target(name="Documentation", text="Documentation"),
        page_url="https://example.test/docs",
        story_phase="transition",
        evidence_refs=["page:https://example.test/docs", "section:Latest guidance"],
    )
    verify = SemanticOperation(
        kind=OperationKind.VERIFY_STATE,
        intent="Inspect the latest guidance",
        target=Target(name="Latest guidance", text="Latest guidance"),
        page_url="https://example.test/docs",
        story_phase="establish",
        page_contract_phases=["establish", "explore", "explain", "verify"],
        evidence_refs=[
            "page:https://example.test/docs",
            "section:Latest guidance",
            "fact:guidance",
        ],
        required_content_groups=["Latest guidance"],
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="nav", intent=nav.intent, operation=nav
            ),
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="verify", intent=verify.intent, operation=verify
            ),
        ],
        expected_outcomes=["Documentation"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    context = ProductContext(
        url="https://example.test/",
        title="Example",
        application_type="documentation",
        visible_text="Latest guidance explains the current release.",
        page_knowledge=[
            PageKnowledge(
                url="https://example.test/docs",
                title="Documentation",
                purpose="Reference",
                visible_facts=["Latest guidance explains the current release."],
                fingerprint="docs",
            )
        ],
        confidence=1,
    )
    board = build_editorial_storyboard(context, plan)
    events = [
        InteractionEvent(
            operation_id=nav.id,
            kind=nav.kind,
            intent=nav.intent,
            occurred_at=now + timedelta(seconds=8),
            success=True,
            duration_ms=5_000,
        ),
        InteractionEvent(
            operation_id=verify.id,
            kind=verify.kind,
            intent=verify.intent,
            occurred_at=now + timedelta(seconds=14),
            success=True,
            duration_ms=6_000,
            page_contract_phases=list(verify.page_contract_phases),
            required_content_groups=list(verify.required_content_groups),
        ),
    ]
    trace = DemoTrace(
        run_id="run",
        objective="walkthrough",
        started_at=now,
        recording_started_at=now,
        events=events,
        outcome_verified=True,
    )
    report = inspect_editorial(
        context=context,
        plan=plan,
        trace=trace,
        storyboard=board,
        script=editorial_script(board, {e.operation_id: e.id for e in events}),
    )
    assert "TRACE_NAVIGATED_PAGE_NOT_EXPLORED" not in report["hard_failures"]


@pytest.mark.asyncio
async def test_editorial_writer_can_change_only_grounded_prose_not_scene_contract():
    context = ProductContext(
        url="https://example.test/",
        title="Example",
        application_type="dashboard",
        visible_text="A dashboard that presents account activity.",
        elements=[
            ObservedElement(
                tag="h2",
                name="Activity",
                selector="#activity",
                text="Account activity is displayed in a chronological list.",
            )
        ],
        confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Explore activity",
        target=Target(name="Activity", text="Activity"),
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one", intent="Explore activity", operation=operation
            )
        ],
        expected_outcomes=["activity"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    board = build_editorial_storyboard(context, plan)

    class Writer:
        async def structured(self, prompt, schema):
            assert schema is EditorialNarrationDraft
            return EditorialNarrationDraft(
                lines=[
                    EditorialNarrationLine(
                        id=scene.id,
                        narration="Account activity is displayed in a chronological list, making recent changes easy to review.",
                    )
                    for scene in board.scenes
                    if scene.operation_id is not None
                ]
            )

    enriched = await enrich_editorial_storyboard(context, board, Writer())
    assert enriched.scenes[1].narration.startswith("Account activity")
    assert enriched.scenes[1].required_dwell_seconds == board.scenes[1].required_dwell_seconds
    assert enriched.scenes[1].evidence == board.scenes[1].evidence


@pytest.mark.asyncio
async def test_editorial_writer_rejects_generic_claim_despite_small_word_overlap():
    context = ProductContext(
        url="https://example.test/",
        title="Example",
        application_type="dashboard",
        visible_text="Account activity is shown in a chronological list.",
        elements=[
            ObservedElement(
                tag="h2",
                name="Activity",
                selector="#activity",
                text="Account activity is shown in a chronological list.",
            )
        ],
        confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Explore activity",
        target=Target(name="Activity", text="Activity"),
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one", intent=operation.intent, operation=operation
            )
        ],
        expected_outcomes=["activity"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    board = build_editorial_storyboard(context, plan)

    class GenericWriter:
        async def structured(self, prompt, schema):
            return board.model_copy(
                update={
                    "scenes": [
                        scene.model_copy(
                            update={
                                "narration": "This bespoke command interface makes activity visible before moving on."
                            }
                        )
                        for scene in board.scenes
                    ]
                }
            )

    enriched = await enrich_editorial_storyboard(context, board, GenericWriter())
    assert all(
        "bespoke command interface" not in scene.narration.casefold()
        for scene in enriched.scenes
        if scene.operation_id is not None
    )


@pytest.mark.asyncio
async def test_editorial_writer_rejects_reading_dwell_boilerplate():
    """A timing explanation must not replace a scene's product takeaway."""
    context = ProductContext(
        url="https://example.test/",
        title="Example",
        application_type="dashboard",
        visible_text="Engineering notes explain queue retries and failure recovery.",
        elements=[
            ObservedElement(
                tag="h2",
                name="Engineering Notes",
                selector="#notes",
                text="Engineering notes explain queue retries and failure recovery.",
            )
        ],
        confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Explore Engineering Notes",
        target=Target(name="Engineering Notes", text="Engineering Notes"),
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one",
                intent=operation.intent,
                operation=operation,
            )
        ],
        expected_outcomes=["notes"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    board = build_editorial_storyboard(context, plan)

    class Writer:
        async def structured(self, _prompt, schema):
            assert schema is EditorialNarrationDraft
            return EditorialNarrationDraft(
                lines=[
                    EditorialNarrationLine(
                        id=scene.id,
                        narration="This Engineering Notes workspace keeps the section in view while observed details are read before the walkthrough continues.",
                    )
                    for scene in board.scenes
                    if scene.operation_id is not None
                ]
            )

    with pytest.raises(ProviderError, match="ENRICH_UNGROUNDED"):
        await enrich_editorial_storyboard(context, board, Writer())


@pytest.mark.asyncio
async def test_enrich_editorial_storyboard_requires_structured_provider():
    context = ProductContext(
        url="https://example.test/",
        title="Example",
        application_type="dashboard",
        visible_text="Account activity is shown in a chronological list.",
        confidence=1,
    )
    operation = SemanticOperation(
        kind=OperationKind.SCROLL_TO,
        intent="Explore activity",
        target=Target(name="Activity", text="Activity"),
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one", intent=operation.intent, operation=operation
            )
        ],
        expected_outcomes=["activity"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    board = build_editorial_storyboard(context, plan)

    class NoStructured:
        pass

    class StructuredNone:
        structured = None

    with pytest.raises(ProviderError, match="ENRICH_PROVIDER_ERROR"):
        await enrich_editorial_storyboard(context, board, NoStructured())
    with pytest.raises(ProviderError, match="ENRICH_PROVIDER_ERROR"):
        await enrich_editorial_storyboard(context, board, StructuredNone())


def test_reentry_narration_is_diversified_only_for_the_same_page():
    """A meaningful re-entry must not repeat an earlier page summary."""
    root = "https://example.test/"
    page = PageKnowledge(
        url=root,
        title="Leads",
        purpose="lead records",
        visible_facts=[
            "Leads :: You see all leads in this organization. Filters and status controls support review."
        ],
        fingerprint="leads",
    )
    first = SemanticOperation(
        kind=OperationKind.OPEN_NAVIGATION_ITEM,
        intent="Open leads",
        target=Target(name="Leads", source_url=root),
    )
    second = SemanticOperation(
        kind=OperationKind.OPEN_NAVIGATION_ITEM,
        intent="Return to leads for the next step",
        target=Target(name="Leads", source_url=root),
    )
    plan = __import__(
        "app.contracts.models", fromlist=["DemoPlan", "WorkflowStep"]
    ).DemoPlan(
        objective="walkthrough",
        narrative_goal="demo",
        audience="prospect",
        target_duration_seconds=60,
        selected_workflow="demo",
        workflow_steps=[
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="one", intent=first.intent, operation=first
            ),
            __import__("app.contracts.models", fromlist=["WorkflowStep"]).WorkflowStep(
                id="two", intent=second.intent, operation=second
            ),
        ],
        expected_outcomes=["leads"],
        viewport_strategy="native",
        stop_conditions=["done"],
    )
    context = ProductContext(
        url=root,
        title="Example",
        application_type="dashboard",
        visible_text="Example",
        page_knowledge=[page],
        confidence=1,
    )
    board = build_editorial_storyboard(context, plan)
    first = board.scenes[1].narration
    second = board.scenes[2].narration
    # Even deterministic fallback copy must be grounded in the observed page;
    # the old "control in focus" skeleton hid the product story and produced
    # generic captions when the editorial provider was unavailable.
    assert "leads" in first.casefold()
    assert "leads" in second.casefold()
    assert "return" not in second.casefold()
    assert "leads" in first.casefold() and "leads" in second.casefold()
    assert "next view" not in second.casefold()
    assert "activates" not in second.casefold()
    assert "next part of the walkthrough" not in second.casefold()
