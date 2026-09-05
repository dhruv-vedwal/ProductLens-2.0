from __future__ import annotations

import asyncio
import os
from typing import Any

import dramatiq

from productlens.providers.errors import ProviderError
from productlens.services.runtime import build_job_service
from productlens.workers.broker import broker

STAGES = ("DISCOVERY", "PLANNING", "EXECUTION", "NARRATION", "RENDER", "VIDEO_QA")


async def activate_generation_job(job_id: str) -> dict[str, Any] | None:
    """Claim the durable root job; individual stages do the provider work."""
    repository, _ = build_job_service()
    return repository.claim_job(job_id)


async def execute_generation_job(job_id: str) -> None:
    repository, jobs = build_job_service()
    repository.recover_stale_jobs(
        max_running_seconds=int(os.getenv("PRODUCTLENS_WORKER_LEASE_SECONDS", "1800"))
    )
    job = repository.claim_job(job_id)
    if job is None:
        return
    if job["kind"] in {"fixture", "url"}:
        next_stage = next(
            (item["stage"] for item in repository.stage_jobs(job["run_id"]) if item["status"] in {"QUEUED", "RETRYING"}),
            None,
        )
        if next_stage is None:
            repository.finish_job(job["id"], status="COMPLETE")
            return
        process_generation_stage.send(job["run_id"], next_stage)
        return
    await execute_claimed_generation_job(job, repository=repository, jobs=jobs)


async def execute_claimed_generation_job(
    job: dict[str, Any], *, repository, jobs
) -> None:
    """Fallback for an unknown job kind; supported jobs use durable stages."""
    if job["kind"] == "fixture":
        return
    try:
        raise ValueError(f"unknown generation job kind: {job['kind']}")
    except Exception as error:
        repository.fail_active_stage_jobs(job["run_id"], type(error).__name__)
        repository.finish_job(job["id"], status="FAILED", error_code=type(error).__name__)
        raise
    else:
        repository.finish_job(job["id"], status="COMPLETE")


def _finish_if_terminal(repository, run_id: str) -> bool:
    stages = repository.stage_jobs(run_id)
    if not stages or any(item["status"] not in {"COMPLETE", "SKIPPED"} for item in stages):
        return False
    job = repository.job_for_run(run_id)
    if job and job["status"] == "RUNNING":
        repository.finish_job(job["id"], status="COMPLETE")
    return True


async def execute_generation_stage(
    run_id: str,
    stage: str,
    *,
    repository=None,
    jobs=None,
    schedule_next: bool = True,
) -> None:
    """Claim and execute one fixture stage exactly once.

    The compare-and-set claim in the repository makes duplicate broker messages
    harmless and prevents an out-of-order delivery from executing provider work.
    """
    if stage not in STAGES:
        raise ValueError(f"unknown generation stage: {stage}")
    if repository is None or jobs is None:
        repository, jobs = build_job_service()
    repository.recover_stale_jobs(
        max_running_seconds=int(os.getenv("PRODUCTLENS_WORKER_LEASE_SECONDS", "1800"))
    )
    claimed = repository.claim_stage_job(run_id, stage)
    if claimed is None:
        return
    job = repository.job_for_run(run_id)
    if job is None:
        repository.update_stage_job(run_id, stage, status="FAILED", error_code="ROOT_JOB_MISSING")
        return
    try:
        if job["kind"] == "fixture":
            await jobs.run_fixture_stage(
                run_id, stage, gate=int(job["payload"]["gate"]), render=bool(job["payload"]["render"])
            )
        elif job["kind"] == "url":
            await jobs.run_url_stage(run_id, stage, payload=job["payload"])
        else:
            raise ValueError(f"unknown generation job kind: {job['kind']}")
    except Exception as error:
        code = error.failure_code if isinstance(error, ProviderError) else type(error).__name__
        repository.update_stage_job(run_id, stage, status="FAILED", error_code=code)
        repository.update_run(run_id, stage="FAILED", status="FAILED", error_code=code)
        repository.finish_job(job["id"], status="FAILED", error_code=code)
        raise
    if _finish_if_terminal(repository, run_id):
        return
    next_index = STAGES.index(stage) + 1
    if schedule_next and next_index < len(STAGES):
        process_generation_stage.send(run_id, STAGES[next_index])


@dramatiq.actor(broker=broker, queue_name="generation", max_retries=0)
def process_generation_job(job_id: str) -> None:
    asyncio.run(execute_generation_job(job_id))


@dramatiq.actor(broker=broker, queue_name="generation-stage", max_retries=0)
def process_generation_stage(run_id: str, stage: str) -> None:
    asyncio.run(execute_generation_stage(run_id, stage))
