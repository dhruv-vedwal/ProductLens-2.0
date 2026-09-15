from productlens.presentation.title import concise_demo_title


def test_presentation_title_is_concise_and_objective_backed():
    assert (
        concise_demo_title("Show how to create a lead and schedule a follow-up booking")
        == "Create a lead and schedule a follow-up"
    )


def test_presentation_title_has_safe_fallback():
    assert concise_demo_title(" ") == "Product walkthrough"


def test_presentation_title_omits_following_execution_constraint():
    assert (
        concise_demo_title("Demonstrate the study planner. Do not change any data.")
        == "The study planner"
    )


def test_presentation_title_uses_verified_action_sequence_for_long_compound_objective():
    assert (
        concise_demo_title(
            "Demonstrate the Today view, open one weekly study plan, and show how progress is reviewed.",
            action_labels=["Start from Today", "Open Week 1", "Open Progress"],
        )
        == "Today, Week 1 & Progress"
    )
