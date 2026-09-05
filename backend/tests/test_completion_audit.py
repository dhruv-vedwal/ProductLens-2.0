import hashlib
import json

from productlens.evaluation.completion_audit import audit_run


def test_audit_reports_missing_layers_without_inferring_from_video(tmp_path):
    (tmp_path / "final").mkdir()
    (tmp_path / "final" / "demo.mp4").write_bytes(b"video")
    entries = []
    for path in sorted(path for path in tmp_path.rglob("*") if path.is_file() and path.name != "artifact-manifest.json"):
        entries.append({"path": path.relative_to(tmp_path).as_posix(), "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (tmp_path / "artifact-manifest.json").write_text(json.dumps({"artifacts": entries}), encoding="utf-8")
    report = audit_run(tmp_path)
    assert report["complete_evidence"] is False
    assert "delivery" in report["missing_layers"]


def test_audit_requires_all_layers_and_positive_delivery_declaration(tmp_path):
    for relative in (
        "objective.json", "discovery/product-context.json", "plan.json", "execution/trace.json",
        "presentation/presentation-plan.json", "presentation/narration-script.json",
        "qa/execution-report.json", "qa/story-report.json", "qa/video-report.json",
        "qa/synchronization-report.json", "qa/delivery-report.json", "artifact-manifest.json",
        "final/demo.mp4",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"deliverable": true}' if relative.endswith("delivery-report.json") else "{}")
    (tmp_path / "page-knowledge").mkdir()
    report = audit_run(tmp_path)
    assert report["complete_evidence"] is True
    assert report["missing_layers"] == []
