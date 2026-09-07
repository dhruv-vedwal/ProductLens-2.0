from productlens.benchmark.fixture_discovery import FixtureTargetedDiscovery
from productlens.contracts.models import ObservedElement, PageKnowledge, ProductContext
from productlens.discovery.live import (
    _canonical_route,
    _objective_spec,
    _page_knowledge,
    _restore_missing_page_landmarks,
)


def test_invite_does_not_crawl_unrelated_sections():
    result = FixtureTargetedDiscovery().select("Invite a new teammate")
    assert result.selected_route == "section-users.html"
    assert "section-billing.html" not in result.visited_routes
    assert "section-settings.html" not in result.visited_routes


def test_export_targets_reports():
    result = FixtureTargetedDiscovery().select("Export last month's report")
    assert result.selected_route == "section-reports.html"


def test_full_walkthrough_objective_requires_thorough_exploration_contract():
    objective = _objective_spec("Create a full walkthrough of every primary tab")
    assert objective.depth == "thorough"
    assert objective.minimum_duration_seconds >= 110
    assert "each selected page is explored before transition" in objective.success_criteria


def test_duration_words_do_not_break_full_walkthrough_detection():
    objective = _objective_spec("Create a full 2 to 3 minute walkthrough of each primary section")
    assert objective.demo_type == "full_walkthrough"


def test_complete_every_primary_section_is_full_walkthrough_intent():
    objective = _objective_spec(
        "Create a complete, evidence-grounded walkthrough of every safe primary section"
    )
    assert objective.demo_type == "full_walkthrough"
    assert objective.depth == "thorough"


def test_punctuated_complete_duration_request_is_a_full_walkthrough():
    objective = _objective_spec("Create a complete, evidence-backed 2 to 3 minute walkthrough")
    assert objective.demo_type == "full_walkthrough"
    assert objective.maximum_duration_seconds == 240


def test_objective_spec_does_not_privilege_a_known_application_label():
    objective = _objective_spec("Demonstrate the invoice approval queue")
    assert "invoice" in objective.must_show
    assert "today" not in objective.must_show


def test_page_knowledge_retains_non_secret_screenshot_evidence_reference():
    context = ProductContext(
        url="https://example.test/", title="Example", application_type="dashboard",
        elements=[ObservedElement(tag="h1", name="Overview", selector="h1")],
        evidence=["current DOM", "screenshot:discovery/screenshots/abc.png"], confidence=1,
    )

    page = _page_knowledge(context)

    assert page.screenshot_evidence == "discovery/screenshots/abc.png"


def test_discovery_canonical_route_does_not_reinspect_transport_or_trailing_slash_variants():
    assert _canonical_route("http://Example.Test/") == _canonical_route("https://example.test")
    assert _canonical_route("https://example.test/overview/?ignored=1#section") == "https://example.test/overview"


def test_discovery_restores_role_grounded_page_landmarks_when_dense_dom_truncates_them():
    page = PageKnowledge(
        url="https://example.test/reports", title="Reports", purpose="Reports",
        scroll_landmarks=["Monthly report", "Export history"], fingerprint="reports",
    )
    restored = _restore_missing_page_landmarks([], [page])

    assert [item.name for item in restored] == ["Monthly report", "Export history"]
    assert all(item.role == "heading" and item.source_url == page.url for item in restored)
    assert all(item.selector.startswith("observed-heading:") for item in restored)
