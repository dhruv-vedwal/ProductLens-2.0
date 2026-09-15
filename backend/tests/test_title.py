from productlens.presentation.title import concise_demo_title


def test_title_keeps_visible_destinations_not_execution_purposes():
    title = concise_demo_title(
        "Show how a learner moves through Today, a focused week, and progress in this study plan",
        action_labels=[
            "Go to the Today page to start the learner's journey",
            "Open Week 1 to view its details",
            "Navigate to Progress so the relevant workspace is in view",
        ],
    )
    assert title == "Today page, Week 1 & Progress"


def test_title_uses_the_objective_without_site_specific_special_cases():
    assert (
        concise_demo_title(
            "Create a full walkthrough of every safe primary tab and the project collection."
        )
        == "Create a full walkthrough of every safe"
    )
