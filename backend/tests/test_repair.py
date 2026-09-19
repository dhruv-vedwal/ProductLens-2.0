from app.quality.repair import classify_repair


def test_repair_decision_names_the_earliest_safe_repair_boundary():
    assert classify_repair(["EXCESSIVE_FROZEN_VIDEO"]).retry_from_stage == "RENDER"
    assert classify_repair(["OBJECTIVE_OUTCOME_UNVERIFIED"]).retry_from_stage == "EXECUTION"
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


def test_missing_certified_outcome_retries_the_workflow_boundary():
    decision = classify_repair(["CERTIFIED_OUTCOME_MISSING"])
    assert decision.category == "workflow"
    assert decision.retry_from_stage == "PLANNING"


def test_external_blockers_require_input_instead_of_blind_retry():
    decision = classify_repair(["AUTH_REQUIRED"])
    assert (decision.category, decision.action, decision.retry_from_stage) == (
        "external_input",
        "needs_input",
        None,
    )


def test_rehearsal_outcome_unververified_retries_discovery_not_execution():
    decision = classify_repair(
        [
            "PLANNING_GENERATIONPRECONDITIONERROR",
            "REHEARSAL_OUTCOME_UNVERIFIED: SUBMISSION DID NOT YIELD AN INDEPENDENT VISIBLE RESULT WITNESS",
        ]
    )
    assert decision.category == "discovery"
    assert decision.retry_from_stage == "DISCOVERY"

