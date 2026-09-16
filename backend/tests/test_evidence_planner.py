import pytest

from app.contracts.models import (
    ObjectiveSpec,
    ObservedElement,
    OperationKind,
    PageKnowledge,
    Postcondition,
    ProductContext,
    SemanticOperation,
    WorkflowProposal,
)
from app.planning.candidates import (
    _editorial_landmark_groups,
    _page_landmarks,
    _select_representative_groups,
    build_page_complete_proposal,
    candidate_flows_from_evidence,
    select_candidate_flow,
)
from app.planning.production import ProductionPlanningService


def test_complete_walkthrough_regrounds_route_to_visible_navigation_control():
    root = "https://example.test/"
    destination = "https://example.test/timeline"
    context = ProductContext(
        url=root,
        title="Example",
        application_type="web_application",
        navigation=[
            ObservedElement(
                tag="a",
                name="Timeline",
                selector="#timeline",
                href="/timeline",
                source_url=root,
                navigation_scope="primary",
                actionable=True,
            )
        ],
    )
    proposal = WorkflowProposal(
        selected_workflow="complete walkthrough",
        narrative_goal="Explain the product journey",
        expected_outcomes=["Timeline is visible"],
        steps=[
            SemanticOperation(
                kind=OperationKind.NAVIGATE,
                intent="Open timeline",
                value=destination,
                postconditions=[Postcondition(kind="url", expected=destination)],
            )
        ],
    )

    compiled = ProductionPlanningService._compile_navigation(proposal, context)

    assert compiled.steps[0].kind is OperationKind.OPEN_NAVIGATION_ITEM
    assert compiled.steps[0].target is not None
    assert compiled.steps[0].target.name == "Timeline"


def test_navigation_compilation_preserves_semantic_target_with_generic_selector():
    """A tag-only selector must not resolve to a footer/policy link.

    Discovery snapshots may use ``a``/``button`` as a selector when no stable
    attribute was available.  Page provenance and accessible name are the
    identity in that case; selector-only lookup previously picked the last
    element in the snapshot and injected an unrelated Cookie Policy route.
    """
    root = "https://example.test/"
    demo = "https://example.test/demo"
    context = ProductContext(
        url=root,
        title="Example",
        application_type="web_application",
        elements=[
            ObservedElement(
                tag="a",
                name="Privacy Policy",
                selector="a",
                href="/privacy",
                source_url=demo,
                navigation_scope="footer",
                actionable=True,
            ),
            ObservedElement(
                tag="a",
                name="Cookie Policy",
                selector="a",
                href="/cookies",
                source_url=demo,
                navigation_scope="unknown",
                actionable=True,
            ),
            ObservedElement(
                tag="a",
                name="Live demo",
                selector="a",
                href="/demo",
                source_url=root,
                navigation_scope="primary",
                actionable=True,
            ),
        ],
    )
    proposal = WorkflowProposal(
        selected_workflow="demo",
        narrative_goal="Show the demo",
        expected_outcomes=["Demo is visible"],
        steps=[
            SemanticOperation(
                kind=OperationKind.OPEN_NAVIGATION_ITEM,
                intent="Open the live demo",
                target={
                    "name": "Live demo",
                    "selector": "a",
                    "source_url": root,
                    "text": "Live demo",
                },
                postconditions=[Postcondition(kind="url", expected=demo)],
            )
        ],
    )

    compiled = ProductionPlanningService._compile_navigation(proposal, context)

    assert compiled.steps[0].target is not None
    assert compiled.steps[0].target.name == "Live demo"
    assert not any(
        step.value in {"https://example.test/cookies", "https://example.test/privacy"}
        for step in compiled.steps
        if step.kind is OperationKind.NAVIGATE
    )


