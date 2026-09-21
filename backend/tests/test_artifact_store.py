import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.artifacts.store import RunArtifacts, materialize_trace_lifecycle
from app.contracts.models import (
    DemoTrace,
    InteractionEvent,
    OperationKind,
    Postcondition,
    Target,
)
from app.storage.local import LocalArtifactStorage
from app.storage.s3 import S3ArtifactStorage


def test_json_artifacts_are_replaced_atomically_without_leaking_temp_files(tmp_path):
    artifacts = RunArtifacts(tmp_path, "run")
    first = artifacts.write_json("qa/report.json", {"status": "first"})
    second = artifacts.write_json("qa/report.json", {"status": "second"})

    assert first == second
    assert json.loads(second.read_text(encoding="utf-8")) == {"status": "second"}
    assert not list(second.parent.glob("report.json.tmp"))


def test_json_artifact_write_retries_a_transient_windows_replace_lock(tmp_path, monkeypatch):
    artifacts = RunArtifacts(tmp_path, "run")
    original_replace = Path.replace
    attempts = 0

    def locked_once(path, target):
        nonlocal attempts
        if path.suffix == ".tmp" and attempts == 0:
            attempts += 1
            raise PermissionError("transient file lock")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", locked_once)
    monkeypatch.setattr("app.artifacts.store.time.sleep", lambda _: None)

    destination = artifacts.write_json("qa/report.json", {"status": "written"})

    assert attempts == 1
    assert json.loads(destination.read_text(encoding="utf-8")) == {"status": "written"}


def test_object_storage_manifest_is_checksum_backed_and_published_last(tmp_path, monkeypatch):
    run = tmp_path / "runs" / "run-1"
    (run / "qa").mkdir(parents=True)
    (run / "qa" / "report.json").write_text('{"ok":true}', encoding="utf-8")
    published: list[str] = []
    storage = object.__new__(S3ArtifactStorage)
    storage.bucket, storage.prefix = "bucket", "prefix"
    monkeypatch.setattr(
        storage, "put", lambda source, key: published.append(key) or f"s3://bucket/{key}"
    )

    manifest = storage.publish_run(run)

    assert manifest["artifacts"][0]["path"] == "qa/report.json"
    assert published[-1] == "run-1/artifact-manifest.json"
    assert Path(run / "artifact-manifest.json").exists()


def test_republishing_never_includes_a_prior_manifest_in_its_own_checksum_inventory(tmp_path):
    run = tmp_path / "runs" / "run-1"
    (run / "qa").mkdir(parents=True)
    (run / "qa" / "report.json").write_text('{"ok":true}', encoding="utf-8")
    storage = LocalArtifactStorage(tmp_path / "published")

    storage.publish_run(run)
    manifest = storage.publish_run(run)

    assert all(item["path"] != "artifact-manifest.json" for item in manifest["artifacts"])


def test_manifest_verifier_detects_mutation_and_missing_files(tmp_path):
    artifacts = RunArtifacts(tmp_path, "run")
    file = artifacts.write_json("qa/report.json", {"ok": True})
    artifacts.write_manifest()
    assert artifacts.verify_manifest()["valid"] is True
    file.write_text('{"ok":false}', encoding="utf-8")
    report = artifacts.verify_manifest()
    assert report["valid"] is False
    assert report["mutated"] == ["qa/report.json"]


def test_retention_removes_only_a_truly_empty_run_directory(tmp_path):
    empty = RunArtifacts(tmp_path, "empty")
    assert RunArtifacts.remove_empty_run_directory(tmp_path, "empty") is True
    assert not empty.root.exists()

    retained = RunArtifacts(tmp_path, "retained")
    retained.write_json("qa/report.json", {"evidence": True})
    with pytest.raises(ValueError, match="retained evidence"):
        RunArtifacts.remove_empty_run_directory(tmp_path, "retained")
    assert retained.root.exists()


def test_render_retry_copies_required_predecessor_evidence_but_not_a_prior_delivery(tmp_path):
    parent = RunArtifacts(tmp_path, "parent")
    parent.write_json("discovery/product-context.json", {"page": "observed"})
    parent.write_json("plan.json", {"workflow": "verified"})
    parent.write_json("planning/demo-brief.json", {"brief": "grounded"})
    parent.write_json("planning/capability-resolutions.json", [{"capability": "fill"}])
    parent.write_json("planning/validated-state-graph.json", {"states": []})
    parent.write_json("execution/trace.json", {"trace": "verified"})
    parent.write_json("presentation/narration-script.json", {"script": "approved"})
    parent.write_json("quality/journey-report.json", {"journey": "approved"})
    parent.write_json("qa/coverage-report.json", {"coverage": "complete"})
    parent.write_json("qa/editorial-report.json", {"editorial": "approved"})
    (parent.root / "final" / "demo.mp4").write_bytes(b"old delivery")

    RunArtifacts.clone_for_targeted_retry(tmp_path, "parent", "child", start_stage="RENDER")

    child = RunArtifacts(tmp_path, "child")
    assert (child.root / "execution" / "trace.json").exists()
    assert (child.root / "presentation" / "narration-script.json").exists()
    assert (child.root / "quality" / "journey-report.json").exists()
    assert (child.root / "planning" / "demo-brief.json").exists()
    assert (child.root / "planning" / "capability-resolutions.json").exists()
    assert (child.root / "planning" / "validated-state-graph.json").exists()
    assert (child.qa / "coverage-report.json").exists()
    assert (child.qa / "editorial-report.json").exists()
    assert not (child.root / "final" / "demo.mp4").exists()


