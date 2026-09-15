from datetime import UTC, datetime
from pathlib import Path

import pytest

from productlens.artifacts.store import RunArtifacts
from productlens.contracts.models import (
    ActionCapability,
    DemoPlan,
    DemoTrace,
    FormField,
    FormSchema,
    InteractionEvent,
    OperationKind,
    ProductContext,
    SemanticOperation,
    Target,
    WorkflowStep,
)
from productlens.persistence.repository import RunRepository
from productlens.providers.errors import ProviderError
from productlens.services.jobs import (
    DemoJobService,
    _form_schemas_for_context,
    _product_knowledge_for_context,
)


def test_form_persistence_prefers_scoped_capability_schemas_over_page_wide_controls():
    capability_schema = FormSchema(
        source_url="https://example.test/leads",
        fields=[
            FormField(
                name="Full name", selector="[name=fullName]", control_type="text", required=True
            )
        ],
    )
    context = ProductContext(
        url="https://example.test/leads",
        title="Leads",
        application_type="web_application",
        elements=[
            # This broad page field must not replace the modal-local schema.
            # It could be a hidden/repeated design-system control in a fresh run.
            {"tag": "input", "name": "element-88", "selector": "input"},
        ],
        capabilities=[
            ActionCapability(
                kind="form",
                purpose="New",
                source_url="https://example.test/leads",
                entry_target=Target(name="New", selector="#new"),
                form_schema=capability_schema,
            ).model_dump(mode="json")
        ],
    )

    schemas = _form_schemas_for_context(context)

    assert schemas == [capability_schema]


def test_product_knowledge_snapshot_is_typed_and_content_fingerprinted():
    context = ProductContext(
        url="https://example.test/",
        title="Example",
        application_type="web_application",
        relevant_routes=["https://example.test/today"],
        page_knowledge=[
            {
                "url": "https://example.test/",
                "title": "Example",
                "purpose": "Overview",
                "fingerprint": "fixture-fingerprint",
            }
        ],
    )
    knowledge = _product_knowledge_for_context(context, project_id="project-1")
    assert knowledge.project_id == "project-1"
    assert len(knowledge.product_fingerprint) == 64
    assert knowledge.routes == context.relevant_routes
    assert knowledge.version == knowledge.product_fingerprint[:16]


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
    request = repository.create_request(
        "cloud-request", "https://example.test", "Inspect dashboard"
    )
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
    assert all(item["status"] == "QUEUED" for item in repository.stage_jobs(run["id"])[1:])


@pytest.mark.asyncio
async def test_direct_stage_validation_failure_is_not_left_running(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("planning-failure", "https://example.test", "Show records")
    run = repository.create_run(request["id"], str(tmp_path))

    class FailingGenerator:
        async def plan_stage(self, **_kwargs):
            raise ValueError("insufficient grounded evidence")

    repository.ensure_stage_jobs(run["id"])
    repository.update_stage_job(run["id"], "DISCOVERY", status="COMPLETE")
    with pytest.raises(ValueError):
        await DemoJobService(repository, tmp_path, url_generator=FailingGenerator()).run_url_stage(
            run["id"],
            "PLANNING",
            payload={
                "allow_external_side_effects": False,
                "audience": "prospect",
                "target_duration_seconds": 60,
            },
        )

    assert repository.stage_job(run["id"], "PLANNING")["status"] == "FAILED"
    assert repository.get_run(run["id"])["status"] == "FAILED"


@pytest.mark.asyncio
async def test_failed_checkpoint_cannot_be_silently_skipped_on_resume(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("failed-checkpoint", "https://example.test", "Show records")
    run = repository.create_run(request["id"], str(tmp_path))
    repository.ensure_stage_jobs(run["id"])
    repository.update_stage_job(run["id"], "DISCOVERY", status="COMPLETE")
    repository.update_stage_job(
        run["id"], "PLANNING", status="FAILED", error_code="EDITORIAL_REJECTED"
    )
    repository.update_run(
        run["id"], stage="PLANNING", status="FAILED", error_code="PLANNING_RUNTIMEERROR"
    )

    with pytest.raises(RuntimeError, match="targeted retry"):
        await DemoJobService(repository, tmp_path, url_generator=object()).run_url_stage(
            run["id"],
            "PLANNING",
            payload={},
        )

    assert repository.stage_job(run["id"], "PLANNING")["status"] == "FAILED"
    assert repository.get_run(run["id"])["status"] == "FAILED"


@pytest.mark.asyncio
async def test_execution_stage_forwards_cloud_production_to_url_generator(tmp_path: Path):
    """The durable worker must not silently downgrade cloud production to local."""
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request(
        "cloud-production", "https://example.test", "Inspect dashboard"
    )
    run = repository.create_run(request["id"], str(tmp_path))
    artifacts = RunArtifacts(tmp_path, run["id"])
    artifacts.write_json("presentation/presentation-plan.json", {})

    class Generator:
        captured: dict | None = None

        async def execute_stage(self, **kwargs):
            self.captured = kwargs
            return DemoTrace(
                run_id=kwargs["run_id"],
                objective="Inspect dashboard",
                started_at=datetime.now(UTC),
                outcome_verified=True,
                events=[],
            )

    generator = Generator()
    repository.ensure_stage_jobs(run["id"])
    for stage in ("DISCOVERY", "PLANNING"):
        repository.update_stage_job(run["id"], stage, status="COMPLETE")
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
        "DISCOVERY",
        "PLANNING",
        "EXECUTION",
        "NARRATION",
        "RENDER",
        "VIDEO_QA",
    ]
    assert stages[0]["status"] == "COMPLETE"
    assert all(item["status"] == "QUEUED" for item in stages[1:])


@pytest.mark.asyncio
async def test_creation_intent_rehearses_before_durable_planning(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request(
        "creation-stage", "https://example.test", "Create an isolated demo record"
    )
    run = repository.create_run(request["id"], str(tmp_path))

    class Generator:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def rehearsal_stage(self, **_kwargs):
            self.calls.append("rehearsal")

        async def plan_stage(self, **_kwargs):
            self.calls.append("planning")
            operation = SemanticOperation(
                kind=OperationKind.NAVIGATE, intent="Open product", value="https://example.test"
            )
            return DemoPlan(
                objective="Create an isolated demo record",
                narrative_goal="demo",
                audience="prospect",
                target_duration_seconds=60,
                selected_workflow="record",
                workflow_steps=[
                    WorkflowStep(id="one", intent=operation.intent, operation=operation)
                ],
                expected_outcomes=["record"],
                viewport_strategy="native",
                stop_conditions=["done"],
            )

    generator = Generator()
    repository.ensure_stage_jobs(run["id"])
    repository.update_stage_job(run["id"], "DISCOVERY", status="COMPLETE")
    await DemoJobService(repository, tmp_path, url_generator=generator).run_url_stage(
        run["id"],
        "PLANNING",
        payload={
            "allow_external_side_effects": False,
            "allow_isolated_record_creation": True,
            "cloud_discovery": False,
            "credential_reference": None,
            "audience": "prospect",
            "target_duration_seconds": 60,
        },
    )
    assert generator.calls == ["rehearsal", "planning"]
