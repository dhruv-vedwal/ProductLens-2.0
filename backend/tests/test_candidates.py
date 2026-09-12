from productlens.contracts.models import (
    CandidateDemoFlow,
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
from productlens.planning.candidates import candidate_flows_from_evidence, select_candidate_flow, validate_flow_scope


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


def test_generated_candidate_duration_reflects_captured_content_density():
    root = "https://demo.test/"
    sparse = PageKnowledge(url=root, title="Demo", purpose="Overview", visible_sections=["Overview"], fingerprint="sparse")
    rich = PageKnowledge(
        url=root, title="Demo", purpose="Overview",
        visible_sections=["Overview", "Projects", "Timeline", "Contact"],
        scroll_landmarks=["Projects", "Timeline", "Contact"],
        visible_facts=[
            "Projects :: A collection of documented product work.",
            "Timeline :: A visible career progression.",
            "Contact :: A direct contact path.",
        ], fingerprint="rich",
    )
    sparse_context = ProductContext(url=root, title="Demo", application_type="portfolio", page_knowledge=[sparse])
    rich_context = ProductContext(url=root, title="Demo", application_type="portfolio", page_knowledge=[rich])

    sparse_candidate = select_candidate_flow(sparse_context, "Show the overview")
    rich_candidate = select_candidate_flow(rich_context, "Show the overview")

    assert sparse_candidate is not None and rich_candidate is not None
    assert rich_candidate.estimated_duration_seconds > sparse_candidate.estimated_duration_seconds


def test_feature_opening_page_is_not_replaced_by_noun_matching_support_route():
    context = ProductContext(
        url="https://demo.test/leads-v3", title="Demo", application_type="dashboard",
        objective=ObjectiveSpec(raw="Show the lead management experience and its configuration", requested_features=["lead", "management", "configuration"]),
        page_knowledge=[
            PageKnowledge(url="https://demo.test/leads-v3", title="Demo", purpose="Leads", visible_sections=["Leads", "Filters", "Lead name"], visible_facts=["Manage incoming leads and review their status."], fingerprint="leads"),
            PageKnowledge(url="https://demo.test/settings", title="Demo", purpose="Settings", visible_sections=["Lead Configuration"], fingerprint="settings"),
            PageKnowledge(url="https://demo.test/templates", title="Demo", purpose="Lead Templates", visible_sections=["Templates"], fingerprint="templates"),
        ],
        candidate_demo_flows=[
            CandidateDemoFlow(name="templates", page_urls=["https://demo.test/leads-v3", "https://demo.test/templates"], score=1.0),
            CandidateDemoFlow(name="configuration", page_urls=["https://demo.test/leads-v3", "https://demo.test/settings"], score=.8),
        ],
    )
    candidate = select_candidate_flow(context, "Show the lead management experience and its configuration")
    assert candidate is not None
    assert candidate.page_urls == ["https://demo.test/leads-v3", "https://demo.test/settings"]


def test_feature_context_prefers_a_relation_detail_page_over_a_settings_shell():
    leads = "https://demo.test/leads"
    settings = "https://demo.test/settings"
    config = "https://demo.test/settings/lead-config"
    context = ProductContext(
        url=leads, title="Demo", application_type="dashboard",
        objective=ObjectiveSpec(
            raw="Show Lead Management in the context of Lead Configuration",
            requested_features=["lead", "management", "configuration"],
            supporting_relationships=[{"source": "Lead Configuration", "target": "Lead Management"}],
        ),
        page_knowledge=[
            PageKnowledge(url=leads, title="Lead Management", purpose="Lead workspace", visible_sections=["Lead list"], fingerprint="leads"),
            PageKnowledge(url=settings, title="Settings", purpose="Settings dashboard", visible_sections=["Lead Configuration"], fingerprint="settings"),
            PageKnowledge(url=config, title="Lead Configuration", purpose="Configure lead intake", visible_sections=["Lead source rules"], fingerprint="lead-config"),
        ],
        candidate_demo_flows=[
            CandidateDemoFlow(name="settings shell", page_urls=[leads, settings], score=.95),
            CandidateDemoFlow(name="configuration detail", page_urls=[leads, config], score=.7),
        ],
    )
    candidate = select_candidate_flow(context, context.objective.raw)
    assert candidate is not None
    assert candidate.page_urls == [leads]
    assert candidate.supporting_page_urls == [config]


def test_required_relationship_selects_distinct_context_and_workspace_in_story_order():
    root = "https://demo.test/dashboard"
    settings = "https://demo.test/settings"
    configuration = "https://demo.test/settings/customer/configuration"
    workspace = "https://demo.test/customers-v3"
    context = ProductContext(
        url=root, title="Demo", application_type="dashboard",
        objective=ObjectiveSpec(
            raw="Show Customer Management in the context of Customer Configuration",
            primary_entity="customer management",
            supporting_relationships=[{
                "source": "Customer Configuration", "target": "Customer Management",
            }],
        ),
        page_knowledge=[
            PageKnowledge(url=root, title="Dashboard", purpose="Overview", fingerprint="dashboard"),
            PageKnowledge(url=settings, title="Settings", purpose="Settings", fingerprint="settings"),
            PageKnowledge(url=configuration, title="Configuration", purpose="Customer configuration", fingerprint="customer-config"),
            PageKnowledge(url=workspace, title="Customers V3", purpose="Customers", fingerprint="customers"),
        ],
    )

    candidate = select_candidate_flow(context, context.objective.raw)

    assert candidate is not None
    assert candidate.page_urls == [root, workspace]
    assert candidate.supporting_page_urls == [configuration]
    assert any(configuration in evidence for evidence in candidate.evidence_coverage)


def test_candidate_scope_rejects_unrelated_navigation():
    context = _context()
    candidate = select_candidate_flow(context, "Show reporting")
    proposal = WorkflowProposal(
        narrative_goal="show report", selected_workflow="report", expected_outcomes=["report"],
        steps=[SemanticOperation(kind=OperationKind.NAVIGATE, intent="Open billing", value="/billing", postconditions=[Postcondition(kind="url", expected="https://demo.test/billing")])],
    )
    assert validate_flow_scope(proposal, context=context, candidate=candidate) == ["CANDIDATE_FLOW_SCOPE_VIOLATION:https://demo.test/billing"]


def test_candidate_scope_rejects_action_from_a_page_that_is_no_longer_active():
    leads = "https://demo.test/leads"
    settings = "https://demo.test/settings"
    context = ProductContext(
        url=leads, title="Demo", application_type="dashboard",
        elements=[
            ObservedElement(tag="a", name="Settings", selector="#settings", href="/settings", source_url=leads, actionable=True),
            ObservedElement(tag="button", name="New record", selector="#new", source_url=leads, actionable=True),
        ],
        candidate_demo_flows=[CandidateDemoFlow(name="workflow", page_urls=[leads, settings], score=.9)],
    )
    proposal = WorkflowProposal(
        narrative_goal="show workflow", selected_workflow="workflow", expected_outcomes=["record"],
        steps=[
            SemanticOperation(kind=OperationKind.NAVIGATE, intent="Open leads", value=leads, postconditions=[Postcondition(kind="url", expected=leads)]),
            SemanticOperation(kind=OperationKind.OPEN_NAVIGATION_ITEM, intent="Open settings", target=Target(name="Settings", selector="#settings", source_url=leads), postconditions=[Postcondition(kind="url", expected=settings)]),
            SemanticOperation(kind=OperationKind.OPEN_MODAL, intent="Open record form", target=Target(name="New record", selector="#new", source_url=leads), postconditions=[Postcondition(kind="visible", expected=True)]),
        ],
    )
    failures = validate_flow_scope(proposal, context=context, candidate=context.candidate_demo_flows[0])
    assert any(failure.startswith("WORKFLOW_PAGE_STATE_DISCONTINUITY:New record") for failure in failures)


def test_reversible_form_capability_is_an_evidence_backed_duration_beat():
    records = "https://demo.test/records"
    context = ProductContext(
        url=records, title="Records", application_type="dashboard",
        objective=ObjectiveSpec(raw="Show record workflow", primary_entity="record workflow"),
        page_knowledge=[PageKnowledge(
            url=records, title="Records", purpose="Records", visible_sections=["Records"],
            actionable_controls=["New record", "Filter", "Export"], fingerprint="records",
        )],
        capabilities=[{
            "kind": "form", "purpose": "New record", "source_url": records,
            "evidence_refs": [f"capability:{records}:New record"],
        }],
    )

    flow = candidate_flows_from_evidence(context, context.objective.raw)[0]

    assert flow.estimated_duration_seconds >= 60
    assert any(reference.startswith("capability:") for reference in flow.evidence_coverage)


def test_candidate_scope_rejects_direct_navigation_when_visible_control_exists():
    root = "https://demo.test/"
    reports = "https://demo.test/reports"
    context = ProductContext(
        url=root, title="Demo", application_type="dashboard",
        navigation=[ObservedElement(tag="a", name="Reports", selector="#reports", href="/reports", source_url=root, actionable=True, navigation_scope="primary")],
        candidate_demo_flows=[CandidateDemoFlow(name="reports", page_urls=[root, reports], score=.9)],
    )
    proposal = WorkflowProposal(
        narrative_goal="show reports", selected_workflow="reports", expected_outcomes=["reports"],
        steps=[
            SemanticOperation(kind=OperationKind.NAVIGATE, intent="Open home", value=root, postconditions=[Postcondition(kind="url", expected=root)]),
            SemanticOperation(kind=OperationKind.NAVIGATE, intent="Open reports", value=reports, postconditions=[Postcondition(kind="url", expected=reports)]),
        ],
    )
    failures = validate_flow_scope(proposal, context=context, candidate=select_candidate_flow(context, "Show reports"))
    assert f"WORKFLOW_DIRECT_NAVIGATION_WHEN_VISIBLE_CONTROL:{reports}" in failures


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