def test_targeted_retry_rebinds_native_recording_provenance_to_child_run(tmp_path):
    parent = RunArtifacts(tmp_path, "parent")
    recording = parent.root / "execution" / "browser-recording.mp4"
    recording.parent.mkdir(parents=True, exist_ok=True)
    recording.write_bytes(b"native recording")
    parent.write_json(
        "execution/browserbase-recording.json",
        {
            "provider": "browserbase",
            "run_id": "parent",
            "artifact": str(recording),
            "sha256": "stale",
        },
    )
    parent.write_json("execution/trace.json", {"run_id": "parent", "events": []})

    RunArtifacts.clone_for_targeted_retry(tmp_path, "parent", "child", start_stage="RENDER")

    metadata = json.loads(
        (tmp_path / "runs" / "child" / "execution" / "browserbase-recording.json").read_text()
    )
    assert metadata["run_id"] == "child"
    assert metadata["source_run_id"] == "parent"
    assert metadata["inherited_by_run_id"] == "child"
    assert metadata["artifact"] == str(tmp_path / "runs" / "child" / "execution" / "browser-recording.mp4")
    assert metadata["sha256"] != "stale"
    trace = json.loads((tmp_path / "runs" / "child" / "execution" / "trace.json").read_text())
    assert trace["run_id"] == "child"


def test_manifest_excludes_mutable_run_status_checkpoint(tmp_path):
    artifacts = RunArtifacts(tmp_path, "manifest-status")
    artifacts.write_json("objective.json", {"objective": "demo"})
    artifacts.write_json("run-status.json", {"status": "RUNNING"})
    artifacts.write_manifest()
    entries = json.loads((artifacts.root / "artifact-manifest.json").read_text())["artifacts"]
    assert "run-status.json" not in {entry["path"] for entry in entries}


def test_planning_retry_inherits_root_discovery_evidence_required_by_delivery_qa(tmp_path):
    parent = RunArtifacts(tmp_path, "parent")
    parent.write_json("discovery/product-context.json", {"page": "observed"})
    parent.write_json("objective.json", {"raw": "Show the product"})
    parent.write_json("exploration-report.json", {"stop_reason": "grounded"})
    parent.write_json("page-knowledge/home.json", {"url": "https://example.test/"})
    parent.write_json("feature-graph.json", [{"name": "overview"}])
    parent.write_json("candidate-flows.json", [{"name": "overview"}])

    RunArtifacts.clone_for_targeted_retry(tmp_path, "parent", "child", start_stage="PLANNING")

    child = RunArtifacts(tmp_path, "child")
    assert (child.root / "discovery" / "product-context.json").exists()
    assert (child.root / "objective.json").exists()
    assert (child.root / "exploration-report.json").exists()
    assert list((child.root / "page-knowledge").glob("*.json"))
    assert (child.root / "feature-graph.json").exists()
    assert (child.root / "candidate-flows.json").exists()


def test_trace_lifecycle_projection_materializes_verification_boundaries():
    event = InteractionEvent(
        operation_id="op-1",
        kind=OperationKind.CLICK,
        intent="Open the observed panel",
        occurred_at=datetime.now(UTC),
        action_at=datetime.now(UTC),
        target=Target(name="Panel", role="button", selector="#panel"),
        page_url="https://example.test/",
        success=True,
        duration_ms=240,
    )
    trace = DemoTrace(
        run_id="run-1", objective="inspect the panel", started_at=datetime.now(UTC), events=[event]
    )
    enriched = materialize_trace_lifecycle(trace)
    assert len(enriched.state_snapshots) == 2
    assert len(enriched.action_attempts) == 1
    assert enriched.verification_results[0].status == "passed"


def test_trace_lifecycle_preserves_authorized_submit_witness():
    event = InteractionEvent(
        operation_id="submit-1",
        kind=OperationKind.SUBMIT,
        intent="Submit the form",
        target=Target(name="Save", role="button", selector="#save"),
        postconditions=[
            Postcondition(
                kind="visible", expected=True, target=Target(name="Created result", role="status")
            )
        ],
        page_url="https://example.test/",
        success=True,
        duration_ms=300,
    )
    trace = DemoTrace(
        run_id="run-submit",
        objective="create a safe record",
        started_at=datetime.now(UTC),
        events=[event],
    )
    enriched = materialize_trace_lifecycle(trace)
    assert len(enriched.action_attempts) == 1
    assert enriched.action_attempts[0].intent.gesture == "submit"
    assert enriched.action_attempts[0].intent.side_effect_policy == "authorized_mutation"
    assert enriched.action_attempts[0].intent.expected_state[0].target.name == "Created result"
