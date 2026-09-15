from pathlib import Path

import pytest

from productlens.persistence.repository import RunRepository
from productlens.providers.errors import ProviderError
from productlens.workers.local import next_pending_stage, worker_concurrency
from productlens.workers.selected import run_selected_job
from productlens.workers.tasks import execute_generation_stage


@pytest.mark.asyncio
async def test_fixture_stage_worker_claims_once_and_advances_without_replaying(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("staged-fixture", "fixture://gate-1", "Open dashboard")
    run = repository.create_run(request["id"], str(tmp_path))
    job = repository.enqueue_job(run["id"], "fixture", {"gate": 1, "render": False})
    assert repository.claim_job(job["id"])

    class FakeJobs:
        def __init__(self):
            self.calls: list[str] = []

        async def run_fixture_stage(self, run_id: str, stage: str, *, gate: int, render: bool):
            assert run_id == run["id"] and gate == 1 and not render
            self.calls.append(stage)
            repository.update_stage_job(run_id, stage, status="COMPLETE")

    fake = FakeJobs()
    await execute_generation_stage(run["id"], "DISCOVERY", repository=repository, jobs=fake)
    await execute_generation_stage(run["id"], "DISCOVERY", repository=repository, jobs=fake)

    assert fake.calls == ["DISCOVERY"]
    assert repository.stage_job(run["id"], "DISCOVERY")["delivery_attempts"] == 1
    assert repository.stage_job(run["id"], "PLANNING")["status"] == "QUEUED"


@pytest.mark.asyncio
async def test_url_stage_worker_uses_the_same_durable_claim_boundary(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("staged-url", "https://example.test", "Show dashboard")
    run = repository.create_run(request["id"], str(tmp_path))
    job = repository.enqueue_job(
        run["id"],
        "url",
        {"max_pages": 3, "render": False, "allow_external_side_effects": False},
    )
    assert repository.claim_job(job["id"])

    class FakeJobs:
        def __init__(self):
            self.calls: list[tuple[str, dict]] = []

        async def run_url_stage(self, run_id: str, stage: str, *, payload: dict):
            self.calls.append((stage, payload))
            repository.update_stage_job(run_id, stage, status="COMPLETE")

    fake = FakeJobs()
    await execute_generation_stage(run["id"], "DISCOVERY", repository=repository, jobs=fake)
    assert fake.calls == [
        ("DISCOVERY", {"max_pages": 3, "render": False, "allow_external_side_effects": False})
    ]
    assert repository.stage_job(run["id"], "DISCOVERY")["status"] == "COMPLETE"


@pytest.mark.asyncio
async def test_preclaimed_local_stage_is_executed_without_a_second_claim(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("preclaimed", "fixture://gate-1", "Open dashboard")
    run = repository.create_run(request["id"], str(tmp_path))
    job = repository.enqueue_job(run["id"], "fixture", {"gate": 1, "render": False})
    assert repository.claim_job(job["id"])
    claimed = repository.claim_stage_job(run["id"], "DISCOVERY")
    assert claimed is not None

    original_claim = repository.claim_stage_job

    def fail_if_claimed_again(*_args, **_kwargs):
        raise AssertionError("a preclaimed local stage must not be claimed twice")

    repository.claim_stage_job = fail_if_claimed_again

    class FakeJobs:
        async def run_fixture_stage(self, run_id: str, stage: str, *, gate: int, render: bool):
            assert (run_id, stage, gate, render) == (run["id"], "DISCOVERY", 1, False)
            repository.update_stage_job(run_id, stage, status="COMPLETE")

    try:
        await execute_generation_stage(
            run["id"], "DISCOVERY", repository=repository, jobs=FakeJobs(), claimed_stage=claimed
        )
    finally:
        repository.claim_stage_job = original_claim
    assert repository.stage_job(run["id"], "DISCOVERY")["status"] == "COMPLETE"


def test_local_worker_dispatches_first_stage_for_url_root_job(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("local-url", "https://example.test", "Show dashboard")
    run = repository.create_run(request["id"], str(tmp_path))
    repository.enqueue_job(run["id"], "url", {"render": False})

    assert next_pending_stage(repository, run["id"]) == "DISCOVERY"


def test_local_worker_concurrency_is_bounded_and_safe():
    assert worker_concurrency("1") == 1
    assert worker_concurrency("50") == 50
    assert worker_concurrency("1000") == 100
    assert worker_concurrency("invalid") == 1


@pytest.mark.asyncio
async def test_stage_worker_persists_non_retryable_provider_credit_failure(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    request = repository.create_request("credit", "https://example.test", "Show dashboard")
    run = repository.create_run(request["id"], str(tmp_path))
    job = repository.enqueue_job(run["id"], "url", {"render": False})
    assert repository.claim_job(job["id"])

    class FailingJobs:
        async def run_url_stage(self, run_id: str, stage: str, *, payload: dict):
            raise ProviderError("browserbase", 402, "payment required")

    with pytest.raises(ProviderError):
        await execute_generation_stage(
            run["id"], "DISCOVERY", repository=repository, jobs=FailingJobs()
        )
    assert repository.stage_job(run["id"], "DISCOVERY")["error_code"] == "PROVIDER_CREDIT_REQUIRED"
    assert repository.get_job(job["id"])["error_code"] == "PROVIDER_CREDIT_REQUIRED"


@pytest.mark.asyncio
async def test_selected_worker_executes_only_the_requested_job(tmp_path: Path):
    repository = RunRepository(tmp_path / "productlens.sqlite3")
    first_request = repository.create_request("first", "fixture://one", "First")
    second_request = repository.create_request("second", "fixture://two", "Second")
    first_run = repository.create_run(first_request["id"], str(tmp_path))
    second_run = repository.create_run(second_request["id"], str(tmp_path))
    first_job = repository.enqueue_job(first_run["id"], "fixture", {"gate": 1, "render": False})
    second_job = repository.enqueue_job(second_run["id"], "fixture", {"gate": 1, "render": False})

    class FakeJobs:
        async def run_fixture_stage(self, run_id: str, stage: str, *, gate: int, render: bool):
            repository.update_stage_job(run_id, stage, status="COMPLETE")

    result = await run_selected_job(first_job["id"], repository=repository, jobs=FakeJobs())

    assert result["status"] == "COMPLETE"
    assert repository.get_job(second_job["id"])["status"] == "QUEUED"
    assert all(item["status"] == "QUEUED" for item in repository.stage_jobs(second_run["id"]))
