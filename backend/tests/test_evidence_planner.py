import pytest

from productlens.contracts.models import (
    ObjectiveSpec,
    ObservedElement,
    PageKnowledge,
    ProductContext,
)
from productlens.planning.candidates import (
    _page_landmarks,
    build_page_complete_proposal,
    candidate_flows_from_evidence,
    select_candidate_flow,
)
from productlens.planning.production import ProductionPlanningService


def _context() -> ProductContext:
    home = "https://portfolio.test/"
    timeline = "https://portfolio.test/timeline"
    designs = "https://portfolio.test/designs"
    pages = [
        PageKnowledge(url=home, title="Ava", purpose="identity and featured work",
                      visible_sections=["About Ava", "Featured projects"], scroll_landmarks=["About Ava", "Atlas"],
                      visible_facts=["Ava builds reliable systems", "Atlas coordinates release planning"],
                      evidence_refs=["home-shot", "home-dom"], fingerprint="h"),
        PageKnowledge(url=timeline, title="Timeline", purpose="career progression",
                      visible_sections=["Smartsevak", "Datansh Solutions"], scroll_landmarks=["Smartsevak", "Datansh Solutions"],
                      visible_facts=["Senior engineer at Smartsevak", "Earlier internship at Datansh Solutions"],
                      evidence_refs=["timeline-shot", "timeline-dom"], fingerprint="t"),
        PageKnowledge(url=designs, title="System Designs", purpose="system design work",
                      visible_sections=["Queue processing"], scroll_landmarks=["Queue processing"],
                      visible_facts=["A queue design explains resilient processing"],
                      evidence_refs=["design-shot"], fingerprint="d"),
    ]
    elements = [
        ObservedElement(tag="h1", name="About Ava", selector="#about", source_url=home, actionable=False),
        ObservedElement(tag="h2", name="Atlas", selector="#atlas", source_url=home, actionable=False),
        ObservedElement(tag="a", name="Timeline", selector="#timeline-nav", href="/timeline", source_url=home, navigation_scope="primary"),
        ObservedElement(tag="a", name="System Designs", selector="#design-nav", href="/designs", source_url=timeline, navigation_scope="primary"),
        ObservedElement(tag="h1", name="Smartsevak", selector="#smartsevak", source_url=timeline, actionable=False),
        ObservedElement(tag="h2", name="Datansh Solutions", selector="#datansh", source_url=timeline, actionable=False),
        ObservedElement(tag="h1", name="Queue processing", selector="#queue", source_url=designs, actionable=False),
    ]
    return ProductContext(url=home, title="Ava", application_type="portfolio", elements=elements,
                          navigation=[element for element in elements if element.href], page_knowledge=pages,
                          objective=ObjectiveSpec(raw="Complete walkthrough", demo_type="full_walkthrough", depth="thorough"))


def test_complete_candidate_is_page_knowledge_ordered_and_not_a_route_sweep():
    context = _context()
    flows = candidate_flows_from_evidence(context, "Complete walkthrough")
    selected = select_candidate_flow(context, "Complete walkthrough")
    assert len(flows) >= 1
    assert selected is not None
    assert selected.page_urls == ["https://portfolio.test/", "https://portfolio.test/timeline", "https://portfolio.test/designs"]
    assert "opening page is completed before transition" in selected.rationale


def test_page_complete_proposal_finishes_home_before_timeline_without_returning_home():
    context = _context()
    candidate = select_candidate_flow(context, "Complete walkthrough")
    proposal = build_page_complete_proposal(context, candidate)
    home = [step for step in proposal.steps if step.page_url == "https://portfolio.test/"]
    assert {phase for step in home for phase in step.page_contract_phases} == {"establish", "explore", "explain", "demonstrate", "verify"}
    transitions = [step for step in proposal.steps if step.story_phase == "transition"]
    assert [step.target.name for step in transitions] == ["Timeline", "System Designs"]
    assert [step.kind.value for step in proposal.steps].count("Navigate") == 1
    assert all("Open the Timeline tab" not in step.intent for step in proposal.steps)
    timeline_content = [
        step.target.name for step in proposal.steps
        if step.page_url == "https://portfolio.test/timeline"
        and step.kind.value == "ScrollTo"
    ]
    # Each observed career card is a required group, not a three-beat budget
    # casualty. This protects both the current role and the internship scene.
    assert timeline_content == ["Smartsevak", "Datansh Solutions"]
    datansh = next(
        step for step in proposal.steps
        if step.target and step.target.name == "Datansh Solutions"
    )
    assert "Earlier internship at Datansh Solutions" in datansh.intent


class _UnusedProvider:
    async def structured(self, *_args, **_kwargs):  # pragma: no cover - complete plans must not call it
        raise AssertionError("complete evidence plans must not use route-tour model planning")


@pytest.mark.asyncio
async def test_full_planner_emits_page_phase_metadata_from_evidence():
    context = _context()
    plan = await ProductionPlanningService(_UnusedProvider()).plan(
        objective="Create a complete thorough walkthrough", context=context, target_duration_seconds=120
    )
    assert {phase for step in plan.workflow_steps for phase in ([step.operation.story_phase] if step.operation.story_phase else []) + step.operation.page_contract_phases} >= {"establish", "explore", "explain", "demonstrate", "verify", "transition"}
    timeline_transition = next(step for step in plan.workflow_steps if step.operation.story_phase == "transition")
    assert timeline_transition.operation.kind.value == "OpenNavigationItem"
    assert "page:https://portfolio.test/timeline" in timeline_transition.evidence_refs


def test_page_landmarks_do_not_turn_footer_or_primary_navigation_into_reading_scenes():
    contact = "https://portfolio.test/contact"
    context = _context().model_copy(update={
        "page_knowledge": [PageKnowledge(
            url=contact, title="Contact", purpose="contact path",
            visible_sections=["Connect", "Send a Message"],
            scroll_landmarks=["Connect", "Send a Message"],
            visible_facts=["A contact form is available"], evidence_refs=["contact-shot"], fingerprint="contact",
        )],
        "elements": [
            ObservedElement(tag="h1", name="Connect", selector="#connect", source_url=contact),
            ObservedElement(tag="h2", name="Send a Message", selector="#message", source_url=contact),
            ObservedElement(tag="a", name="Home", selector="#footer-home", href="/", source_url=contact, navigation_scope="footer"),
            ObservedElement(tag="a", name="Timeline", selector="#nav-timeline", href="/timeline", source_url=contact, navigation_scope="primary"),
        ],
    })
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
                ObservedElement(tag="h3", name=f"Week {index}", selector=f"#week-{index}", source_url=root)
                for index in range(1, 4)
            ],
        ],
        confidence=1.0,
    )
    assert [item.name for item in _page_landmarks(context, page)] == ["Weeks", "Week 1"]


@pytest.mark.asyncio
async def test_full_storyboard_reserves_time_for_native_motion_instead_of_five_second_holds():
    from productlens.presentation.editorial import build_editorial_storyboard

    context = _context()
    plan = await ProductionPlanningService(_UnusedProvider()).plan(
        objective="Create a complete thorough walkthrough", context=context, target_duration_seconds=120
    )
    storyboard = build_editorial_storyboard(context, plan)
    # A source-faithful capture also contains gradual scrolls and transitions.
    # A fixed five-second hold per step would push this small fixture far beyond
    # its requested duration before rendering has even started.
    operation_scenes = [scene for scene in storyboard.scenes if scene.operation_id]
    assert max(scene.required_dwell_seconds for scene in operation_scenes) < 5.0
    assert min(scene.required_dwell_seconds for scene in operation_scenes) >= 2.25
