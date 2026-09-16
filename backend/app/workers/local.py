"""Local durable worker for development without RabbitMQ.

Production uses the Dramatiq actor with RabbitMQ. This worker intentionally polls the
same persisted outbox so a developer can run the complete stack without silently
falling back to FastAPI background tasks.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from time import monotonic
from typing import Any

from app.observability.logging import get_logger
from app.services.runtime import build_job_service
from app.workers.tasks import (
    execute_claimed_generation_job,
    execute_generation_stage,
)

logger = get_logger("app.workers.local")


def worker_concurrency(value: str | None = None) -> int:
    """Return a bounded local worker parallelism setting.

    Keeping the policy in one pure function makes deployment configuration
    testable without starting a worker or claiming real jobs.
    """
    raw = value if value is not None else os.getenv("PRODUCTLENS_WORKER_CONCURRENCY", "1")
    try:
        return max(1, min(100, int(raw)))
    except (TypeError, ValueError):
        return 1


def next_pending_stage(repository: Any, run_id: str) -> str | None:
    """Return the next queued stage for a claimed root job."""
    return next(
        (
            item["stage"]
            for item in repository.stage_jobs(run_id)
            if item["status"] in {"QUEUED", "RETRYING"}
        ),
        None,
    )


async def run_forever(poll_seconds: float = 0.75) -> None:
    repository, jobs = build_job_service()
    lease_seconds = int(os.getenv("PRODUCTLENS_WORKER_LEASE_SECONDS", "1800"))
    recovery_seconds = float(os.getenv("PRODUCTLENS_WORKER_RECOVERY_SECONDS", "60"))
    concurrency = worker_concurrency()
    orphaned = repository.fail_orphaned_jobs()
    repository.recover_stale_jobs(max_running_seconds=lease_seconds)
    logger.info(
        "local_worker_started",
        poll_seconds=poll_seconds,
        lease_seconds=lease_seconds,
        concurrency=concurrency,
        orphaned_jobs_closed=orphaned,
    )
    last_recovery = monotonic()
    running: set[asyncio.Task[None]] = set()

    async def execute_stage(stage: dict[str, Any]) -> None:
        """Run one claimed stage in its own task and preserve error isolation."""
        try:
            await execute_generation_stage(
                stage["run_id"],
                stage["stage"],
                repository=repository,
                jobs=jobs,
                claimed_stage=stage,
            )
        except Exception:
            logger.exception(
                "local_worker_stage_failed",
                run_id=stage["run_id"],
                stage=stage["stage"],
            )

    while True:
        finished = {task for task in running if task.done()}
        for task in finished:
            running.discard(task)
            # Exceptions are logged inside execute_stage.  Retrieving the
            # result prevents asyncio from reporting an unhandled task error.
            if not task.cancelled():
                task.exception()
        if monotonic() - last_recovery >= recovery_seconds:
            orphaned = repository.fail_orphaned_jobs()
            repository.recover_stale_jobs(max_running_seconds=lease_seconds)
            if orphaned:
                logger.warning("local_worker_closed_orphaned_jobs", count=orphaned)
            last_recovery = monotonic()
        if len(running) < concurrency:
            stage = repository.claim_next_stage_job()
            if stage:
                logger.info(
                    "local_worker_stage_claimed", run_id=stage["run_id"], stage=stage["stage"]
                )
                # Keep the compare-and-set lease while the task runs. Requeueing
                # a claimed stage before scheduling created a duplicate-claim
                # window whenever multiple local workers were active.
                running.add(asyncio.create_task(execute_stage(stage)))
                continue
        job = repository.claim_next_job() if len(running) < concurrency else None
        if job:
            logger.info(
                "local_worker_root_job_claimed",
                job_id=job["id"],
                run_id=job["run_id"],
                kind=job["kind"],
            )
            try:
                if job["kind"] == "fixture":
                    # The local mode consumes the same persisted stage queue as
                    # Dramatiq; it never falls back to FastAPI background tasks.
                    continue
                if job["kind"] == "url":
                    # URL jobs use the identical durable stage pipeline as broker
                    # workers.  The root claim must enqueue the first stage;
                    # discarding it leaves production runs stuck in QUEUED.
                    pending = next_pending_stage(repository, job["run_id"])
                    if pending:
                        repository.update_stage_job(job["run_id"], pending, status="QUEUED")
                    continue
                await execute_claimed_generation_job(job, repository=repository, jobs=jobs)
            except Exception:
                logger.exception("local_worker_root_job_failed", run_id=job.get("run_id"))
            continue
        await asyncio.sleep(poll_seconds if running else min(poll_seconds, 0.1))


def main() -> None:
    poll_seconds = float(os.getenv("PRODUCTLENS_WORKER_POLL_SECONDS", "0.75"))
    with suppress(KeyboardInterrupt):
        asyncio.run(run_forever(poll_seconds))


if __name__ == "__main__":
    main()
