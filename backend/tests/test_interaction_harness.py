from datetime import UTC, datetime

from app.artifacts.store import RunArtifacts
from app.contracts import (
    DemoTrace,
    HarnessFailureCode,
    HarnessStatus,
    InteractionEvent,
    InteractionHarnessRequest,
    OperationKind,
    Target,
    WorkflowState,
)
from app.interaction.harness import (
    classify_harness_exception,
    objective_fingerprint,
    persist_validation,
    validate_trace,
    validation_for_exception,
)
from app.persistence.repository import RunRepository
from app.services.jobs import DemoJobService


def request(**updates):
    payload = {
        "url": "https://example.com",
        "objective": "Open the product overview",
    }
    payload.update(updates)
    return InteractionHarnessRequest.model_validate(payload)


def event(*, success: bool = True) -> InteractionEvent:
    now = datetime.now(UTC)
    return InteractionEvent(
        operation_id="open-overview",
        kind=OperationKind.CLICK,
        intent="Open the product overview",
        occurred_at=now,
        action_at=now,
        target=Target(name="Product overview", role="button"),
        page_url="https://example.com/overview",
        before={"url": "https://example.com"},
        after={"url": "https://example.com/overview"},
        success=success,
        duration_ms=240,
    )


def trace(*events: InteractionEvent, verified: bool = True) -> DemoTrace:
    now = datetime.now(UTC)
    return DemoTrace(
        run_id="harness-test",
        objective="Open the product overview",
        started_at=now,
        completed_at=now,
        events=list(events),
        final_state=WorkflowState.COMPLETE,
        outcome_verified=verified,
    )


def test_request_rejects_raw_auth_reference():
    try:
        request(auth_reference="password=secret")
    except ValueError as error:
        assert "opaque ProductLens secret reference" in str(error)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("raw credentials must never be accepted")


def test_objective_fingerprint_is_stable_and_secret_free():
    first = objective_fingerprint(request(auth_reference="secret://productlens/auth-1"))
    second = objective_fingerprint(request(auth_reference="secret://productlens/auth-2"))
    assert first == second
    assert len(first) == 64


def test_harness_promotes_only_a_verified_trace():
    validation = validate_trace(request(), trace(event()))
    assert validation.result.status is HarnessStatus.VERIFIED
    assert validation.result.outcome.verified is True
    assert validation.result.failure is None


def test_harness_blocks_failed_browser_event():
    validation = validate_trace(request(), trace(event(success=False)))
    assert validation.result.status is HarnessStatus.BLOCKED
    assert validation.result.failure is not None
    assert validation.result.failure.code is HarnessFailureCode.POSTCONDITION_FAILED


def test_harness_blocks_missing_terminal_outcome():
    validation = validate_trace(request(), trace(event(), verified=False))
    assert validation.result.status is HarnessStatus.BLOCKED
    assert validation.result.failure is not None
    assert validation.result.failure.code is HarnessFailureCode.OUTCOME_UNVERIFIED


def test_harness_requires_evidence_when_required_outcomes_are_declared():
    validation = validate_trace(
        request(required_outcomes=["the overview is visible"]), trace(event())
    )
    assert validation.result.status is HarnessStatus.BLOCKED
    assert validation.result.failure is not None
    assert validation.result.failure.code is HarnessFailureCode.OUTCOME_UNVERIFIED


def test_harness_rejects_generic_success_for_specific_canvas_outcome():
    validation = validate_trace(
        request(required_outcomes=["a connected diagram is visible"]), trace(event())
    )
    assert validation.result.status is HarnessStatus.BLOCKED
    assert validation.result.failure is not None
    assert validation.result.failure.code is HarnessFailureCode.OUTCOME_UNVERIFIED


def test_harness_blocks_empty_trace():
    validation = validate_trace(request(), trace())
    assert validation.result.status is HarnessStatus.BLOCKED
    assert validation.result.failure is not None
    assert validation.result.failure.code is HarnessFailureCode.TRACE_INCOMPLETE


def test_harness_persists_standalone_redacted_artifact_contract(tmp_path):
    artifacts = RunArtifacts(tmp_path, "artifact-contract")
    validation = validate_trace(request(), trace(event()))
    persist_validation(artifacts, request(), validation)
    assert (artifacts.root / "interaction-run.json").is_file()
    assert (artifacts.root / "observations" / "screenshots").is_dir()
    assert (artifacts.root / "observations" / "accessibility").is_dir()
    assert (artifacts.root / "actions.jsonl").is_file()
    assert (artifacts.root / "state-transitions.json").is_file()
    assert (artifacts.root / "artifact-manifest.json").is_file()
    assert artifacts.verify_manifest()["valid"] is True


def test_harness_classifies_provider_and_target_failures():
    assert classify_harness_exception(RuntimeError("captcha required"))[0] is HarnessFailureCode.CAPTCHA_BLOCKED
    assert classify_harness_exception(RuntimeError("target is ambiguous"))[0] is HarnessFailureCode.TARGET_AMBIGUOUS
    assert classify_harness_exception(RuntimeError("Page.goto: net::ERR_CONNECTION_REFUSED"))[0] is HarnessFailureCode.URL_LOAD_FAILED
    blocked = validation_for_exception(request(), trace(event()), RuntimeError("verification inconclusive"))
    assert blocked.result.status is HarnessStatus.BLOCKED
    assert blocked.result.failure is not None
    assert blocked.result.failure.code is HarnessFailureCode.OUTCOME_UNVERIFIED


def test_interaction_job_finalizer_skips_presentation_after_verified_trace(tmp_path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request_row = repository.create_request("harness-finalizer", "https://example.com", "Open overview")
    run = repository.create_run(request_row["id"], str(tmp_path))
    job = repository.enqueue_job(run["id"], "interaction", {"harness_mode": "capability"})
    artifacts = RunArtifacts(tmp_path, run["id"])
    artifacts.save_trace(trace(event()))
    service = DemoJobService(repository, tmp_path)

    assert service.finalize_interaction_harness(
        run["id"], {"harness_mode": "capability"}
    ) is True
    assert repository.get_job(job["id"])["status"] == "COMPLETE"
    assert repository.get_run(run["id"])["status"] == "COMPLETE"
    assert all(
        item["status"] == "SKIPPED"
        for item in repository.stage_jobs(run["id"])
        if item["stage"] in {"NARRATION", "RENDER", "VIDEO_QA"}
    )


def test_interaction_job_finalizer_blocks_unverified_trace(tmp_path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request_row = repository.create_request("harness-blocked", "https://example.com", "Open overview")
    run = repository.create_run(request_row["id"], str(tmp_path))
    job = repository.enqueue_job(run["id"], "interaction", {"harness_mode": "capability"})
    artifacts = RunArtifacts(tmp_path, run["id"])
    artifacts.save_trace(trace(event(), verified=False))
    service = DemoJobService(repository, tmp_path)

    assert service.finalize_interaction_harness(
        run["id"], {"harness_mode": "capability"}
    ) is False
    assert repository.get_job(job["id"])["status"] == "FAILED"
    assert repository.get_run(run["id"])["status"] == "FAILED"
    assert (artifacts.root / "harness" / "failure.json").is_file()
