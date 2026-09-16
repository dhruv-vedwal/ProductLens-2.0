from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.persistence.db import _DAO

class JobsDAO(_DAO):
    """Domain persistence for jobs."""

    def prepare_targeted_retry(self, run_id: str, start_stage: str) -> None:
        """Mark inherited stages complete and queue only the safe repair boundary."""
        stage = self.stage_job(run_id, start_stage)
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE generation_stage_jobs
            SET status=CASE WHEN ordinal < ? THEN 'COMPLETE' ELSE 'QUEUED' END,
                error_code=NULL, claimed_at=NULL, completed_at=CASE WHEN ordinal < ? THEN completed_at ELSE NULL END,
                updated_at=? WHERE run_id=?""",
            (stage["ordinal"], stage["ordinal"], now, run_id),
        )
        # A targeted retry may start from a previously terminal run.  Restore
        # the run lease before stage claiming; otherwise the compare-and-set
        # correctly refuses the queued stage because the parent still says
        # COMPLETE/FAILED, and a supervisor can silently report success with
        # no QA artifact produced.
        self.connection.execute(
            """UPDATE demo_runs
            SET stage='QUEUED', status='QUEUED', error_code=NULL, updated_at=?
            WHERE id=? AND status NOT IN ('CANCELLED')""",
            (now, run_id),
        )
        self.connection.commit()

    def enqueue_job(self, run_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist a worker message before it is published to a broker."""
        existing = self.connection.execute(
            "SELECT * FROM generation_jobs WHERE run_id=?", (run_id,)
        ).fetchone()
        if existing:
            return dict(existing)
        now = datetime.now(UTC).isoformat()
        job = {
            "id": str(uuid4()),
            "run_id": run_id,
            "kind": kind,
            "payload_json": json.dumps(payload),
            "status": "QUEUED",
            "delivery_attempts": 0,
            "error_code": None,
            "queued_at": now,
            "claimed_at": None,
            "completed_at": None,
        }
        self.connection.execute(
            "INSERT INTO generation_jobs VALUES (:id,:run_id,:kind,:payload_json,:status,:delivery_attempts,:error_code,:queued_at,:claimed_at,:completed_at)",
            job,
        )
        self._ensure_stage_jobs(run_id)
        self.connection.commit()
        return job

    def _ensure_stage_jobs(self, run_id: str) -> None:
        """Create durable checkpoints before a worker starts any provider work."""
        now = datetime.now(UTC).isoformat()
        for ordinal, stage in enumerate(
            ("DISCOVERY", "PLANNING", "EXECUTION", "NARRATION", "RENDER", "VIDEO_QA")
        ):
            self.connection.execute(
                """INSERT INTO generation_stage_jobs
                (id, run_id, stage, ordinal, status, error_code, delivery_attempts, claimed_at,
                 started_at, completed_at, updated_at)
                VALUES (?, ?, ?, ?, 'QUEUED', NULL, 0, NULL, NULL, NULL, ?)
                ON CONFLICT(run_id, stage) DO NOTHING""",
                (str(uuid4()), run_id, stage, ordinal, now),
            )

    def ensure_stage_jobs(self, run_id: str) -> None:
        """Idempotently provision the stage ledger for every execution entry point.

        API requests normally get this through :meth:`enqueue_job`, but an
        operator may deliberately invoke the shared service from a supervisor
        or command-line acceptance runner.  Those runs need exactly the same
        recoverable checkpoint ledger; otherwise a process loss would leave
        artifacts without durable stage ownership.
        """
        self._ensure_stage_jobs(run_id)
        self.connection.commit()

    def update_stage_job(
        self, run_id: str, stage: str, *, status: str, error_code: str | None = None
    ) -> None:
        if status not in {"QUEUED", "RETRYING", "RUNNING", "COMPLETE", "FAILED", "SKIPPED"}:
            raise ValueError("invalid stage job status")
        now = datetime.now(UTC).isoformat()
        started = now if status == "RUNNING" else None
        completed = now if status in {"COMPLETE", "FAILED", "SKIPPED"} else None
        self.connection.execute(
            """UPDATE generation_stage_jobs SET status=?, error_code=?,
                started_at=COALESCE(started_at, ?), completed_at=?, heartbeat_at=?, updated_at=?
                WHERE run_id=? AND stage=? AND status != 'CANCELLED'""",
            (
                status,
                error_code,
                started,
                completed,
                now if status == "RUNNING" else None,
                now,
                run_id,
                stage,
            ),
        )
        self.connection.commit()

    def claim_stage_job(self, run_id: str, stage: str) -> dict[str, Any] | None:
        """Atomically claim one ready stage.

        A stage cannot be claimed until every earlier stage has reached a terminal
        successful state. This database-enforced compare-and-set keeps duplicate
        or out-of-order Dramatiq deliveries safe across local SQLite and PostgreSQL.
        """
        now = datetime.now(UTC).isoformat()
        cursor = self.connection.execute(
            """UPDATE generation_stage_jobs AS candidate
            SET status='RUNNING', error_code=NULL, delivery_attempts=delivery_attempts+1, claimed_at=?,
                started_at=COALESCE(started_at, ?), completed_at=NULL, heartbeat_at=?, updated_at=?
            WHERE candidate.run_id=? AND candidate.stage=?
              AND candidate.status IN ('QUEUED', 'RETRYING')
              AND EXISTS (
                SELECT 1 FROM demo_runs AS run
                WHERE run.id=candidate.run_id AND run.status NOT IN ('CANCELLED', 'FAILED', 'COMPLETE')
              )
              AND NOT EXISTS (
                SELECT 1 FROM generation_stage_jobs AS earlier
                WHERE earlier.run_id=candidate.run_id
                  AND earlier.ordinal<candidate.ordinal
                  AND earlier.status NOT IN ('COMPLETE', 'SKIPPED')
              )""",
            (now, now, now, now, run_id, stage),
        )
        self.connection.commit()
        if cursor.rowcount != 1:
            return None
        return self.stage_job(run_id, stage)

    def stage_job(self, run_id: str, stage: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT stage, ordinal, status, error_code, delivery_attempts, claimed_at, started_at, completed_at, heartbeat_at, updated_at "
            "FROM generation_stage_jobs WHERE run_id=? AND stage=?",
            (run_id, stage),
        ).fetchone()
        if not row:
            raise KeyError(f"{run_id}:{stage}")
        return dict(row)

    def heartbeat_stage_job(self, run_id: str, stage: str) -> None:
        """Refresh the liveness lease without changing lifecycle status."""
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE generation_stage_jobs
            SET heartbeat_at=?, updated_at=?
            WHERE run_id=? AND stage=? AND status='RUNNING'""",
            (now, now, run_id, stage),
        )
        self.connection.commit()

    def claim_next_stage_job(self) -> dict[str, Any] | None:
        """Claim the oldest stage whose predecessor is complete for the local worker."""
        row = self.connection.execute(
            """SELECT candidate.run_id, candidate.stage
            FROM generation_stage_jobs AS candidate
            JOIN generation_jobs AS job ON job.run_id=candidate.run_id
            WHERE job.status='RUNNING' AND candidate.status IN ('QUEUED', 'RETRYING')
              AND EXISTS (
                SELECT 1 FROM demo_runs AS run
                WHERE run.id=candidate.run_id AND run.status NOT IN ('CANCELLED', 'FAILED', 'COMPLETE')
              )
              AND NOT EXISTS (
                SELECT 1 FROM generation_stage_jobs AS earlier
                WHERE earlier.run_id=candidate.run_id
                  AND earlier.ordinal<candidate.ordinal
                  AND earlier.status NOT IN ('COMPLETE', 'SKIPPED')
              )
            ORDER BY candidate.updated_at, candidate.ordinal LIMIT 1"""
        ).fetchone()
        if not row:
            return None
        claimed = self.claim_stage_job(row["run_id"], row["stage"])
        if claimed is not None:
            # Keep the owning run alongside the stage record so local workers
            # can safely hand the claimed lease to the shared executor.
            claimed["run_id"] = row["run_id"]
        return claimed

    def stage_jobs(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT stage, ordinal, status, error_code, delivery_attempts, claimed_at, started_at, completed_at, updated_at "
                "FROM generation_stage_jobs WHERE run_id=? ORDER BY ordinal",
                (run_id,),
            ).fetchall()
        ]

    def fail_active_stage_jobs(self, run_id: str, error_code: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE generation_stage_jobs SET status='FAILED', error_code=?, completed_at=?, updated_at=?
            WHERE run_id=? AND status='RUNNING'""",
            (error_code, now, now, run_id),
        )
        self.connection.commit()

    def claim_job(self, job_id: str) -> dict[str, Any] | None:
        """Atomically claim one queued/recoverable job without double execution."""
        now = datetime.now(UTC).isoformat()
        cursor = self.connection.execute(
            "UPDATE generation_jobs SET status='RUNNING', claimed_at=?, delivery_attempts=delivery_attempts+1 "
            "WHERE id=? AND status IN ('QUEUED', 'RETRYING') AND EXISTS ("
            "SELECT 1 FROM demo_runs WHERE demo_runs.id=generation_jobs.run_id "
            "AND demo_runs.status NOT IN ('CANCELLED', 'FAILED', 'COMPLETE'))",
            (now, job_id),
        )
        self.connection.commit()
        if cursor.rowcount != 1:
            return None
        return self.get_job(job_id)

    def claim_next_job(self) -> dict[str, Any] | None:
        """Claim the oldest pending job for the local durable-worker development mode."""
        row = self.connection.execute(
            "SELECT job.id FROM generation_jobs AS job "
            "JOIN demo_runs AS run ON run.id=job.run_id "
            "WHERE job.status IN ('QUEUED', 'RETRYING') "
            "AND run.status NOT IN ('CANCELLED', 'FAILED', 'COMPLETE') "
            "ORDER BY job.queued_at LIMIT 1"
        ).fetchone()
        return self.claim_job(row["id"]) if row else None

    def fail_orphaned_jobs(self) -> int:
        """Terminally close outbox records whose run was removed by retention.

        Older SQLite databases can contain these records from development tests
        that delete empty runs.  An orphan has no request or run evidence and can
        never be executed safely, so it must not occupy a worker lease.
        """
        now = datetime.now(UTC).isoformat()
        cursor = self.connection.execute(
            """UPDATE generation_jobs SET status='FAILED', error_code='RUN_ORPHANED', completed_at=?
            WHERE status IN ('QUEUED', 'RETRYING', 'RUNNING')
              AND NOT EXISTS (SELECT 1 FROM demo_runs WHERE demo_runs.id=generation_jobs.run_id)""",
            (now,),
        )
        self.connection.execute(
            """UPDATE generation_stage_jobs SET status='FAILED', error_code='RUN_ORPHANED',
            completed_at=?, updated_at=?
            WHERE status IN ('QUEUED', 'RETRYING', 'RUNNING')
              AND NOT EXISTS (SELECT 1 FROM demo_runs WHERE demo_runs.id=generation_stage_jobs.run_id)""",
            (now, now),
        )
        self.connection.commit()
        return int(cursor.rowcount)

    def recover_stale_jobs(self, max_running_seconds: int = 1_800) -> int:
        """Make abandoned worker claims visible for an explicit, auditable retry."""
        cutoff = datetime.fromtimestamp(
            datetime.now(UTC).timestamp() - max_running_seconds, UTC
        ).isoformat()
        cursor = self.connection.execute(
            "UPDATE generation_jobs SET status='RETRYING', error_code='WORKER_LEASE_EXPIRED', "
            "claimed_at=NULL WHERE status='RUNNING' AND claimed_at < ?",
            (cutoff,),
        )
        if cursor.rowcount:
            self.connection.execute(
                """UPDATE generation_stage_jobs SET status='QUEUED',
                error_code='WORKER_LEASE_EXPIRED', claimed_at=NULL, completed_at=NULL, updated_at=?
                WHERE status='RUNNING' AND run_id IN (
                  SELECT run_id FROM generation_jobs
                  WHERE status='RETRYING' AND error_code='WORKER_LEASE_EXPIRED'
                )""",
                (datetime.now(UTC).isoformat(),),
            )
        self.connection.commit()
        return int(cursor.rowcount)

    def finish_job(self, job_id: str, *, status: str, error_code: str | None = None) -> None:
        if status not in {"COMPLETE", "FAILED", "RETRYING"}:
            raise ValueError("invalid durable job status")
        self.connection.execute(
            "UPDATE generation_jobs SET status=?, error_code=?, completed_at=? WHERE id=? AND status != 'CANCELLED'",
            (
                status,
                error_code,
                datetime.now(UTC).isoformat() if status != "RETRYING" else None,
                job_id,
            ),
        )
        self.connection.commit()

    def get_job(self, job_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM generation_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not row:
            raise KeyError(job_id)
        job = dict(row)
        job["payload"] = json.loads(job.pop("payload_json"))
        return job

    def _job_for_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM generation_jobs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not row:
            return None
        job = dict(row)
        job["payload"] = json.loads(job.pop("payload_json"))
        return job

    def job_for_run(self, run_id: str) -> dict[str, Any] | None:
        """Return non-secret generation configuration retained for an auditable retry."""
        return self._job_for_run(run_id)
