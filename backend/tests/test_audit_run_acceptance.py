import json

from scripts.audit_run_acceptance import REQUIRED_ARTIFACTS, audit_run


def _write_json(root, relative, payload):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_run_acceptance_audit_reports_missing_layers_without_calling_providers(tmp_path):
    _write_json(
        tmp_path,
        "qa/delivery-report.json",
        {"deliverable": False, "hard_failures": ["story:missing"]},
    )
    report = audit_run(tmp_path)
    assert report["deliverable"] is False
    assert "objective.json" in report["missing_artifacts"]
    assert "qa/delivery-report.json:story:missing" in report["hard_failures"]


def test_required_artifact_contract_includes_all_independent_qa_layers():
    assert "qa/exploration-report.json" in REQUIRED_ARTIFACTS
    assert "qa/editorial-report.json" in REQUIRED_ARTIFACTS
    assert "qa/synchronization-report.json" in REQUIRED_ARTIFACTS
    assert "presentation/narration-script.json" in REQUIRED_ARTIFACTS
    assert "narration/fact-extraction.json" in REQUIRED_ARTIFACTS
