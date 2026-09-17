from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.persistence.db import _DAO


class RunsDAO(_DAO):
    """Domain persistence for runs."""

    def start_attempt(self, run_id: str, stage: str) -> int:
        ordinal = int(
            self._fetchone(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM demo_attempts WHERE run_id=?", (run_id,)
            )[0]
        )
        self.connection.execute(
            "INSERT INTO demo_attempts VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid4()), run_id, ordinal, stage, "RUNNING", None, datetime.now(UTC).isoformat()),
        )
        self.connection.commit()
        return ordinal

    def finish_attempt(
        self, run_id: str, ordinal: int, *, status: str, failure_code: str | None = None
    ) -> None:
        self.connection.execute(
            "UPDATE demo_attempts SET status=?, failure_code=? WHERE run_id=? AND ordinal=?",
            (status, failure_code, run_id, ordinal),
        )
        self.connection.commit()

    def create_request(
        self, request_id: str, url: str, objective: str, project_id: str | None = None
    ) -> dict[str, Any]:
        existing = self.connection.execute(
            "SELECT * FROM demo_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if existing:
            return dict(existing)
        if project_id:
            self.get_project(project_id)
        identifier = str(uuid4())
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            "INSERT INTO demo_requests (id, request_id, url, objective, status, created_at, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (identifier, request_id, url, objective, "QUEUED", now, project_id),
        )
        self.connection.commit()
        return dict(self._fetchone("SELECT * FROM demo_requests WHERE id = ?", (identifier,)))

    def list_runs_for_user(
        self, user_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT demo_runs.*, demo_requests.url, demo_requests.objective, demo_requests.project_id
            FROM demo_runs JOIN demo_requests ON demo_requests.id=demo_runs.request_id
            JOIN projects ON projects.id=demo_requests.project_id
            WHERE projects.owner_id=? ORDER BY demo_runs.created_at DESC LIMIT ? OFFSET ?""",
            (user_id, limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_run_for_user(self, run_id: str, user_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            """SELECT demo_runs.* FROM demo_runs JOIN demo_requests ON demo_requests.id=demo_runs.request_id
            JOIN projects ON projects.id=demo_requests.project_id
            WHERE demo_runs.id=? AND projects.owner_id=?""",
            (run_id, user_id),
        ).fetchone()
        if not row:
            raise KeyError(run_id)
        return dict(row)

    def user_has_product_access(self, product_key: str, user_id: str) -> bool:
        """Knowledge invalidation is scoped to a product in the caller's workspace."""
        row = self.connection.execute(
            """SELECT 1 FROM demo_requests AS requests
            JOIN projects ON projects.id=requests.project_id
            WHERE requests.url=? AND projects.owner_id=? LIMIT 1""",
            (product_key, user_id),
        ).fetchone()
        return bool(row)

    def create_run(self, request_id: str, artifact_root: str) -> dict[str, Any]:
        identifier = str(uuid4())
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            "INSERT INTO demo_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (identifier, request_id, "QUEUED", "QUEUED", artifact_root, None, now, now),
        )
        self.connection.commit()
        return self.get_run(identifier)

    def create_idempotent_run(
        self, request_id: str, artifact_root: str
    ) -> tuple[dict[str, Any], bool]:
        """Return an existing non-failed run for the request instead of duplicating side effects."""
        existing = self.connection.execute(
            "SELECT * FROM demo_runs WHERE request_id=? AND status != 'FAILED' ORDER BY created_at DESC LIMIT 1",
            (request_id,),
        ).fetchone()
        if existing:
            return dict(existing), False
        return self.create_run(request_id, artifact_root), True

    def create_retry_run(self, parent_run_id: str, artifact_root: str) -> dict[str, Any]:
        """Create a new, auditable run for an explicit retry without touching prior evidence."""
        parent = self.get_run(parent_run_id)
        if parent["status"] != "FAILED":
            raise ValueError("only failed runs may be retried")
        retry = self.create_run(parent["request_id"], artifact_root)
        self.connection.execute(
            "INSERT INTO run_lineage VALUES (?, ?, ?)",
            (parent_run_id, retry["id"], datetime.now(UTC).isoformat()),
        )
        self.connection.commit()
        return retry

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        """Cancel a queued or active run without deleting its audit evidence.

        Cancellation is cooperative for a stage that is already inside a browser
        call: workers observe the terminal run state before claiming/finalising
        another stage.  Queued work is made unclaimable immediately.
        """
        run = self.get_run(run_id)
        if run["status"] in {"COMPLETE", "FAILED", "CANCELLED"}:
            raise ValueError("only queued or active runs may be cancelled")
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            "UPDATE demo_runs SET stage='CANCELLED', status='CANCELLED', error_code='CANCELLED_BY_USER', updated_at=? WHERE id=?",
            (now, run_id),
        )
        self.connection.execute(
            """UPDATE generation_jobs SET status='CANCELLED', error_code='CANCELLED_BY_USER',
            completed_at=? WHERE run_id=? AND status IN ('QUEUED', 'RETRYING', 'RUNNING')""",
            (now, run_id),
        )
        self.connection.execute(
            """UPDATE generation_stage_jobs SET status='CANCELLED', error_code='CANCELLED_BY_USER',
            completed_at=?, updated_at=? WHERE run_id=? AND status IN ('QUEUED', 'RETRYING', 'RUNNING')""",
            (now, now, run_id),
        )
        self.connection.commit()
        return self.get_run(run_id)

    def resume_run(self, run_id: str) -> dict[str, Any]:
        """Requeue an interrupted durable job only when no action can be replayed.

        Failed/cancelled runs intentionally require the auditable child-run retry
        endpoint.  This operation is solely for a queued/recoverable root job.
        """
        run = self.get_run(run_id)
        if run["status"] not in {"QUEUED", "RETRYING"}:
            raise ValueError("only queued or recoverable runs may be resumed")
        job = self.job_for_run(run_id)
        if not job:
            raise ValueError("run has no durable job to resume")
        if job["status"] not in {"QUEUED", "RETRYING"}:
            raise ValueError("run is already being processed or is terminal")
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            "UPDATE generation_jobs SET status='QUEUED', error_code=NULL, claimed_at=NULL, completed_at=NULL WHERE id=?",
            (job["id"],),
        )
        self.connection.execute(
            "UPDATE demo_runs SET stage='QUEUED', status='QUEUED', error_code=NULL, updated_at=? WHERE id=?",
            (now, run_id),
        )
        self.connection.commit()
        return dict(self.get_job(job["id"]))

    def delete_empty_terminal_run(self, run_id: str) -> bool:
        """Delete a terminal run only when it contains no retained artifacts.

        This deliberately keeps failed runs with screenshots, traces, reports, or
        videos: those records are essential repair evidence.  It is safe for the
        UI's "clear empty failures" control and never deletes the parent request.
        """
        run = self.get_run(run_id)
        if run["status"] not in {"FAILED", "CANCELLED"}:
            raise ValueError("only failed or cancelled runs can be removed")
        evidence_tables = (
            "artifacts",
            "demo_attempts",
            "demo_plans",
            "workflow_steps",
            "browser_sessions",
            "interaction_events",
            "presentation_plans",
            "narration_scripts",
            "quality_reports",
            "form_schemas",
            "synthetic_datasets",
            "audio_assets",
            "video_renders",
            "provider_calls",
            "run_artifact_documents",
        )
        if any(
            self.connection.execute(
                f"SELECT 1 FROM {table} WHERE run_id=? LIMIT 1", (run_id,)
            ).fetchone()
            for table in evidence_tables
        ):
            raise ValueError("run has retained evidence and cannot be removed as empty")
        # Lineage is an audit record. Do not remove a run that has children.
        if self.connection.execute(
            "SELECT 1 FROM run_lineage WHERE parent_run_id=? LIMIT 1", (run_id,)
        ).fetchone():
            raise ValueError("run has retry descendants and cannot be removed")
        # Database rows are not the complete retention boundary: a worker can
        # crash before registering files in the ledger. Refuse to remove the
        # run row while its exact artifact directory still contains anything,
        # including an unregistered crash artifact. This keeps cleanup from
        # creating orphaned evidence and makes the operation reversible by
        # the caller until the directory is explicitly archived/removed.
        artifact_root = Path(str(run.get("artifact_root") or "")).resolve()
        run_directory = (artifact_root / "runs" / run_id).resolve()
        if run_directory.parent != (artifact_root / "runs").resolve():
            raise ValueError("run artifact directory is outside configured artifact root")
        if run_directory.exists() and any(path.is_file() for path in run_directory.rglob("*")):
            raise ValueError("run artifact directory contains unregistered evidence")
        self.connection.execute("DELETE FROM run_lineage WHERE retry_run_id=?", (run_id,))
        self.connection.execute("DELETE FROM generation_stage_jobs WHERE run_id=?", (run_id,))
        self.connection.execute("DELETE FROM generation_jobs WHERE run_id=?", (run_id,))
        self.connection.execute("DELETE FROM demo_runs WHERE id=?", (run_id,))
        self.connection.commit()
        return True

    def purge_empty_terminal_runs(self, *, older_than_seconds: int = 0) -> list[str]:
        """Retention sweep for truly empty terminal runs, returning deleted IDs."""
        cutoff = datetime.fromtimestamp(
            datetime.now(UTC).timestamp() - older_than_seconds, UTC
        ).isoformat()
        candidates = self.connection.execute(
            "SELECT id FROM demo_runs WHERE status IN ('FAILED', 'CANCELLED') AND updated_at <= ? ORDER BY updated_at",
            (cutoff,),
        ).fetchall()
        deleted: list[str] = []
        for candidate in candidates:
            try:
                if self.delete_empty_terminal_run(candidate["id"]):
                    deleted.append(candidate["id"])
            except ValueError:
                continue
        return deleted

    def copy_run_evidence(
        self, parent_run_id: str, retry_run_id: str, *, through_stage: str
    ) -> None:
        """Copy immutable database evidence needed by a targeted child retry."""
        target = self.stage_job(retry_run_id, through_stage)["ordinal"]
        tables_by_stage = {
            0: ("form_schemas",),
            1: ("demo_plans", "workflow_steps", "synthetic_datasets"),
            2: ("interaction_events", "presentation_plans"),
            3: ("narration_scripts", "audio_assets"),
        }
        for ordinal, tables in tables_by_stage.items():
            if ordinal >= target:
                continue
            for table in tables:
                rows = self.connection.execute(
                    f"SELECT * FROM {table} WHERE run_id=?", (parent_run_id,)
                ).fetchall()
                for row in rows:
                    values = dict(row)
                    values["id"] = str(uuid4())
                    values["run_id"] = retry_run_id
                    columns = ", ".join(values)
                    placeholders = ", ".join("?" for _ in values)
                    self.connection.execute(
                        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                        tuple(values.values()),
                    )
        self.connection.commit()

    def update_run(
        self, run_id: str, *, stage: str, status: str, error_code: str | None = None
    ) -> dict[str, Any]:
        self.connection.execute(
            "UPDATE demo_runs SET stage=?, status=?, error_code=?, updated_at=? WHERE id=? AND status != 'CANCELLED'",
            (stage, status, error_code, datetime.now(UTC).isoformat(), run_id),
        )
        self.connection.commit()
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM demo_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        return dict(row)

    def list_runs(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Return recent runs with their request context, never provider secrets."""
        rows = self.connection.execute(
            """SELECT runs.*, requests.url, requests.objective
            FROM demo_runs AS runs
            JOIN demo_requests AS requests ON requests.id = runs.request_id
            ORDER BY runs.created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]

    def run_details(self, run_id: str) -> dict[str, Any]:
        """Collect the durable, frontend-safe evidence produced by one run."""
        run = self.get_run(run_id)
        request = self.get_request(run["request_id"])

        def payloads(table: str) -> list[dict[str, Any]]:
            order = (
                "ordinal, id"
                if table in {"workflow_steps", "interaction_events"}
                else "created_at, id"
            )
            rows = self.connection.execute(
                f"SELECT payload_json FROM {table} WHERE run_id=? ORDER BY {order}", (run_id,)
            ).fetchall()
            return [json.loads(row["payload_json"]) for row in rows]

        return {
            "run": run,
            "request": request,
            "job": self._job_for_run(run_id),
            "stage_jobs": self.stage_jobs(run_id),
            "plans": payloads("demo_plans"),
            "workflow_steps": payloads("workflow_steps"),
            "interaction_events": payloads("interaction_events"),
            "presentation_plans": payloads("presentation_plans"),
            "narration_scripts": payloads("narration_scripts"),
            "quality_reports": payloads("quality_reports"),
            # The document ledger is the canonical, version-neutral view of
            # every architecture artifact. Keep the legacy typed collections
            # above for API compatibility, while exposing the newer
            # objective/knowledge/storyboard documents to resumable workers
            # and future clients without filesystem enumeration.
            "artifact_documents": self.run_artifact_documents(run_id),
            "form_schemas": payloads("form_schemas"),
            "synthetic_datasets": payloads("synthetic_datasets"),
            "attempts": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT ordinal, stage, status, failure_code, created_at FROM demo_attempts WHERE run_id=? ORDER BY ordinal",
                    (run_id,),
                ).fetchall()
            ],
            "browser_sessions": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT provider, external_session_id, status, created_at "
                    "FROM browser_sessions WHERE run_id=? ORDER BY created_at",
                    (run_id,),
                ).fetchall()
            ],
            "retry_of": (
                self.connection.execute(
                    "SELECT parent_run_id FROM run_lineage WHERE retry_run_id=?", (run_id,)
                ).fetchone()
                or {"parent_run_id": None}
            )["parent_run_id"],
            "retries": [
                row["retry_run_id"]
                for row in self.connection.execute(
                    "SELECT retry_run_id FROM run_lineage WHERE parent_run_id=? ORDER BY created_at",
                    (run_id,),
                ).fetchall()
            ],
            "artifacts": self.locations(run_id),
            "audio_assets": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT location, duration_seconds, provider, created_at FROM audio_assets WHERE run_id=? ORDER BY created_at",
                    (run_id,),
                ).fetchall()
            ],
            "video_renders": [
                {**dict(row), "metadata": json.loads(row["payload_json"])}
                for row in self.connection.execute(
                    "SELECT location, status, payload_json, created_at FROM video_renders WHERE run_id=? ORDER BY created_at",
                    (run_id,),
                ).fetchall()
            ],
            "provider_calls": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT provider, operation, status, duration_ms, error_code, created_at FROM provider_calls WHERE run_id=? ORDER BY created_at",
                    (run_id,),
                ).fetchall()
            ],
        }

    def get_request(self, request_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM demo_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if not row:
            raise KeyError(request_id)
        return dict(row)
