"""Cancel every queued/running local generation run."""

from __future__ import annotations

from productlens.config.settings import Settings
from productlens.persistence.repository import RunRepository


def main() -> None:
    settings = Settings.from_environment()
    repo = RunRepository(settings.database_url)
    rows = repo.connection.execute(
        """
        SELECT id, status, stage FROM demo_runs
        WHERE status IN ('QUEUED', 'RUNNING', 'RETRYING')
        ORDER BY updated_at DESC
        """
    ).fetchall()
    print(f"active_runs={len(rows)}")
    cancelled: list[str] = []
    for row in rows:
        run_id = row["id"]
        try:
            repo.cancel_run(run_id)
            cancelled.append(run_id)
            print(f"cancelled {run_id} was={row['status']}/{row['stage']}")
        except Exception as error:  # noqa: BLE001
            print(f"failed {run_id}: {error}")
    jobs = repo.connection.execute(
        """
        SELECT id, run_id, status FROM generation_jobs
        WHERE status IN ('QUEUED', 'RUNNING', 'RETRYING')
        """
    ).fetchall()
    print(f"remaining_jobs={len(jobs)}")
    for job in jobs:
        print(f"  job {job['id']} run={job['run_id']} status={job['status']}")
    print(f"done cancelled={len(cancelled)}")


if __name__ == "__main__":
    main()