def test_navigation_compilation_keeps_opening_navigate_for_executor_noop_strip():
    root = "https://example.test/"
    context = ProductContext(url=root, title="Example", application_type="web_application")
    proposal = WorkflowProposal(
        selected_workflow="opening",
        narrative_goal="Establish the product",
        expected_outcomes=["Opening page is visible"],
        steps=[
            SemanticOperation(
                kind=OperationKind.NAVIGATE,
                intent="Establish opening page once",
                value=root,
                postconditions=[Postcondition(kind="url", expected=root)],
            )
        ],
    )

    compiled = ProductionPlanningService._compile_navigation(proposal, context)

    assert compiled.steps[0].kind is OperationKind.NAVIGATE
    assert compiled.steps[0].value == root


def test_navigation_compilation_does_not_reuse_stale_header_after_route_change():
    root = "https://example.test/"
    first = "https://example.test/first"
    second = "https://example.test/second"
    context = ProductContext(
        url=root,
        title="Example",
        application_type="web_application",
        navigation=[
            ObservedElement(
                tag="a",
                name="First",
                selector="#first",
                href="/first",
                source_url=root,
                navigation_scope="primary",
                actionable=True,
            ),
            # This is intentionally observed only on the opening page. It is
            # not proof that the control survives the first route transition.
            ObservedElement(
                tag="a",
                name="Second",
                selector="#second",
                href="/second",
                source_url=root,
                navigation_scope="primary",
                actionable=True,
            ),
        ],
    )
    proposal = WorkflowProposal(
        selected_workflow="two pages",
        narrative_goal="Show both pages",
        expected_outcomes=["Second is visible"],
        steps=[
            SemanticOperation(
                kind=OperationKind.NAVIGATE,
                intent="Open the first page",
                value=first,
                postconditions=[Postcondition(kind="url", expected=first)],
            ),
            SemanticOperation(
                kind=OperationKind.NAVIGATE,
                intent="Open the second page",
                value=second,
                postconditions=[Postcondition(kind="url", expected=second)],
            ),
        ],
    )

    compiled = ProductionPlanningService._compile_navigation(proposal, context)

    assert [step.kind for step in compiled.steps] == [
        OperationKind.OPEN_NAVIGATION_ITEM,
        OperationKind.NAVIGATE,
    ]
    assert compiled.steps[1].value == second


def test_representative_groups_prioritize_semantic_roles_over_even_spacing():
    groups = [
        [ObservedElement(tag="h2", name="Professional History", selector="#history")],
        [
            ObservedElement(
                tag="h3",
                name="Senior Engineer",
                selector="#senior",
                text="Current role and contributions",
            )
        ],
        [
            ObservedElement(
                tag="h3",
                name="Software Development Intern",
                selector="#intern",
                text="Earlier internship contribution",
            )
        ],
        [ObservedElement(tag="h2", name="Technical Skills", selector="#skills")],
        [ObservedElement(tag="h2", name="Academic Foundation", selector="#academic")],
    ]

    selected = _select_representative_groups(groups, maximum=3)
    names = [item.name for group in selected for item in group]

    assert names == ["Professional History", "Senior Engineer", "Software Development Intern"]


def test_editorial_landmark_groups_preserve_every_heading_without_one_action_per_card():
    landmarks = [
        ObservedElement(tag="h1", name="Portfolio", selector="#portfolio"),
        ObservedElement(tag="h2", name="Featured work", selector="#work"),
        *[
            ObservedElement(tag="h3", name=f"Project {index}", selector=f"#project-{index}")
            for index in range(1, 7)
        ],
        ObservedElement(tag="h2", name="Impact", selector="#impact"),
        *[
            ObservedElement(tag="h4", name=f"Metric {index}", selector=f"#metric-{index}")
            for index in range(1, 5)
        ],
    ]
    groups = _editorial_landmark_groups(landmarks)
    flattened = [item.name for group in groups for item in group]
    assert flattened == [item.name for item in landmarks]
    assert len(groups) < len(landmarks)
    assert all(group for group in groups)


