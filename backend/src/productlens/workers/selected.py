"""Run one explicitly selected durable generation job.

This is intentionally separate from the polling development worker.  Operators
can validate a single run without allowing historical queued records to consume
browser or model providers.  It shares the same database claims and stage
handlers as Dramatiq, so it is not a second execution implementation.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from productlens.services.runtime import build_job_service
from productlens.workers.tasks import STAGES, _finish_if_terminal, execute_generation_stage


def _next_pending_stage(repository: Any, run_id: str) -> str | None:
    """Return this run's next durable stage without inspecting any other run."""
    for record in repository.stage_jobs(run_id):
        if record["status"] in {"QUEUED", "RETRYING"}:
            return str(record["stage"])
        if record["status"] not in {"COMPLETE", "SKIPPED"}:
            return None
    return None


async def run_selected_job(
    job_id: str,
    *,
    repository: Any | None = None,
    jobs: Any | None = None,
) -> dict[str, Any]:
    """Claim and finish exactly ``job_id``; never claim the general queue."""
    if repository is None or jobs is None:
        repository, jobs = build_job_service()
    job = repository.claim_job(job_id)
    if job is None:
        current = repository.get_job(job_id)
        if current["status"] in {"COMPLETE", "FAILED"}:
            return current
        raise RuntimeError(f"selected job is not claimable: {job_id} ({current['status']})")

    if job["kind"] not in {"fixture", "url"}:
        repository.finish_job(job_id, status="FAILED", error_code="UNKNOWN_JOB_KIND")
        raise ValueError(f"unknown generation job kind: {job['kind']}")

    while True:
        if _finish_if_terminal(repository, job["run_id"]):
            return repository.get_job(job_id)
        stage = _next_pending_stage(repository, job["run_id"])
        if stage is None:
            current = repository.get_job(job_id)
            if current["status"] == "RUNNING":
                repository.finish_job(job_id, status="FAILED", error_code="STAGE_PIPELINE_STALLED")
            return repository.get_job(job_id)
        if stage not in STAGES:
            repository.finish_job(job_id, status="FAILED", error_code="UNKNOWN_STAGE")
            raise ValueError(f"unknown generation stage: {stage}")
        await execute_generation_stage(
            job["run_id"], stage, repository=repository, jobs=jobs, schedule_next=False
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute one persisted ProductLens generation job")
    parser.add_argument("job_id", help="The exact generation_jobs.id to execute")
    args = parser.parse_args()
    result = asyncio.run(run_selected_job(args.job_id))
    print(f"job_id={result['id']} run_id={result['run_id']} status={result['status']}", flush=True)


if __name__ == "__main__":
    main()
