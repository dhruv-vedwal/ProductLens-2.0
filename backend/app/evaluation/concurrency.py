"""Deterministic local validation of durable multi-run job claims.

This benchmark exercises the same compare-and-set claim boundary used by the
local worker and broker consumers. It deliberately uses independent database
connections in worker threads, so a green result is evidence of cross-worker
claim safety rather than an asyncio single-thread illusion.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic
from uuid import uuid4

from app.persistence.repository import RunRepository


def run_claim_benchmark(database: Path | str, *, runs: int, workers: int | None = None) -> dict:
    """Claim each of ``runs`` independent jobs exactly once.

    The benchmark is intentionally provider-free. It verifies the database
    invariant that allows many browser/render runs to be queued at once; it
    does not pretend to measure provider throughput or video duration.
    """
    if runs not in {1, 10, 50, 100}:
        raise ValueError("runs must be one of the acceptance levels: 1, 10, 50, 100")
    worker_count = max(1, min(runs, workers or runs))
    root = Path(database)
    setup = RunRepository(root)
    for _ in range(runs):
        request = setup.create_request(
            f"concurrency-{uuid4()}", "fixture://concurrency", "benchmark"
        )
        run = setup.create_run(request["id"], str(root.parent))
        setup.enqueue_job(run["id"], "fixture", {"gate": 1, "render": False})
    setup.close()

    started = monotonic()

    def claim_until_empty(_worker: int) -> list[str]:
        repository = RunRepository(root)
        claimed: list[str] = []
        try:
            while True:
                job = repository.claim_next_job()
                if job is None:
                    return claimed
                claimed.append(str(job["id"]))
        finally:
            repository.close()

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        results = list(executor.map(claim_until_empty, range(worker_count)))
    claimed = [item for result in results for item in result]
    unique = len(set(claimed))
    return {
        "requested_runs": runs,
        "workers": worker_count,
        "claimed_runs": len(claimed),
        "unique_claims": unique,
        "duplicate_claims": len(claimed) - unique,
        "all_claimed_once": len(claimed) == runs and unique == runs,
        "duration_ms": round((monotonic() - started) * 1000, 2),
    }