def _context() -> ProductContext:
    home = "https://portfolio.test/"
    timeline = "https://portfolio.test/timeline"
    designs = "https://portfolio.test/designs"
    pages = [
        PageKnowledge(
            url=home,
            title="Ava",
            purpose="identity and featured work",
            visible_sections=["About Ava", "Featured projects"],
            scroll_landmarks=["About Ava", "Atlas"],
            visible_facts=["Ava builds reliable systems", "Atlas coordinates release planning"],
            evidence_refs=["home-shot", "home-dom"],
            fingerprint="h",
        ),
        PageKnowledge(
            url=timeline,
            title="Timeline",
            purpose="career progression",
            visible_sections=["Smartsevak", "Datansh Solutions"],
            scroll_landmarks=["Smartsevak", "Datansh Solutions"],
            visible_facts=[
                "Senior engineer at Smartsevak",
                "Earlier internship at Datansh Solutions",
            ],
            evidence_refs=["timeline-shot", "timeline-dom"],
            fingerprint="t",
        ),
        PageKnowledge(
            url=designs,
            title="System Designs",
            purpose="system design work",
            visible_sections=["Queue processing"],
            scroll_landmarks=["Queue processing"],
            visible_facts=["A queue design explains resilient processing"],
            evidence_refs=["design-shot"],
            fingerprint="d",
        ),
    ]
    elements = [
        ObservedElement(
            tag="h1", name="About Ava", selector="#about", source_url=home, actionable=False
        ),
        ObservedElement(
            tag="h2", name="Atlas", selector="#atlas", source_url=home, actionable=False
        ),
        ObservedElement(
            tag="a",
            name="Timeline",
            selector="#timeline-nav",
            href="/timeline",
            source_url=home,
            navigation_scope="primary",
        ),
        ObservedElement(
            tag="a",
            name="System Designs",
            selector="#design-nav",
            href="/designs",
            source_url=timeline,
            navigation_scope="primary",
        ),
        ObservedElement(
            tag="h1",
            name="Smartsevak",
            selector="#smartsevak",
            source_url=timeline,
            actionable=False,
        ),
        ObservedElement(
            tag="h2",
            name="Datansh Solutions",
            selector="#datansh",
            source_url=timeline,
            actionable=False,
        ),
        ObservedElement(
            tag="h1",
            name="Queue processing",
            selector="#queue",
            source_url=designs,
            actionable=False,
        ),
    ]
    return ProductContext(
        url=home,
        title="Ava",
        application_type="portfolio",
        elements=elements,
        navigation=[element for element in elements if element.href],
        page_knowledge=pages,
        objective=ObjectiveSpec(
            raw="Complete walkthrough", demo_type="full_walkthrough", depth="thorough"
        ),
    )


def test_complete_candidate_is_page_knowledge_ordered_and_not_a_route_sweep():
    context = _context()
    flows = candidate_flows_from_evidence(context, "Complete walkthrough")
    selected = select_candidate_flow(context, "Complete walkthrough")
    assert len(flows) >= 1
    assert selected is not None
    assert selected.page_urls == [
        "https://portfolio.test/",
        "https://portfolio.test/timeline",
        "https://portfolio.test/designs",
    ]
    assert "opening page is completed before transition" in selected.rationale


def test_page_complete_proposal_finishes_home_before_timeline_without_returning_home():
    context = _context()
    candidate = select_candidate_flow(context, "Complete walkthrough")
    proposal = build_page_complete_proposal(context, candidate)
    home = [step for step in proposal.steps if step.page_url == "https://portfolio.test/"]
    assert {phase for step in home for phase in step.page_contract_phases} == {
        "establish",
        "explore",
        "explain",
        "demonstrate",
        "verify",
    }
    transitions = [step for step in proposal.steps if step.story_phase == "transition"]
    assert [step.target.name for step in transitions] == ["Timeline", "System Designs"]
    assert [step.kind.value for step in proposal.steps].count("Navigate") == 1
    assert all("Open the Timeline tab" not in step.intent for step in proposal.steps)
    timeline_content = [
        step.target.name
        for step in proposal.steps
        if step.page_url == "https://portfolio.test/timeline" and step.kind.value == "ScrollTo"
    ]
    # The current role is already readable when Timeline opens, so it is held
    # as a verified state rather than faking a zero-distance scroll. The
    # internship remains a real continuous exploration beat.
    assert timeline_content == ["Datansh Solutions"]
    assert any(
        step.kind.value == "VerifyState" and step.target and step.target.name == "Smartsevak"
        for step in proposal.steps
    )
    datansh = next(
        step for step in proposal.steps if step.target and step.target.name == "Datansh Solutions"
    )
    assert "Earlier internship at Datansh Solutions" in datansh.intent


