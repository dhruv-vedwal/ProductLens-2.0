from pathlib import Path

from scripts.classify_artifacts import classify_root, classify_run


def test_retention_classifier_keeps_video_runs(tmp_path: Path):
    run = tmp_path / "runs" / "with-video"
    (run / "final").mkdir(parents=True)
    (run / "final" / "demo.mp4").write_bytes(b"video")
    result = classify_run(run)
    assert result["category"] == "retain_deliverable"


def test_retention_classifier_keeps_trace_only_runs(tmp_path: Path):
    run = tmp_path / "runs" / "with-trace"
    (run / "execution").mkdir(parents=True)
    (run / "execution" / "trace.json").write_text("{}")
    result = classify_run(run)
    assert result["category"] == "retain_evidence"


def test_retention_classifier_marks_empty_directories_for_review(tmp_path: Path):
    run = tmp_path / "runs" / "empty"
    (run / "execution").mkdir(parents=True)
    report = classify_root(tmp_path)
    assert report["dry_run"] is True
    assert report["counts"] == {"review_empty": 1}


def test_retention_classifier_marks_stale_plans_without_deleting_them(tmp_path: Path):
    run = tmp_path / "runs" / "stale"
    (run / "execution").mkdir(parents=True)
    (run / "execution" / "trace.json").write_text("{}")
    (run / "plan.json").write_text(
        '{"selected_workflow":"Booking","expected_outcomes":["Booking"],'
        '"workflow_steps":[{"operation":{"page_url":"https://example.test/bookings"}}],'
        '"synthetic_data_plan":{"selected_candidate_flow":{'
        '"name":"Booking","page_urls":["https://example.test/leads"],'
        '"expected_outcomes":["Leads"],"semantic_steps":["Navigate:Leads"]}}}',
        encoding="utf-8",
    )
    result = classify_run(run)
    assert result["category"] == "review_stale_plan"
    assert result["consistency_failures"] == ["SELECTED_CANDIDATE_ARTIFACT_MISMATCH"]
