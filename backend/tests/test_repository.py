import hashlib
import json
from pathlib import Path

import pytest

from productlens.persistence.repository import RunRepository


def test_request_is_idempotent_and_knowledge_versions(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    first = repository.create_request("request-1", "https://example.test", "Create a lead")
    second = repository.create_request("request-1", "https://ignored.test", "Ignored")
    assert first["id"] == second["id"]
    run = repository.create_run(first["id"], "runs/test")
    assert (
        repository.update_run(run["id"], stage="EXECUTING", status="RUNNING")["stage"]
        == "EXECUTING"
    )
    repository.upsert_knowledge("example.test", {"routes": ["/leads"]}, 0.8)
    repository.upsert_knowledge("example.test", {"routes": ["/leads", "/bookings"]}, 0.9)
    assert (
        repository.connection.execute("SELECT version FROM product_knowledge").fetchone()["version"]
        == 2
    )


def test_repository_persists_run_evidence(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("request-evidence", "fixture://gate-2", "Create a lead")
    run = repository.create_run(request["id"], "runs/test")
    repository.save_workflow_steps(run["id"], [{"intent": "Create a lead"}])
    repository.save_interaction_events(run["id"], [{"intent": "Submit lead"}])
    repository.save_location(run["id"], "final_video", "runs/test/final/demo.mp4")
    assert (
        repository.connection.execute("SELECT COUNT(*) AS count FROM workflow_steps").fetchone()[
            "count"
        ]
        == 1
    )
    assert (
        repository.connection.execute("SELECT COUNT(*) AS count FROM interaction_events").fetchone()[
            "count"
        ]
        == 1
    )
    assert (
        repository.connection.execute("SELECT COUNT(*) AS count FROM artifacts").fetchone()["count"]
        == 1
    )


def test_generic_run_document_ledger_mirrors_architectural_artifacts(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("document-request", "https://example.test", "Show product")
    run = repository.create_run(request["id"], str(tmp_path))
    root = tmp_path / "runs" / run["id"]
    evidence = {
        "objective.json": {"demo_type": "full_walkthrough"},
        "exploration-report.json": {"stop_reason": "grounded"},
        "feature-graph.json": [{"name": "Timeline"}],
        "candidate-flows.json": [{"score": 0.9}],
        "plan.json": {"workflow_steps": []},
        "presentation/validated-scene-plan.json": {"scenes": []},
        "execution/trace.json": {"events": []},
        "qa/repair-decision.json": {"retry_from_stage": "NARRATION"},
        "artifact-manifest.json": {"artifacts": []},
    }
    for relative, payload in evidence.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    persisted = repository.persist_run_documents(run["id"], root)
    documents = {item["kind"]: item for item in repository.run_artifact_documents(run["id"])}
    assert set(persisted) == set(documents)
    assert documents["objective"]["payload"] == evidence["objective.json"]
    assert documents["demo_trace"]["source_path"] == "execution/trace.json"
    assert documents["demo_trace"]["sha256"] == hashlib.sha256(
        (root / "execution/trace.json").read_bytes()
    ).hexdigest()

    (root / "objective.json").write_text(json.dumps({"demo_type": "feature"}), encoding="utf-8")
    repository.persist_run_documents(run["id"], root)
    refreshed = {item["kind"]: item for item in repository.run_artifact_documents(run["id"])}
    assert len(refreshed) == len(evidence)
    assert refreshed["objective"]["payload"] == {"demo_type": "feature"}


def test_idempotent_run_does_not_duplicate_a_nonfailed_request(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request(
        "request-idempotent", "https://example.test", "Show product"
    )
    first, created = repository.create_idempotent_run(request["id"], "runs")
    second, repeated = repository.create_idempotent_run(request["id"], "runs")
    assert created and not repeated
    assert first["id"] == second["id"]


def test_attempts_are_ordered_and_classify_failures(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("attempt-request", "https://example.test", "Test")
    run = repository.create_run(request["id"], "runs")
    first = repository.start_attempt(run["id"], "DISCOVERING")
    repository.finish_attempt(run["id"], first, status="FAILED", failure_code="TIMEOUT")
    assert repository.start_attempt(run["id"], "DISCOVERING") == 2


def test_provider_configuration_stores_reference_not_secret(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    repository.upsert_provider_config(
        provider_type="tts",
        name="elevenlabs",
        credential_reference="secret://providers/elevenlabs",
        active=True,
        settings={"voice_id": "voice-1"},
    )
    assert (
        repository.provider_configs()[0]["credential_reference"] == "secret://providers/elevenlabs"
    )
    with pytest.raises(ValueError):
        repository.upsert_provider_config(
            provider_type="tts", name="bad", credential_reference="sk-not-a-reference", active=True
        )


def test_explicit_retry_creates_new_run_and_retains_lineage(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("retry-request", "fixture://gate-1", "Open dashboard")
    original = repository.create_run(request["id"], str(tmp_path))
    repository.update_run(original["id"], stage="FAILED", status="FAILED", error_code="TIMEOUT")
    retry = repository.create_retry_run(original["id"], str(tmp_path))
    assert retry["id"] != original["id"]
    assert retry["status"] == "QUEUED"
    assert repository.run_details(retry["id"])["retry_of"] == original["id"]
    assert repository.run_details(original["id"])["retries"] == [retry["id"]]


def test_targeted_retry_inherits_only_predecessor_evidence(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("targeted-retry", "https://example.test", "Show a workflow")
    parent = repository.create_run(request["id"], str(tmp_path))
    repository.enqueue_job(parent["id"], "url", {"render": True})
    repository.save_json_artifact("demo_plans", parent["id"], {"objective": "Show a workflow"})
    repository.save_interaction_events(parent["id"], [{"intent": "Open dashboard"}])
    repository.update_run(parent["id"], stage="FAILED", status="FAILED", error_code="PRESENTATION_FAILURE")
    retry = repository.create_retry_run(parent["id"], str(tmp_path))
    repository.enqueue_job(retry["id"], "url", {"render": True})
    repository.prepare_targeted_retry(retry["id"], "RENDER")
    repository.copy_run_evidence(parent["id"], retry["id"], through_stage="RENDER")

    assert [item["status"] for item in repository.stage_jobs(retry["id"])] == [
        "COMPLETE", "COMPLETE", "COMPLETE", "COMPLETE", "QUEUED", "QUEUED"
    ]
    details = repository.run_details(retry["id"])
    assert details["plans"] == [{"objective": "Show a workflow"}]
    assert details["interaction_events"] == [{"intent": "Open dashboard"}]


def test_durable_job_is_persisted_and_can_only_be_claimed_once(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("job-request", "fixture://gate-1", "Open dashboard")
    run = repository.create_run(request["id"], str(tmp_path))
    job = repository.enqueue_job(run["id"], "fixture", {"gate": 1, "render": False})
    claimed = repository.claim_job(job["id"])
    assert claimed and claimed["payload"] == {"gate": 1, "render": False}
    assert repository.claim_job(job["id"]) is None
    repository.finish_job(job["id"], status="COMPLETE")
    assert repository.get_job(job["id"])["status"] == "COMPLETE"


def test_expired_worker_lease_requeues_only_the_running_stage(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("lease-request", "fixture://gate-1", "Open dashboard")
    run = repository.create_run(request["id"], str(tmp_path))
    job = repository.enqueue_job(run["id"], "fixture", {"gate": 1, "render": False})
    assert repository.claim_job(job["id"])
    assert repository.claim_stage_job(run["id"], "DISCOVERY")
    repository.connection.execute(
        "UPDATE generation_jobs SET claimed_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00", job["id"])
    )
    repository.connection.commit()

    assert repository.recover_stale_jobs(max_running_seconds=1) == 1
    assert repository.get_job(job["id"])["status"] == "RETRYING"
    assert repository.stage_job(run["id"], "DISCOVERY")["status"] == "QUEUED"
    assert repository.stage_job(run["id"], "PLANNING")["status"] == "QUEUED"


def test_stage_claim_is_ordered_idempotent_and_can_resume(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("stage-request", "fixture://gate-1", "Open dashboard")
    run = repository.create_run(request["id"], str(tmp_path))
    job = repository.enqueue_job(run["id"], "fixture", {"gate": 1, "render": False})
    assert repository.claim_job(job["id"])

    # A broker cannot race ahead of discovery, and a duplicate delivery is a no-op.
    assert repository.claim_stage_job(run["id"], "PLANNING") is None
    discovery = repository.claim_stage_job(run["id"], "DISCOVERY")
    assert discovery and discovery["status"] == "RUNNING"
    assert repository.claim_stage_job(run["id"], "DISCOVERY") is None

    repository.update_stage_job(run["id"], "DISCOVERY", status="COMPLETE")
    planning = repository.claim_next_stage_job()
    assert planning and planning["stage"] == "PLANNING"
    repository.update_stage_job(run["id"], "PLANNING", status="COMPLETE")
    assert repository.claim_next_stage_job()["stage"] == "EXECUTION"


def test_requests_are_scoped_to_a_studio_project(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    project = repository.create_project("Launch demos")
    request = repository.create_request(
        "project-request", "https://example.test", "Show the launch workflow", project["id"]
    )
    assert request["project_id"] == project["id"]
    assert repository.list_projects()[0]["request_count"] == 1


def test_form_and_synthetic_evidence_are_retained_with_the_run(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("evidence-request", "https://example.test", "Create lead")
    run = repository.create_run(request["id"], str(tmp_path))
    repository.save_form_schema(run["id"], {"fields": [{"name": "Email"}]})
    repository.save_synthetic_dataset(run["id"], {"generated_values": {"Email": "ada@example.test"}})
    details = repository.run_details(run["id"])
    assert details["form_schemas"] == [{"fields": [{"name": "Email"}]}]
    assert details["synthetic_datasets"][0]["generated_values"]["Email"] == "ada@example.test"


def test_delivery_assets_and_provider_telemetry_are_retained_without_secrets(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("delivery-request", "https://example.test", "Create lead")
    run = repository.create_run(request["id"], str(tmp_path))
    repository.save_audio_asset(run["id"], "runs/audio.mp3", duration_seconds=12.4, provider="elevenlabs")
    repository.save_video_render(run["id"], "runs/demo.mp4", status="COMPLETE", metadata={"overall_score": 1})
    repository.record_provider_call(
        run_id=run["id"], provider="playwright", operation="execution", status="COMPLETE", duration_ms=50
    )
    details = repository.run_details(run["id"])
    assert details["audio_assets"][0]["provider"] == "elevenlabs"
    assert details["video_renders"][0]["metadata"]["overall_score"] == 1
    assert details["provider_calls"] == [
        {"provider": "playwright", "operation": "execution", "status": "COMPLETE", "duration_ms": 50, "error_code": None, "created_at": details["provider_calls"][0]["created_at"]}
    ]


def test_fresh_knowledge_is_versioned_and_retrievable(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    repository.upsert_knowledge(
        "https://example.test", {"relevant_routes": ["https://example.test/leads"]}, 0.9
    )
    cached = repository.fresh_knowledge("https://example.test")
    assert cached and cached["evidence"]["relevant_routes"] == ["https://example.test/leads"]


def test_page_knowledge_is_persisted_with_its_product_version(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    repository.upsert_knowledge("https://example.test", {"title": "Example"}, 0.8)
    repository.upsert_page_knowledge("https://example.test", [{"url": "https://example.test/timeline", "purpose": "Career"}], confidence=0.8)
    row = repository.connection.execute("SELECT url, evidence_json FROM page_knowledge").fetchone()
    assert row["url"] == "https://example.test/timeline"
    assert __import__("json").loads(row["evidence_json"])["purpose"] == "Career"


def test_successful_action_knowledge_is_non_secret_and_survives_discovery_refresh(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    repository.upsert_knowledge("https://example.test", {"relevant_routes": ["https://example.test/leads"]}, 0.9)
    repository.record_successful_actions("https://example.test", [{
        "kind": "Click", "intent": "Open lead", "success": True,
        "target": {"name": "Leads", "selector": "#leads"}, "value": "must-not-be-cached",
    }])
    repository.upsert_knowledge("https://example.test", {"relevant_routes": ["https://example.test/leads"]}, 0.9)
    cached = repository.fresh_knowledge("https://example.test")
    action = cached["evidence"]["successful_actions"][0]
    assert action["target"] == {"name": "Leads", "selector": "#leads"}
    assert "value" not in action


def test_run_details_and_list_runs_are_frontend_safe(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("request-detail", "https://example.test", "Create a lead")
    run = repository.create_run(request["id"], "runs/test")
    repository.save_workflow_steps(run["id"], [{"intent": "Create lead"}])
    repository.save_interaction_events(run["id"], [{"operation": "FILL"}])
    repository.save_location(run["id"], "final_video", "runs/test/final/demo.mp4")

    assert repository.list_runs()[0]["id"] == run["id"]
    details = repository.run_details(run["id"])
    assert details["request"]["objective"] == "Create a lead"
    assert details["workflow_steps"] == [{"intent": "Create lead"}]
    assert details["artifacts"][0]["kind"] == "final_video"


def test_cancel_prevents_any_later_stage_claim_and_keeps_audit_record(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("cancel-run", "https://example.test", "Show product")
    run = repository.create_run(request["id"], str(tmp_path))
    job = repository.enqueue_job(run["id"], "url", {"render": True})
    assert repository.claim_job(job["id"])

    cancelled = repository.cancel_run(run["id"])
    assert cancelled["status"] == "CANCELLED"
    assert repository.get_job(job["id"])["status"] == "CANCELLED"
    assert {stage["status"] for stage in repository.stage_jobs(run["id"])} == {"CANCELLED"}
    assert repository.claim_stage_job(run["id"], "DISCOVERY") is None


def test_local_claim_skips_a_root_job_after_its_run_becomes_terminal(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("terminal-root", "https://example.test", "Show product")
    run = repository.create_run(request["id"], str(tmp_path))
    job = repository.enqueue_job(run["id"], "url", {"render": False})
    repository.update_run(run["id"], stage="FAILED", status="FAILED")

    assert repository.claim_next_job() is None
    assert repository.get_job(job["id"])["status"] == "QUEUED"


def test_resume_only_requeues_recoverable_root_job(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("resume-run", "https://example.test", "Show product")
    run = repository.create_run(request["id"], str(tmp_path))
    job = repository.enqueue_job(run["id"], "url", {"render": True})
    repository.connection.execute("UPDATE generation_jobs SET status='RETRYING' WHERE id=?", (job["id"],))
    repository.update_run(run["id"], stage="RETRYING", status="RETRYING")
    resumed = repository.resume_run(run["id"])
    assert resumed["status"] == "QUEUED"
    assert repository.get_run(run["id"])["status"] == "QUEUED"
    repository.update_run(run["id"], stage="FAILED", status="FAILED")
    import pytest
    with pytest.raises(ValueError, match="queued or recoverable"):
        repository.resume_run(run["id"])


def test_retention_deletes_only_empty_terminal_runs_and_invalidation_removes_pages(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("retention-empty", "https://example.test", "Show product")
    empty = repository.create_run(request["id"], str(tmp_path))
    repository.update_run(empty["id"], stage="FAILED", status="FAILED")
    retained = repository.create_run(request["id"], str(tmp_path))
    repository.update_run(retained["id"], stage="FAILED", status="FAILED")
    repository.save_location(retained["id"], "trace", "runs/trace.json")
    assert repository.purge_empty_terminal_runs() == [empty["id"]]
    with __import__("pytest").raises(KeyError):
        repository.get_run(empty["id"])
    assert repository.get_run(retained["id"])["id"] == retained["id"]

    repository.upsert_knowledge("https://example.test", {"title": "Example"}, 0.9)
    repository.upsert_page_knowledge("https://example.test", [{"url": "https://example.test/today"}], confidence=0.9)
    assert repository.invalidate_knowledge("https://example.test") is True
    assert repository.fresh_knowledge("https://example.test") is None
    assert repository.connection.execute("SELECT 1 FROM page_knowledge").fetchone() is None