class _UnusedProvider:
    async def structured(
        self, *_args, **_kwargs
    ):  # pragma: no cover - complete plans must not call it
        raise AssertionError("complete evidence plans must not use route-tour model planning")


@pytest.mark.asyncio
async def test_full_planner_emits_page_phase_metadata_from_evidence():
    context = _context()
    plan = await ProductionPlanningService(_UnusedProvider()).plan(
        objective="Create a complete thorough walkthrough",
        context=context,
        target_duration_seconds=120,
    )
    assert {
        phase
        for step in plan.workflow_steps
        for phase in ([step.operation.story_phase] if step.operation.story_phase else [])
        + step.operation.page_contract_phases
    } >= {"establish", "explore", "explain", "demonstrate", "verify", "transition"}
    timeline_transition = next(
        step for step in plan.workflow_steps if step.operation.story_phase == "transition"
    )
    assert timeline_transition.operation.kind.value == "OpenNavigationItem"
    assert "page:https://portfolio.test/timeline" in timeline_transition.evidence_refs


def test_page_landmarks_do_not_turn_footer_or_primary_navigation_into_reading_scenes():
    contact = "https://portfolio.test/contact"
    context = _context().model_copy(
        update={
            "page_knowledge": [
                PageKnowledge(
                    url=contact,
                    title="Contact",
                    purpose="contact path",
                    visible_sections=["Connect", "Send a Message"],
                    scroll_landmarks=["Connect", "Send a Message"],
                    visible_facts=["A contact form is available"],
                    evidence_refs=["contact-shot"],
                    fingerprint="contact",
                )
            ],
            "elements": [
                ObservedElement(tag="h1", name="Connect", selector="#connect", source_url=contact),
                ObservedElement(
                    tag="h2", name="Send a Message", selector="#message", source_url=contact
                ),
                ObservedElement(
                    tag="a",
                    name="Home",
                    selector="#footer-home",
                    href="/",
                    source_url=contact,
                    navigation_scope="footer",
                ),
                ObservedElement(
                    tag="a",
                    name="Timeline",
                    selector="#nav-timeline",
                    href="/timeline",
                    source_url=contact,
                    navigation_scope="primary",
                ),
            ],
        }
    )
    landmarks = _page_landmarks(context, context.page_knowledge[0])
    assert [landmark.name for landmark in landmarks] == ["Connect", "Send a Message"]


def test_page_landmarks_keep_one_representative_numbered_collection_item():
    root = "https://study.test/"
    page = PageKnowledge(
        url=root,
        title="Study Plan",
        purpose="Weeks",
        visible_sections=["Weeks", "Week 1", "Week 2", "Week 3"],
        scroll_landmarks=["Weeks", "Week 1", "Week 2", "Week 3"],
        fingerprint="study-weeks",
    )
    context = ProductContext(
        url=root,
        title="Study Plan",
        application_type="planner",
        page_knowledge=[page],
        elements=[
            ObservedElement(tag="h2", name="Weeks", selector="#weeks", source_url=root),
            *[
                ObservedElement(
                    tag="h3", name=f"Week {index}", selector=f"#week-{index}", source_url=root
                )
                for index in range(1, 4)
            ],
        ],
        confidence=1.0,
    )
    assert [item.name for item in _page_landmarks(context, page)] == ["Weeks", "Week 1"]


