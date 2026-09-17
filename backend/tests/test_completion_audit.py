import hashlib
import json

from app.evaluation.completion_audit import REQUIRED_ARTIFACTS, audit_run


def test_audit_reports_missing_layers_without_inferring_from_video(tmp_path):
    (tmp_path / "final").mkdir()
    (tmp_path / "final" / "demo.mp4").write_bytes(b"video")
    entries = []
    for path in sorted(
        path
        for path in tmp_path.rglob("*")
        if path.is_file() and path.name != "artifact-manifest.json"
    ):
        entries.append(
            {
                "path": path.relative_to(tmp_path).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    (tmp_path / "artifact-manifest.json").write_text(
        json.dumps({"artifacts": entries}), encoding="utf-8"
    )
    report = audit_run(tmp_path)
    assert report["complete_evidence"] is False
    assert "delivery" in report["missing_layers"]


def test_audit_requires_all_layers_and_positive_delivery_declaration(tmp_path):
    for _, relative in REQUIRED_ARTIFACTS:
        path = tmp_path / relative
        if relative == "page-knowledge":
            path.mkdir(parents=True, exist_ok=True)
            (path / "page.json").write_text("{}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"deliverable": true}' if relative.endswith("delivery-report.json") else "{}"
        )
    # The manifest is written last in real runs; test the same immutable
    # integrity boundary after all required evidence is materialised.
    entries = []
    for path in sorted(
        path
        for path in tmp_path.rglob("*")
        if path.is_file() and path.name != "artifact-manifest.json"
    ):
        entries.append(
            {
                "path": path.relative_to(tmp_path).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    (tmp_path / "artifact-manifest.json").write_text(json.dumps({"artifacts": entries}))
    report = audit_run(tmp_path)
    assert report["complete_evidence"] is True
    assert report["missing_layers"] == []


def test_audit_rejects_persisted_qa_failures_even_when_delivery_was_marked_true(tmp_path):
    for _, relative in REQUIRED_ARTIFACTS:
        path = tmp_path / relative
        if relative == "page-knowledge":
            path.mkdir(parents=True, exist_ok=True)
            (path / "page.json").write_text("{}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        content = '{"deliverable": true}' if relative.endswith("delivery-report.json") else "{}"
        if relative == "qa/editorial-report.json":
            content = '{"hard_failures": ["GENERIC_ROUTE_LABEL_CAPTION"]}'
        path.write_text(content)
    entries = []
    for path in sorted(
        path
        for path in tmp_path.rglob("*")
        if path.is_file() and path.name != "artifact-manifest.json"
    ):
        entries.append(
            {
                "path": path.relative_to(tmp_path).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    (tmp_path / "artifact-manifest.json").write_text(json.dumps({"artifacts": entries}))
    report = audit_run(tmp_path)
    assert report["complete_evidence"] is False
    assert "editorial_qa_failed" in report["missing_layers"]


def test_creation_audit_requires_matching_production_outcome_witness(tmp_path):
    (tmp_path / "discovery").mkdir()
    (tmp_path / "execution").mkdir()
    (tmp_path / "objective.json").write_text(
        json.dumps({"permitted_mutations": ["create_isolated_record"]})
    )
    (tmp_path / "discovery" / "rehearsal-report.json").write_text(
        json.dumps({"outcome_target": {"name": "Appointment created"}})
    )
    (tmp_path / "execution" / "trace.json").write_text(
        json.dumps(
            {"events": [{"kind": "Submit", "success": True, "after": {"text": "Save button"}}]}
        )
    )

    report = audit_run(tmp_path)

    assert "production_creation_outcome_proof" in report["missing_layers"]


def test_creation_audit_accepts_rehearsal_url_witness(tmp_path):
    (tmp_path / "discovery").mkdir()
    (tmp_path / "execution").mkdir()
    url = "https://example.test/leads/created-1"
    (tmp_path / "objective.json").write_text(
        json.dumps({"permitted_mutations": ["create_isolated_record"]})
    )
    (tmp_path / "discovery" / "rehearsal-report.json").write_text(
        json.dumps({"outcome_target": {"name": "verified created record", "source_url": url}})
    )
    (tmp_path / "execution" / "trace.json").write_text(
        json.dumps({"events": [{"kind": "Submit", "success": True, "after": {"url": url}}]})
    )
    report = audit_run(tmp_path)
    assert "production_creation_outcome_proof" not in report["missing_layers"]


def test_creation_audit_accepts_dynamic_url_with_matched_form_value(tmp_path):
    (tmp_path / "discovery").mkdir()
    (tmp_path / "execution").mkdir()
    (tmp_path / "objective.json").write_text(
        json.dumps({"permitted_mutations": ["create_isolated_record"]})
    )
    (tmp_path / "discovery" / "rehearsal-report.json").write_text(
        json.dumps({"outcome_target": {"name": "verified created record"}})
    )
    (tmp_path / "execution" / "trace.json").write_text(
        json.dumps(
            {
                "events": [
                    {
                        "kind": "Submit",
                        "success": True,
                        "after": {
                            "url": "https://example.test/leads/server-generated-id",
                            "verified_outcome": {"matched_form_values": ["Demo Contact"]},
                        },
                        "state_delta": {"url_changed": True},
                    }
                ]
            }
        )
    )
    report = audit_run(tmp_path)
    assert "production_creation_outcome_proof" not in report["missing_layers"]


def test_audit_rejects_manifest_that_omits_required_evidence(tmp_path):
    for _, relative in REQUIRED_ARTIFACTS:
        path = tmp_path / relative
        if relative == "page-knowledge":
            path.mkdir(parents=True, exist_ok=True)
            (path / "page.json").write_text("{}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"deliverable": true}' if relative.endswith("delivery-report.json") else "{}"
        )
    # Deliberately register only the video. A hash-valid partial manifest must
    # not satisfy the completion audit.
    video = tmp_path / "final" / "demo.mp4"
    video.write_bytes(b"video")
    (tmp_path / "artifact-manifest.json").write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "path": "final/demo.mp4",
                        "bytes": video.stat().st_size,
                        "sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    report = audit_run(tmp_path)
    assert report["complete_evidence"] is False
    assert "manifest_integrity" in report["missing_layers"]


def test_audit_rejects_stale_selected_candidate_metadata(tmp_path):
    """A candidate summary must describe the same workflow that executes."""
    for _, relative in REQUIRED_ARTIFACTS:
        path = tmp_path / relative
        if relative == "page-knowledge":
            path.mkdir(parents=True, exist_ok=True)
            (path / "page.json").write_text("{}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"deliverable": true}' if relative.endswith("delivery-report.json") else "{}"
        )
    plan = {
        "selected_workflow": "Booking walkthrough",
        "expected_outcomes": ["Booking visible"],
        "workflow_steps": [
            {
                "operation": {
                    "page_url": "https://example.test/bookings/",
                    "evidence_refs": ["page:bookings"],
                }
            }
        ],
        "synthetic_data_plan": {
            "selected_candidate_flow": {
                "name": "Booking walkthrough",
                "page_urls": ["https://example.test/leads"],
                "expected_outcomes": ["Leads visible"],
                "semantic_steps": ["Navigate:Leads"],
                "evidence_coverage": ["page:leads"],
            }
        },
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    entries = []
    for path in sorted(
        path
        for path in tmp_path.rglob("*")
        if path.is_file() and path.name != "artifact-manifest.json"
    ):
        entries.append(
            {
                "path": path.relative_to(tmp_path).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    (tmp_path / "artifact-manifest.json").write_text(
        json.dumps({"artifacts": entries}), encoding="utf-8"
    )
    report = audit_run(tmp_path)
    assert "selected_candidate_artifact_mismatch" in report["missing_layers"]
    assert report["plan_consistency_failures"] == ["SELECTED_CANDIDATE_ARTIFACT_MISMATCH"]
