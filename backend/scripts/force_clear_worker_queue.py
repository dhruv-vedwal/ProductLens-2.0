"""Force-cancel leftover generation jobs that outlived their runs."""

from __future__ import annotations

from datetime import UTC, datetime

from productlens.config.settings import Settings
from productlens.persistence.repository import RunRepository


def main() -> None:
    settings = Settings.from_environment()
    repo = RunRepository(settings.database_url)
    now = datetime.now(UTC).isoformat()

    # Cancel any run still marked active.
    runs = repo.connection.execute(
        """
        SELECT id FROM demo_runs
        WHERE status IN ('QUEUED', 'RUNNING', 'RETRYING')
        """
    ).fetchall()
    for row in runs:
        try:
            repo.cancel_run(row["id"])
        except Exception as error:  # noqa: BLE001
            print(f"run cancel skipped {row['id']}: {error}")

    # Force-cancel orphan/outbox jobs the worker could still claim.
    jobs = repo.connection.execute(
        """
        UPDATE generation_jobs
        SET status='CANCELLED', error_code='CANCELLED_BY_USER', completed_at=?
        WHERE status IN ('QUEUED', 'RUNNING', 'RETRYING')
        """,
        (now,),
    )
    stages = repo.connection.execute(
        """
        UPDATE generation_stage_jobs
        SET status='CANCELLED', error_code='CANCELLED_BY_USER',
            completed_at=?, updated_at=?
        WHERE status IN ('QUEUED', 'RUNNING', 'RETRYING')
        """,
        (now, now),
    )
    repo.connection.execute(
        """
        UPDATE demo_runs
        SET stage='CANCELLED', status='CANCELLED',
            error_code=COALESCE(error_code, 'CANCELLED_BY_USER'), updated_at=?
        WHERE status IN ('QUEUED', 'RUNNING', 'RETRYING')
        """,
        (now,),
    )
    repo.connection.commit()

    leftover_jobs = repo.connection.execute(
        """
        SELECT COUNT(*) AS n FROM generation_jobs
        WHERE status IN ('QUEUED', 'RUNNING', 'RETRYING')
        """
    ).fetchone()["n"]
    leftover_runs = repo.connection.execute(
        """
        SELECT COUNT(*) AS n FROM demo_runs
        WHERE status IN ('QUEUED', 'RUNNING', 'RETRYING')
        """
    ).fetchone()["n"]
    print(
        f"jobs_updated={jobs.rowcount} stages_updated={stages.rowcount} "
        f"leftover_jobs={leftover_jobs} leftover_runs={leftover_runs}"
    )


if __name__ == "__main__":
    main()
