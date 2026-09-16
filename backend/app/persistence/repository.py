from __future__ import annotations

from pathlib import Path
from typing import Any

from app.persistence.artifacts import ArtifactsDAO
from app.persistence.credentials import CredentialsDAO
from app.persistence.db import _PostgresConnection, bootstrap_schema, connect_database
from app.persistence.jobs import JobsDAO
from app.persistence.knowledge import KnowledgeDAO
from app.persistence.projects import ProjectsDAO
from app.persistence.runs import RunsDAO
from app.persistence.users import UsersDAO

# Re-export for callers/tests that import connection helpers from this module.
__all__ = ["RunRepository", "_PostgresConnection"]


class RunRepository:
    """Idempotent job/run persistence with no secret-bearing payload columns."""

    def __init__(self, database: Path | str):
        self.dialect, self.connection = connect_database(database)
        self._users = UsersDAO(self)
        self._projects = ProjectsDAO(self)
        self._credentials = CredentialsDAO(self)
        self._runs = RunsDAO(self)
        self._jobs = JobsDAO(self)
        self._artifacts = ArtifactsDAO(self)
        self._knowledge = KnowledgeDAO(self)
        bootstrap_schema(self.connection, self.dialect, self)

    def close(self) -> None:
        """Release SQLite handles for short-lived workers and test processes."""
        self.connection.close()

    def _upgrade_sqlite_schema(self) -> None:
        from app.persistence.db import upgrade_sqlite_schema

        upgrade_sqlite_schema(self)

    def _fetchone(
        self, statement: str, parameters: tuple[Any, ...] | dict[str, Any] | None = None
    ) -> Any:
        """Return one row through the portable SQLite/PostgreSQL boundary."""
        cursor = (
            self.connection.execute(statement)
            if parameters is None
            else self.connection.execute(statement, parameters)
        )
        return cursor.fetchone()

    def _fetchall(
        self, statement: str, parameters: tuple[Any, ...] | dict[str, Any] | None = None
    ) -> list[Any]:
        """Return all rows through the portable connection boundary."""
        cursor = (
            self.connection.execute(statement)
            if parameters is None
            else self.connection.execute(statement, parameters)
        )
        return list(cursor.fetchall())


    def start_attempt(self, run_id: str, stage: str) -> int:
        return self._runs.start_attempt(run_id, stage)

    def finish_attempt(
        self, run_id: str, ordinal: int, *, status: str, failure_code: str | None = None
    ) -> None:
        self._runs.finish_attempt(run_id, ordinal, status=status, failure_code=failure_code)

    def save_json_artifact(self, table: str, run_id: str, payload: dict[str, Any]) -> None:
        self._artifacts.save_json_artifact(table, run_id, payload)

    def replace_json_artifact(self, table: str, run_id: str, payload: dict[str, Any]) -> None:
        """Replace a singleton stage artifact on a same-run recovery attempt."""

        self._artifacts.replace_json_artifact(table, run_id, payload)

    def save_workflow_steps(self, run_id: str, steps: list[dict[str, Any]]) -> None:
        self._artifacts.save_workflow_steps(run_id, steps)

    def save_interaction_events(self, run_id: str, events: list[dict[str, Any]]) -> None:
        self._artifacts.save_interaction_events(run_id, events)

    def replace_interaction_events(self, run_id: str, events: list[dict[str, Any]]) -> None:
        self._artifacts.replace_interaction_events(run_id, events)

    def save_form_schema(self, run_id: str, schema: dict[str, Any]) -> None:
        self._artifacts.save_form_schema(run_id, schema)

    def save_synthetic_dataset(self, run_id: str, dataset: dict[str, Any]) -> None:
        self._artifacts.save_synthetic_dataset(run_id, dataset)

    def save_browser_session(
        self, run_id: str, provider: str, external_session_id: str | None, status: str
    ) -> None:
        self._artifacts.save_browser_session(run_id, provider, external_session_id, status)

    def save_location(self, run_id: str, kind: str, location: str) -> None:
        self._artifacts.save_location(run_id, kind, location)

    def upsert_run_artifact_document(
        self,
        run_id: str,
        *,
        kind: str,
        payload: Any,
        source_path: str,
        sha256: str,
        byte_size: int,
    ) -> None:
        """Persist a version-neutral snapshot of a structured run artifact.

        The filesystem artifact is still authoritative for rendering.  This
        durable ledger is deliberately generic: new evidence contracts do not
        require a new relational table or a destructive migration.
        """

        self._artifacts.upsert_run_artifact_document(run_id, kind=kind, payload=payload, source_path=source_path, sha256=sha256, byte_size=byte_size)

    def persist_run_documents(self, run_id: str, run_root: Path) -> list[str]:
        """Mirror all required architectural JSON evidence written so far.

        It is safe to call after every durable stage: absent future-stage
        files are ignored and a same-kind snapshot is atomically replaced.
        """

        return self._artifacts.persist_run_documents(run_id, run_root)

    def run_artifact_documents(self, run_id: str) -> list[dict[str, Any]]:
        return self._artifacts.run_artifact_documents(run_id)

    def save_audio_asset(
        self,
        run_id: str,
        location: str,
        *,
        duration_seconds: float | None,
        provider: str | None,
    ) -> None:
        self._artifacts.save_audio_asset(run_id, location, duration_seconds=duration_seconds, provider=provider)

    def save_video_render(
        self, run_id: str, location: str, *, status: str, metadata: dict[str, Any]
    ) -> None:
        self._artifacts.save_video_render(run_id, location, status=status, metadata=metadata)

    def record_provider_call(
        self,
        *,
        run_id: str | None,
        provider: str,
        operation: str,
        status: str,
        duration_ms: int | None = None,
        error_code: str | None = None,
        model: str | None = None,
        cost_class: str | None = None,
    ) -> None:
        """Store non-secret provider telemetry for diagnostics and cost review."""

        self._artifacts.record_provider_call(run_id=run_id, provider=provider, operation=operation, status=status, duration_ms=duration_ms, error_code=error_code, model=model, cost_class=cost_class)

    def locations(self, run_id: str) -> list[dict[str, Any]]:
        return self._artifacts.locations(run_id)

    def upsert_provider_config(
        self,
        *,
        provider_type: str,
        name: str,
        credential_reference: str | None,
        active: bool,
        priority: int = 100,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self._credentials.upsert_provider_config(provider_type=provider_type, name=name, credential_reference=credential_reference, active=active, priority=priority, settings=settings)

    def provider_configs(self) -> list[dict[str, Any]]:
        return self._credentials.provider_configs()

    def create_request(
        self, request_id: str, url: str, objective: str, project_id: str | None = None
    ) -> dict[str, Any]:
        return self._runs.create_request(request_id, url, objective, project_id)

    def create_project(self, name: str, *, owner_id: str | None = None) -> dict[str, Any]:
        return self._projects.create_project(name, owner_id=owner_id)

    def create_user(
        self, *, email: str, password_hash: str, display_name: str | None
    ) -> dict[str, Any]:
        return self._users.create_user(email=email, password_hash=password_hash, display_name=display_name)

    def get_user(self, user_id: str) -> dict[str, Any]:
        return self._users.get_user(user_id)

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        return self._users.get_user_by_email(email)

    def update_user_preferences(self, user_id: str, *, theme_preference: str) -> dict[str, Any]:
        return self._users.update_user_preferences(user_id, theme_preference=theme_preference)

    def create_session(self, user_id: str, expires_at: str) -> dict[str, Any]:
        return self._users.create_session(user_id, expires_at)

    def active_session(self, session_id: str, user_id: str) -> bool:
        return self._users.active_session(session_id, user_id)

    def revoke_session(self, session_id: str, user_id: str) -> None:
        self._users.revoke_session(session_id, user_id)

    def create_product_credential(
        self,
        *,
        owner_id: str,
        name: str,
        reference: str,
        username_ciphertext: str,
        password_ciphertext: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return self._credentials.create_product_credential(owner_id=owner_id, name=name, reference=reference, username_ciphertext=username_ciphertext, password_ciphertext=password_ciphertext, project_id=project_id)

    def get_product_credential_for_user(self, credential_id: str, user_id: str) -> dict[str, Any]:
        return self._credentials.get_product_credential_for_user(credential_id, user_id)

    def list_product_credentials_for_user(self, user_id: str) -> list[dict[str, Any]]:
        return self._credentials.list_product_credentials_for_user(user_id)

    def get_product_credential_secrets_by_reference(self, reference: str) -> dict[str, Any] | None:
        return self._credentials.get_product_credential_secrets_by_reference(reference)

    def delete_product_credential_for_user(self, credential_id: str, user_id: str) -> None:
        self._credentials.delete_product_credential_for_user(credential_id, user_id)

    def get_project(self, project_id: str) -> dict[str, Any]:
        return self._projects.get_project(project_id)

    def list_projects(self, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        return self._projects.list_projects(owner_id=owner_id)

    def get_project_for_user(self, project_id: str, user_id: str) -> dict[str, Any]:
        return self._projects.get_project_for_user(project_id, user_id)

    def rename_project_for_user(self, project_id: str, user_id: str, name: str) -> dict[str, Any]:
        return self._projects.rename_project_for_user(project_id, user_id, name)

    def rename_project(self, project_id: str, name: str) -> dict[str, Any]:
        return self._projects.rename_project(project_id, name)

    def ensure_user_project(self, user_id: str) -> dict[str, Any]:
        return self._projects.ensure_user_project(user_id)

    def list_runs_for_user(
        self, user_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        return self._runs.list_runs_for_user(user_id, limit=limit, offset=offset)

    def get_run_for_user(self, run_id: str, user_id: str) -> dict[str, Any]:
        return self._runs.get_run_for_user(run_id, user_id)

    def user_has_product_access(self, product_key: str, user_id: str) -> bool:
        """Knowledge invalidation is scoped to a product in the caller's workspace."""

        return self._runs.user_has_product_access(product_key, user_id)

    def list_projects_legacy(self) -> list[dict[str, Any]]:
        return self._projects.list_projects_legacy()

    def ensure_local_project(self) -> dict[str, Any]:
        """Create a transparent local-only workspace for an unauthenticated studio."""

        return self._projects.ensure_local_project()

    def create_run(self, request_id: str, artifact_root: str) -> dict[str, Any]:
        return self._runs.create_run(request_id, artifact_root)

    def create_idempotent_run(
        self, request_id: str, artifact_root: str
    ) -> tuple[dict[str, Any], bool]:
        """Return an existing non-failed run for the request instead of duplicating side effects."""

        return self._runs.create_idempotent_run(request_id, artifact_root)

    def create_retry_run(self, parent_run_id: str, artifact_root: str) -> dict[str, Any]:
        """Create a new, auditable run for an explicit retry without touching prior evidence."""

        return self._runs.create_retry_run(parent_run_id, artifact_root)

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        """Cancel a queued or active run without deleting its audit evidence.

        Cancellation is cooperative for a stage that is already inside a browser
        call: workers observe the terminal run state before claiming/finalising
        another stage.  Queued work is made unclaimable immediately.
        """

        return self._runs.cancel_run(run_id)

    def resume_run(self, run_id: str) -> dict[str, Any]:
        """Requeue an interrupted durable job only when no action can be replayed.

        Failed/cancelled runs intentionally require the auditable child-run retry
        endpoint.  This operation is solely for a queued/recoverable root job.
        """

        return self._runs.resume_run(run_id)

    def delete_empty_terminal_run(self, run_id: str) -> bool:
        """Delete a terminal run only when it contains no retained artifacts.

        This deliberately keeps failed runs with screenshots, traces, reports, or
        videos: those records are essential repair evidence.  It is safe for the
        UI's "clear empty failures" control and never deletes the parent request.
        """

        return self._runs.delete_empty_terminal_run(run_id)

    def purge_empty_terminal_runs(self, *, older_than_seconds: int = 0) -> list[str]:
        """Retention sweep for truly empty terminal runs, returning deleted IDs."""

        return self._runs.purge_empty_terminal_runs(older_than_seconds=older_than_seconds)

    def prepare_targeted_retry(self, run_id: str, start_stage: str) -> None:
        """Mark inherited stages complete and queue only the safe repair boundary."""

        self._jobs.prepare_targeted_retry(run_id, start_stage)

    def copy_run_evidence(
        self, parent_run_id: str, retry_run_id: str, *, through_stage: str
    ) -> None:
        """Copy immutable database evidence needed by a targeted child retry."""

        self._runs.copy_run_evidence(parent_run_id, retry_run_id, through_stage=through_stage)

    def enqueue_job(self, run_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist a worker message before it is published to a broker."""

        return self._jobs.enqueue_job(run_id, kind, payload)

    def _ensure_stage_jobs(self, run_id: str) -> None:
        """Create durable checkpoints before a worker starts any provider work."""

        self._jobs._ensure_stage_jobs(run_id)

    def ensure_stage_jobs(self, run_id: str) -> None:
        """Idempotently provision the stage ledger for every execution entry point.

        API requests normally get this through :meth:`enqueue_job`, but an
        operator may deliberately invoke the shared service from a supervisor
        or command-line acceptance runner.  Those runs need exactly the same
        recoverable checkpoint ledger; otherwise a process loss would leave
        artifacts without durable stage ownership.
        """

        self._jobs.ensure_stage_jobs(run_id)

    def update_stage_job(
        self, run_id: str, stage: str, *, status: str, error_code: str | None = None
    ) -> None:
        self._jobs.update_stage_job(run_id, stage, status=status, error_code=error_code)

    def claim_stage_job(self, run_id: str, stage: str) -> dict[str, Any] | None:
        """Atomically claim one ready stage.

        A stage cannot be claimed until every earlier stage has reached a terminal
        successful state. This database-enforced compare-and-set keeps duplicate
        or out-of-order Dramatiq deliveries safe across local SQLite and PostgreSQL.
        """

        return self._jobs.claim_stage_job(run_id, stage)

    def stage_job(self, run_id: str, stage: str) -> dict[str, Any]:
        return self._jobs.stage_job(run_id, stage)

    def heartbeat_stage_job(self, run_id: str, stage: str) -> None:
        """Refresh the liveness lease without changing lifecycle status."""

        self._jobs.heartbeat_stage_job(run_id, stage)

    def claim_next_stage_job(self) -> dict[str, Any] | None:
        """Claim the oldest stage whose predecessor is complete for the local worker."""

        return self._jobs.claim_next_stage_job()

    def stage_jobs(self, run_id: str) -> list[dict[str, Any]]:
        return self._jobs.stage_jobs(run_id)

    def fail_active_stage_jobs(self, run_id: str, error_code: str) -> None:
        self._jobs.fail_active_stage_jobs(run_id, error_code)

    def claim_job(self, job_id: str) -> dict[str, Any] | None:
        """Atomically claim one queued/recoverable job without double execution."""

        return self._jobs.claim_job(job_id)

    def claim_next_job(self) -> dict[str, Any] | None:
        """Claim the oldest pending job for the local durable-worker development mode."""

        return self._jobs.claim_next_job()

    def fail_orphaned_jobs(self) -> int:
        """Terminally close outbox records whose run was removed by retention.

        Older SQLite databases can contain these records from development tests
        that delete empty runs.  An orphan has no request or run evidence and can
        never be executed safely, so it must not occupy a worker lease.
        """

        return self._jobs.fail_orphaned_jobs()

    def recover_stale_jobs(self, max_running_seconds: int = 1_800) -> int:
        """Make abandoned worker claims visible for an explicit, auditable retry."""

        return self._jobs.recover_stale_jobs(max_running_seconds)

    def finish_job(self, job_id: str, *, status: str, error_code: str | None = None) -> None:
        self._jobs.finish_job(job_id, status=status, error_code=error_code)

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._jobs.get_job(job_id)

    def update_run(
        self, run_id: str, *, stage: str, status: str, error_code: str | None = None
    ) -> dict[str, Any]:
        return self._runs.update_run(run_id, stage=stage, status=status, error_code=error_code)

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._runs.get_run(run_id)

    def list_runs(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Return recent runs with their request context, never provider secrets."""

        return self._runs.list_runs(limit, offset)

    def run_details(self, run_id: str) -> dict[str, Any]:
        """Collect the durable, frontend-safe evidence produced by one run."""

        return self._runs.run_details(run_id)

    def _job_for_run(self, run_id: str) -> dict[str, Any] | None:
        return self._jobs._job_for_run(run_id)

    def job_for_run(self, run_id: str) -> dict[str, Any] | None:
        """Return non-secret generation configuration retained for an auditable retry."""

        return self._jobs.job_for_run(run_id)

    def get_request(self, request_id: str) -> dict[str, Any]:
        return self._runs.get_request(request_id)

    def upsert_knowledge(
        self, product_key: str, evidence: dict[str, Any], confidence: float
    ) -> None:
        self._knowledge.upsert_knowledge(product_key, evidence, confidence)

    def upsert_page_knowledge(
        self, product_key: str, pages: list[dict[str, Any]], *, confidence: float
    ) -> None:
        """Persist page-level knowledge separately from the product snapshot.

        Run artifacts remain the immutable audit record. This table is the
        reusable, freshness-stamped index used only after discovery has
        re-grounded it on the live product.
        """

        self._knowledge.upsert_page_knowledge(product_key, pages, confidence=confidence)

    def record_successful_actions(self, product_key: str, events: list[dict[str, Any]]) -> None:
        """Cache compact, non-secret action evidence for later DOM re-grounding."""

        self._knowledge.record_successful_actions(product_key, events)

    def fresh_knowledge(
        self, product_key: str, max_age_seconds: int = 86_400
    ) -> dict[str, Any] | None:
        return self._knowledge.fresh_knowledge(product_key, max_age_seconds)

    def knowledge_versions(self, product_key: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """List immutable knowledge snapshots newest-first for audit/reuse decisions."""

        return self._knowledge.knowledge_versions(product_key, limit=limit)

    def save_understanding_preview(
        self, product_key: str, prompt: str, payload: dict[str, Any]
    ) -> None:
        """Persist a non-secret preflight result for prompt assistance reuse."""

        self._knowledge.save_understanding_preview(product_key, prompt, payload)

    def fresh_understanding_preview(
        self, product_key: str, prompt: str, max_age_seconds: int = 86_400
    ) -> dict[str, Any] | None:
        """Return a recent preflight payload, or None when it needs re-grounding."""

        return self._knowledge.fresh_understanding_preview(product_key, prompt, max_age_seconds)

    def invalidate_knowledge(self, product_key: str) -> bool:
        """Remove a reusable product snapshot and all page snapshots atomically.

        Immutable run artifacts remain untouched; the next discovery is therefore
        forced to re-ground live knowledge rather than reusing stale cache data.
        """

        return self._knowledge.invalidate_knowledge(product_key)
