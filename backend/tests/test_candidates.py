from productlens.contracts.models import (
    CandidateDemoFlow,
    ObjectiveSpec,
    ObservedElement,
    OperationKind,
    PageKnowledge,
    Postcondition,
    ProductContext,
    SemanticOperation,
    WorkflowProposal,
)
from productlens.planning.candidates import select_candidate_flow, validate_flow_scope


def _context() -> ProductContext:
    return ProductContext(
        url="https://demo.test/", title="Demo", application_type="web_application",
        objective=ObjectiveSpec(raw="Show reporting", requested_features=["reporting"]),
        candidate_demo_flows=[
            CandidateDemoFlow(name="full application", page_urls=["https://demo.test/", "https://demo.test/reports", "https://demo.test/settings", "https://demo.test/billing"], expected_outcomes=["all pages"], score=.9),
            CandidateDemoFlow(name="reporting workflow", page_urls=["https://demo.test/", "https://demo.test/reports"], rationale=["reporting context"], expected_outcomes=["report export"], evidence_coverage=["section: Reports"], score=.7),
        ], confidence=1,
    )


def test_narrow_objective_prefers_relevant_candidate_over_broad_route_collection():
    candidate = select_candidate_flow(_context(), "Show reporting and the report export")
    assert candidate and candidate.name == "reporting workflow"


def test_candidate_scope_rejects_unrelated_navigation():
    context = _context()
    candidate = select_candidate_flow(context, "Show reporting")
    proposal = WorkflowProposal(
        narrative_goal="show report", selected_workflow="report", expected_outcomes=["report"],
        steps=[SemanticOperation(kind=OperationKind.NAVIGATE, intent="Open billing", value="/billing", postconditions=[Postcondition(kind="url", expected="https://demo.test/billing")])],
    )
    assert validate_flow_scope(proposal, context=context, candidate=candidate) == ["CANDIDATE_FLOW_SCOPE_VIOLATION:https://demo.test/billing"]


def test_full_walkthrough_keeps_deep_probe_as_evidence_not_a_route_chapter():
    context = ProductContext(
        url="https://example.test/", title="Example", application_type="portfolio",
        page_knowledge=[
            PageKnowledge(url="https://example.test/", title="Home", purpose="Home", visible_sections=["Home"], fingerprint="home"),
            PageKnowledge(url="https://example.test/timeline", title="Timeline", purpose="Timeline", visible_sections=["Timeline"], fingerprint="timeline"),
            PageKnowledge(url="https://example.test/notes", title="Notes", purpose="Notes", visible_sections=["Notes"], fingerprint="notes"),
            PageKnowledge(url="https://example.test/notes/retries", title="Retry mechanics", purpose="Retry mechanics", visible_sections=["Retries"], fingerprint="retries"),
        ],
    )
    flow = select_candidate_flow(context, "Create a complete walkthrough")
    assert flow is not None
    assert flow.page_urls == ["https://example.test/", "https://example.test/timeline", "https://example.test/notes"]


def test_full_walkthrough_keeps_one_visible_representative_detail_with_its_source_page():
    context = ProductContext(
        url="https://example.test/", title="Example", application_type="dashboard",
        page_knowledge=[
            PageKnowledge(url="https://example.test/", title="Home", purpose="Home", visible_sections=["Overview"], fingerprint="home"),
            PageKnowledge(url="https://example.test/weeks", title="Weeks", purpose="Weeks", visible_sections=["Weeks"], fingerprint="weeks"),
            PageKnowledge(url="https://example.test/weeks/01", title="Week 1", purpose="Week 1", visible_sections=["Monday"], fingerprint="week-1"),
            PageKnowledge(url="https://example.test/progress", title="Progress", purpose="Progress", visible_sections=["Progress"], fingerprint="progress"),
        ],
        elements=[
            ObservedElement(tag="a", name="Week 1", selector="a", href="/weeks/01", source_url="https://example.test/", actionable=True),
            ObservedElement(tag="a", name="Weeks", selector="a", href="/weeks", source_url="https://example.test/", actionable=True, navigation_scope="primary"),
            ObservedElement(tag="a", name="Progress", selector="a", href="/progress", source_url="https://example.test/", actionable=True, navigation_scope="primary"),
        ],
    )
    flow = select_candidate_flow(context, "Create a complete walkthrough")
    assert flow is not None
    # The bounded detail scene follows the page where its visible card was
    # observed, rather than becoming a detached deep-route crawl.
    assert flow.page_urls == [
        "https://example.test/",
        "https://example.test/weeks",
        "https://example.test/weeks/01",
        "https://example.test/progress",
    ]
