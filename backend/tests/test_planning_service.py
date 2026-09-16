from app.benchmark.fixture_planning import FixturePlanningService


def test_planning_service_excludes_irrelevant_sections():
    plan = FixturePlanningService().plan("Invite a new teammate")
    assert plan.selected_workflow == "section-users.html"
    assert set(plan.excluded_areas) == {"section-billing.html", "section-settings.html"}
