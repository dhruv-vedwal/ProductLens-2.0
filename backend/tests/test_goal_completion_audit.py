import json

from scripts.audit_goal_completion import build_audit


def _touch(root, relative):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")


def test_goal_audit_is_conservative_about_live_acceptance(tmp_path):
    required = [
        "src/productlens/contracts/models.py",
        "src/productlens/persistence/repository.py",
        "alembic/versions",
        "src/productlens/discovery/live.py",
        "src/productlens/services/preflight.py",
        "src/productlens/providers/stagehand.py",
        "src/productlens/execution/engine.py",
        "src/productlens/execution/playwright_adapter.py",
        "src/productlens/execution/state_diff.py",
        "src/productlens/execution/spatial_index.py",
        "src/productlens/planning/production.py",
        "src/productlens/planning/candidates.py",
        "src/productlens/planning/evidence_graph.py",
        "src/productlens/services/generation.py",
        "src/productlens/presentation/director.py",
        "src/productlens/presentation/editorial.py",
        "src/productlens/video/render.py",
        "video/remotion/src/root.tsx",
        "src/productlens/quality/delivery.py",
        "src/productlens/quality/multimodal.py",
        "src/productlens/quality/repair.py",
        "src/productlens/evaluation/completion_audit.py",
        "src/productlens/services/jobs.py",
        "src/productlens/workers/local.py",
        "src/productlens/workers/tasks.py",
        "src/productlens/services/stage_contracts.py",
        "src/productlens/api/main.py",
        "scripts/audit_project_generality.py",
        "scripts/benchmark_concurrency.py",
        "tests/test_advanced_capabilities.py",
        "tests/test_concurrency.py",
    ]
    for relative in required:
        _touch(tmp_path, relative)
    acceptance = tmp_path / "artifacts" / "acceptance" / "public-runs.json"
    acceptance.parent.mkdir(parents=True)
    acceptance.write_text(
        json.dumps(
            {
                "runs": [{"url": "https://example.test", "status": "COMPLETE"}],
                "attempts": [{"url": "https://example.test", "status": "FAILED"}],
            }
        ),
        encoding="utf-8",
    )

    report = build_audit(tmp_path)

    assert report["complete"] is False
    live = next(item for item in report["checks"] if "live acceptance" in item["requirement"])
    assert live["status"] == "in_progress"


def test_goal_audit_handles_invalid_acceptance_manifest(tmp_path):
    path = tmp_path / "artifacts" / "acceptance" / "public-runs.json"
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")

    report = build_audit(tmp_path)

    live = next(item for item in report["checks"] if "live acceptance" in item["requirement"])
    assert live["status"] == "in_progress"
    assert live["live_evidence"]["status"] == "invalid"


def test_goal_audit_requires_a_completed_fresh_sweep_checkpoint(tmp_path):
    path = tmp_path / "artifacts" / "acceptance" / "public-runs.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "url": "https://example.test",
                        "status": "COMPLETE",
                        "deliverable": True,
                        "final_video": "demo.mp4",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (path.parent / "public-sweep-status.json").write_text(
        json.dumps({"status": "RUNNING"}), encoding="utf-8"
    )

    report = build_audit(tmp_path)

    live = next(item for item in report["checks"] if "live acceptance" in item["requirement"])
    assert live["status"] == "in_progress"


def test_goal_audit_rejects_recorded_static_failure(tmp_path):
    path = tmp_path / "artifacts" / "audits" / "static-validation.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"commands": {"pytest_non_integration": {"status": "FAIL"}}}),
        encoding="utf-8",
    )

    report = build_audit(tmp_path)

    static = next(item for item in report["checks"] if "recorded static" in item["requirement"])
    assert static["status"] == "in_progress"
    assert static["validation_evidence"]["failures"]
