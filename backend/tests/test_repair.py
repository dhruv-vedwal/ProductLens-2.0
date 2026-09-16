from app.quality.repair import classify_repair


def test_repair_decision_names_the_earliest_safe_repair_boundary():
    assert classify_repair(["EXCESSIVE_FROZEN_VIDEO"]).retry_from_stage == "RENDERING"
    assert (
        classify_repair(["OBJECTIVE_OUTCOME_UNVERIFIED"]).retry_from_stage == "PRODUCTION_EXECUTION"
    )
    assert classify_repair(["PROVIDER_FAILURE"]).retry_from_stage is None
    decision = classify_repair(["NAVIGATED_PAGE_NOT_EXPLORED"])
    assert (decision.action, decision.retry_from_stage) == ("targeted_reexecution", "PLANNING")
    capture = classify_repair(["CAPTURE_DURATION_EXCEEDS_OBJECTIVE_MAXIMUM"])
    assert (capture.action, capture.retry_from_stage) == ("targeted_reexecution", "PLANNING")


def test_wrong_scene_caption_repairs_the_narration_layer_only():
    decision = classify_repair(["UNSUPPORTED_OR_WRONG_SCENE_CAPTION"])
    assert (decision.category, decision.action, decision.retry_from_stage) == (
        "narration",
        "regenerate_narration",
        "NARRATION",
    )


def test_repair_owns_discovery_and_workflow_failures():
    discovery = classify_repair(["STALE_KNOWLEDGE"])
    assert (discovery.category, discovery.retry_boundary) == ("discovery", "targeted-exploration")
    workflow = classify_repair(["WORKFLOW_VALIDATION_FAILED"])
    assert (workflow.category, workflow.retry_from_stage) == ("workflow", "PLANNING")
