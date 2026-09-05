from datetime import UTC, datetime
from pathlib import Path

import pytest

from productlens.artifacts.store import RunArtifacts
from productlens.contracts.models import DemoTrace, InteractionEvent, OperationKind
from productlens.persistence.repository import RunRepository
from productlens.providers.errors import ProviderError
from productlens.services.jobs import DemoJobService


def test_delivery_publication_persists_the_manifest_location(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("publish-request", "fixture://gate-1", "Inspect")
    run = repository.create_run(request["id"], str(tmp_path))

    class Storage:
        def publish_run(self, root):
            assert root.name == run["id"]
            return {"manifest_location": "s3://bucket/runs/manifest.json"}

    service = DemoJobService(repository, tmp_path, artifact_storage=Storage())
    service._publish_delivery(run["id"], RunArtifacts(tmp_path, run["id"]))
    assert repository.locations(run["id"])[0]["kind"] == "artifact_manifest"


@pytest.mark.asyncio
async def test_fixture_job_persists_trace_and_artifact_locations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("request-job", "fixture://gate-2", "Create a lead")
    run = repository.create_run(request["id"], str(tmp_path))

    async def fake_gate(
        gate: int, root: Path, *, render_final: bool, run_id: str, speech_provider=None
    ):
        run_root = root / "runs" / run_id
        (run_root / "presentation").mkdir(parents=True)
        (run_root / "qa").mkdir(parents=True)
        (run_root / "execution").mkdir(parents=True)
        (run_root / "presentation" / "presentation-plan.json").write_text("{}")
        (run_root / "qa" / "story-report.json").write_text("{}")
        (run_root / "execution" / "trace.json").write_text("{}")
        return DemoTrace(
            run_id=run_id,
            objective="Create a lead",
            started_at=datetime.now(UTC),
            outcome_verified=True,
            events=[
                InteractionEvent(
                    operation_id="op",
                    kind=OperationKind.CLICK,
                    intent="Submit",
                    before={},
                    after={},
                    success=True,
                    duration_ms=1,
                )
            ],
        )

    monkeypatch.setattr("productlens.services.jobs.run_fixture_gate", fake_gate)
    await DemoJobService(repository, tmp_path).run_fixture(run["id"], 2, render=False)
    assert repository.get_run(run["id"])["status"] == "COMPLETE"
    assert (
        repository.connection.execute(
            "SELECT COUNT(*) AS count FROM interaction_events"
        ).fetchone()["count"]
        == 1
    )
    assert (
        repository.connection.execute(
            "SELECT COUNT(*) AS count FROM presentation_plans"
        ).fetchone()["count"]
        == 1
    )


@pytest.mark.asyncio
async def test_failed_cloud_generation_retains_browserbase_session_evidence(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("cloud-request", "https://example.test", "Inspect dashboard")
    run = repository.create_run(request["id"], str(tmp_path))

    class FailingGenerator:
        async def discover_stage(self, **kwargs):
            path = tmp_path / "runs" / kwargs["run_id"] / "discovery"
            path.mkdir(parents=True)
            (path / "browserbase-session.json").write_text(
                '{"provider":"browserbase","session_id":"cloud-1"}'
            )
            raise ProviderError("browserbase", 503, "temporary outage")

    with pytest.raises(ProviderError):
        await DemoJobService(repository, tmp_path, url_generator=FailingGenerator()).run_url(
            run["id"],
            allow_external_side_effects=False,
            render=False,
            cloud_discovery=True,
        )
    session = repository.connection.execute(
        "SELECT provider, external_session_id, status FROM browser_sessions"
    ).fetchone()
    assert dict(session) == {
        "provider": "browserbase",
        "external_session_id": "cloud-1",
        "status": "FAILED",
    }
    assert repository.stage_job(run["id"], "DISCOVERY")["status"] == "FAILED"
    assert all(
        item["status"] == "QUEUED"
        for item in repository.stage_jobs(run["id"])[1:]
    )


@pytest.mark.asyncio
async def test_execution_stage_forwards_cloud_production_to_url_generator(tmp_path: Path):
    """The durable worker must not silently downgrade cloud production to local."""
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("cloud-production", "https://example.test", "Inspect dashboard")
    run = repository.create_run(request["id"], str(tmp_path))
    artifacts = RunArtifacts(tmp_path, run["id"])
    artifacts.write_json("presentation/presentation-plan.json", {})

    class Generator:
        captured: dict | None = None

        async def execute_stage(self, **kwargs):
            self.captured = kwargs
            return DemoTrace(
                run_id=kwargs["run_id"], objective="Inspect dashboard", started_at=datetime.now(UTC),
                outcome_verified=True,
                events=[],
            )

    generator = Generator()
    await DemoJobService(repository, tmp_path, url_generator=generator).run_url_stage(
        run["id"],
        "EXECUTION",
        payload={"cloud_production": True, "credential_reference": None},
    )
    assert generator.captured is not None
    assert generator.captured["cloud_production"] is True
    assert repository.stage_job(run["id"], "EXECUTION")["status"] == "COMPLETE"


@pytest.mark.asyncio
async def test_direct_url_stage_provisions_the_same_durable_checkpoint_ledger(tmp_path: Path):
    """Supervised CLI/service runs must not bypass API-created stage rows."""
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("direct-stage", "https://example.test", "Inspect dashboard")
    run = repository.create_run(request["id"], str(tmp_path))

    class Generator:
        async def discover_stage(self, **kwargs):
            class Context:
                def __init__(self, url: str):
                    self.confidence = 1.0
                    self.page_knowledge = []
                    self.elements = []
                    self.url = url

                def model_dump(self, **_kwargs):
                    return {"url": self.url, "page_knowledge": []}

            return Context(kwargs["url"])

    await DemoJobService(repository, tmp_path, url_generator=Generator()).run_url_stage(
        run["id"],
        "DISCOVERY",
        payload={"max_pages": 1, "cloud_discovery": False, "stagehand_assist": False},
    )
    stages = repository.stage_jobs(run["id"])
    assert [item["stage"] for item in stages] == [
        "DISCOVERY", "PLANNING", "EXECUTION", "NARRATION", "RENDER", "VIDEO_QA"
    ]
    assert stages[0]["status"] == "COMPLETE"
    assert all(item["status"] == "QUEUED" for item in stages[1:])
