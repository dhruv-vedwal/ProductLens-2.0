from productlens.quality.delivery import delivery_report


def test_delivery_requires_all_artifacts_and_quality():
    report = delivery_report(
        artifacts={"trace": True, "final_video": False}, execution={}, story={}, video={}
    )
    assert not report["deliverable"]
    assert "final_video" in report["hard_failures"]


def test_required_delivery_artifacts_include_presentation_qa(tmp_path):
    from productlens.artifacts.store import RunArtifacts

    required = RunArtifacts(tmp_path, "qa").required_delivery_artifacts()
    assert "presentation_qa" in required
    assert not required["presentation_qa"]


def test_live_url_delivery_requires_discovery_and_editorial_lineage(tmp_path):
    from productlens.artifacts.store import RunArtifacts

    required = RunArtifacts(tmp_path, "url-run").required_url_delivery_artifacts()

    assert {
        "objective",
        "exploration_report",
        "page_knowledge",
        "storyboard",
        "coverage_qa",
    } <= set(required)
    assert not all(required.values())


def test_isolated_creation_delivery_requires_rehearsal_outcome_artifact(tmp_path):
    from productlens.artifacts.store import RunArtifacts

    artifacts = RunArtifacts(tmp_path, "creation-run")
    artifacts.write_json("objective.json", {"permitted_mutations": ["create_isolated_record"]})
    assert artifacts.required_url_delivery_artifacts()["rehearsal_outcome"] is False
    artifacts.write_json("discovery/rehearsal-report.json", {"outcome_target": {"name": "Created"}})
    assert artifacts.required_url_delivery_artifacts()["rehearsal_outcome"] is True


def test_browserbase_native_recording_satisfies_live_trace_evidence(tmp_path):
    from productlens.artifacts.store import RunArtifacts

    artifacts = RunArtifacts(tmp_path, "cloud-run")
    artifacts.execution.joinpath("browser-recording.mp4").write_bytes(b"native")
    artifacts.execution.joinpath("browserbase-recording.json").write_text(
        '{"provider":"browserbase","session_id":"session-1"}', encoding="utf-8"
    )
    required = artifacts.required_url_delivery_artifacts()
    assert required["browser_video"]
    assert required["playwright_trace"]


def test_delivery_blocks_a_failed_page_completion_journey():
    report = delivery_report(
        artifacts={"trace": True, "final_video": True},
        execution={"execution_score": 1.0},
        story={"story_score": 0.0, "hard_failures": ["JOURNEY_INCOMPLETE_PAGE_CONTRACT"]},
        video={"visual_score": 1.0},
    )
    assert not report["deliverable"]
    assert "JOURNEY_INCOMPLETE_PAGE_CONTRACT" in report["hard_failures"]