def test_page_landmarks_exclude_record_grid_chrome_and_placeholder_controls():
    root = "https://app.test/bookings"
    page = PageKnowledge(
        url=root,
        title="Bookings",
        purpose="appointment workspace",
        visible_facts=["List Bookings :: Manage and track appointments in one workspace."],
        fingerprint="bookings",
    )
    context = ProductContext(
        url=root,
        title="Bookings",
        application_type="dashboard",
        page_knowledge=[page],
        elements=[
            ObservedElement(
                tag="h1", name="Bookings", selector="h1", source_url=root, actionable=False
            ),
            ObservedElement(
                tag="span", role="button", name="PATIENT", selector="span", source_url=root
            ),
            ObservedElement(tag="td", name="A real customer", selector="td", source_url=root),
            ObservedElement(tag="td", name="22 Sep 2026 11:00", selector="td", source_url=root),
            ObservedElement(tag="input", name="element-40", selector="input", source_url=root),
            ObservedElement(tag="button", name="Create", selector="button", source_url=root),
        ],
    )
    landmarks = _page_landmarks(context, page)
    names = [item.name for item in landmarks]
    assert names == ["Bookings"]


def test_page_complete_proposal_never_emits_anonymous_form_element_target():
    root = "https://app.test/bookings"
    page = PageKnowledge(
        url=root,
        title="Bookings",
        purpose="appointment workspace",
        visible_sections=["Bookings"],
        scroll_landmarks=["Bookings"],
        visible_facts=["Bookings :: Manage appointments and review their current status."],
        fingerprint="bookings",
    )
    context = ProductContext(
        url=root,
        title="Bookings",
        application_type="dashboard",
        page_knowledge=[page],
        elements=[
            ObservedElement(
                tag="h1", name="Bookings", selector="h1", source_url=root, actionable=False
            ),
            ObservedElement(tag="input", name="element-40", selector="input", source_url=root),
        ],
    )
    from app.planning.candidates import CandidateDemoFlow

    proposal = build_page_complete_proposal(
        context,
        CandidateDemoFlow(
            name="bookings", page_urls=[root], estimated_duration_seconds=30, score=1.0
        ),
    )
    assert all(
        step.target is None or not step.target.name.startswith("element-")
        for step in proposal.steps
    )


@pytest.mark.asyncio
async def test_full_storyboard_reserves_time_for_native_motion_instead_of_five_second_holds():
    from app.presentation.editorial import build_editorial_storyboard

    context = _context()
    plan = await ProductionPlanningService(_UnusedProvider()).plan(
        objective="Create a complete thorough walkthrough",
        context=context,
        target_duration_seconds=120,
    )
    storyboard = build_editorial_storyboard(context, plan)
    # A source-faithful capture also contains gradual scrolls and transitions.
    # A fixed five-second hold per step would push this small fixture far beyond
    # its requested duration before rendering has even started.
    operation_scenes = [scene for scene in storyboard.scenes if scene.operation_id]
    assert max(scene.required_dwell_seconds for scene in operation_scenes) < 5.0
    assert min(scene.required_dwell_seconds for scene in operation_scenes) >= 2.25


@pytest.mark.asyncio
async def test_focused_multi_beat_story_allocates_native_reading_time_to_script():
    from app.presentation.editorial import build_editorial_storyboard

    context = _context()
    plan = await ProductionPlanningService(_UnusedProvider()).plan(
        objective="Create a walkthrough of the task board workflow for a delivery manager",
        context=context,
        target_duration_seconds=90,
    )
    storyboard = build_editorial_storyboard(context, plan)
    operation_scenes = [scene for scene in storyboard.scenes if scene.operation_id]
    # Focused stories with several evidence beats must capture enough native
    # page time for the approved captions; a renderer must not manufacture it
    # later by freezing or slowing the source footage.
    assert len(operation_scenes) > 4
    assert min(scene.required_dwell_seconds for scene in operation_scenes) >= 6.0
