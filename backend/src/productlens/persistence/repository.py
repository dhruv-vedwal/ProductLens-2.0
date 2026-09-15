from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from productlens.urls import canonical_product_url


def _canonical_product_key(value: str) -> str:
    """Normalize equivalent product URLs before indexing reusable knowledge."""
    return canonical_product_url(value)


class _DatabaseRow(Mapping[str, Any]):
    """Small cross-dialect row facade used by the legacy repository methods."""

    def __init__(self, row: Any):
        self._values = dict(row._mapping)
        self._ordered = tuple(self._values.values())

    def __getitem__(self, key: str | int) -> Any:
        return self._ordered[key] if isinstance(key, int) else self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


class _DatabaseResult:
    def __init__(self, result: Any):
        self._result = result
        self.rowcount = result.rowcount

    def fetchone(self) -> _DatabaseRow | None:
        row = self._result.fetchone()
        return _DatabaseRow(row) if row is not None else None

    def fetchall(self) -> list[_DatabaseRow]:
        return [_DatabaseRow(row) for row in self._result.fetchall()]


class _PostgresConnection:
    """DB-API-shaped facade that makes the proven repository API portable.

    The repository intentionally retains its explicit commits and compact SQL. This
    facade converts its SQLite qmark parameters to SQLAlchemy named parameters,
    while PostgreSQL owns locking, transactions, and concurrent worker claims.
    """

    def __init__(self, database_url: str):
        # Bare PostgreSQL URLs make SQLAlchemy select psycopg2. ProductLens
        # installs psycopg v3, so normalize operator-provided URLs here.
        if database_url.startswith("postgres://"):
            database_url = "postgresql+psycopg://" + database_url.removeprefix("postgres://")
        elif database_url.startswith("postgresql://"):
            database_url = "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
        self.engine = create_engine(database_url, pool_pre_ping=True, future=True)
        self._connection = self.engine.connect()

    @staticmethod
    def _statement(statement: str, parameters: tuple[Any, ...] | dict[str, Any] | None):
        if not isinstance(parameters, tuple):
            return text(statement), parameters or {}
        binds: dict[str, Any] = {}
        parts = statement.split("?")
        if len(parts) - 1 != len(parameters):
            raise ValueError("database placeholder count does not match parameters")
        rendered = [parts[0]]
        for index, part in enumerate(parts[1:]):
            name = f"p{index}"
            binds[name] = parameters[index]
            rendered.extend((f":{name}", part))
        return text("".join(rendered)), binds

    def execute(self, statement: str, parameters: tuple[Any, ...] | dict[str, Any] | None = None):
        query, binds = self._statement(statement, parameters)
        return _DatabaseResult(self._connection.execute(query, binds))

    def executemany(self, statement: str, parameters: list[tuple[Any, ...] | dict[str, Any]]):
        if not parameters:
            return None
        query, _first = self._statement(statement, parameters[0])
        if isinstance(parameters[0], tuple):
            rendered = []
            for values in parameters:
                _, binds = self._statement(statement, values)
                rendered.append(binds)
            return _DatabaseResult(self._connection.execute(query, rendered))
        return _DatabaseResult(self._connection.execute(query, parameters))

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self) -> None:
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()
        self.engine.dispose()


class RunRepository:
    """Idempotent job/run persistence with no secret-bearing payload columns."""

    def __init__(self, database: Path | str):
        database_url = str(database)
        self.dialect = (
            "postgresql" if database_url.startswith(("postgresql", "postgres://")) else "sqlite"
        )
        if self.dialect == "postgresql":
            self.connection = _PostgresConnection(database_url)
        else:
            database_path = Path(database_url.removeprefix("sqlite:///"))
            database_path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(database_path, check_same_thread=False)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS demo_requests (
              id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, url TEXT NOT NULL,
              objective TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
              project_id TEXT
            );
            CREATE TABLE IF NOT EXISTS demo_runs (
              id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES demo_requests(id),
              stage TEXT NOT NULL, status TEXT NOT NULL, artifact_root TEXT NOT NULL,
              error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS demo_attempts (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), ordinal INTEGER NOT NULL,
              stage TEXT NOT NULL, status TEXT NOT NULL, failure_code TEXT, created_at TEXT NOT NULL,
              UNIQUE(run_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS product_knowledge (
              id TEXT PRIMARY KEY, product_key TEXT UNIQUE NOT NULL, version INTEGER NOT NULL,
              evidence_json TEXT NOT NULL, confidence REAL NOT NULL, last_verified_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS knowledge_versions (
              id TEXT PRIMARY KEY, product_key TEXT NOT NULL, version INTEGER NOT NULL,
              fingerprint TEXT NOT NULL, evidence_json TEXT NOT NULL, confidence REAL NOT NULL,
              captured_at TEXT NOT NULL, UNIQUE(product_key, version)
            );
            CREATE TABLE IF NOT EXISTS demo_plans (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workflow_steps (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), ordinal INTEGER NOT NULL, payload_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS browser_sessions (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), provider TEXT NOT NULL, external_session_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS run_lineage (parent_run_id TEXT NOT NULL REFERENCES demo_runs(id), retry_run_id TEXT PRIMARY KEY REFERENCES demo_runs(id), created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS interaction_events (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), ordinal INTEGER NOT NULL, payload_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS presentation_plans (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS narration_scripts (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS quality_reports (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), kind TEXT NOT NULL, location TEXT NOT NULL, created_at TEXT NOT NULL);
            -- A compact, queryable mirror of the architectural JSON evidence.
            -- Files remain the immutable renderer inputs; this ledger makes a
            -- crashed/resumed run inspectable without scanning its directory.
            CREATE TABLE IF NOT EXISTS run_artifact_documents (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id),
              kind TEXT NOT NULL, payload_json TEXT NOT NULL, source_path TEXT NOT NULL,
              sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, UNIQUE(run_id, kind)
            );
            CREATE TABLE IF NOT EXISTS provider_configs (
              id TEXT PRIMARY KEY, provider_type TEXT NOT NULL, name TEXT NOT NULL,
              credential_reference TEXT, active INTEGER NOT NULL, priority INTEGER NOT NULL,
              settings_json TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(provider_type, name)
            );
            CREATE TABLE IF NOT EXISTS generation_jobs (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES demo_runs(id),
              kind TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL,
              delivery_attempts INTEGER NOT NULL DEFAULT 0, error_code TEXT,
              queued_at TEXT NOT NULL, claimed_at TEXT, completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS generation_stage_jobs (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id),
              stage TEXT NOT NULL, ordinal INTEGER NOT NULL, status TEXT NOT NULL,
              error_code TEXT, delivery_attempts INTEGER NOT NULL DEFAULT 0, claimed_at TEXT,
              started_at TEXT, completed_at TEXT, heartbeat_at TEXT, updated_at TEXT NOT NULL,
              UNIQUE(run_id, stage), UNIQUE(run_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS users (
              id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, display_name TEXT, password_hash TEXT,
              theme_preference TEXT NOT NULL DEFAULT 'system', created_at TEXT NOT NULL, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS auth_sessions (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL,
              revoked_at TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
              id TEXT PRIMARY KEY, owner_id TEXT REFERENCES users(id), name TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS page_knowledge (
              id TEXT PRIMARY KEY, product_knowledge_id TEXT NOT NULL REFERENCES product_knowledge(id),
              url TEXT NOT NULL, evidence_json TEXT NOT NULL, confidence REAL NOT NULL, last_verified_at TEXT NOT NULL,
              UNIQUE(product_knowledge_id, url)
            );
            CREATE TABLE IF NOT EXISTS understanding_previews (
              id TEXT PRIMARY KEY, product_key TEXT NOT NULL, prompt_hash TEXT NOT NULL,
              payload_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(product_key, prompt_hash)
            );
            CREATE TABLE IF NOT EXISTS form_schemas (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id),
              payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS synthetic_datasets (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id),
              payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audio_assets (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id),
              location TEXT NOT NULL, duration_seconds REAL, provider TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS video_renders (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id),
              location TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS provider_calls (
              id TEXT PRIMARY KEY, run_id TEXT REFERENCES demo_runs(id), provider TEXT NOT NULL,
              operation TEXT NOT NULL, status TEXT NOT NULL, duration_ms INTEGER, error_code TEXT,
              model TEXT, cost_class TEXT, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_demo_runs_created_at ON demo_runs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_demo_runs_request_id ON demo_runs(request_id);
            CREATE INDEX IF NOT EXISTS idx_demo_requests_project_id ON demo_requests(project_id);
            CREATE INDEX IF NOT EXISTS idx_demo_attempts_run_id ON demo_attempts(run_id, ordinal);
            CREATE INDEX IF NOT EXISTS idx_artifacts_run_id ON artifacts(run_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_run_artifact_documents_run_id ON run_artifact_documents(run_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_generation_jobs_status ON generation_jobs(status, queued_at);
            CREATE INDEX IF NOT EXISTS idx_provider_calls_run_id ON provider_calls(run_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_knowledge_versions_key ON knowledge_versions(product_key, version DESC);
            CREATE INDEX IF NOT EXISTS idx_projects_owner_id ON projects(owner_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_id ON auth_sessions(user_id, expires_at);
            """
        )
        if self.dialect == "sqlite":
            self._upgrade_sqlite_schema()
        self.connection.commit()

    def _upgrade_sqlite_schema(self) -> None:
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(demo_requests)")
        }
        if "project_id" not in columns:
            self.connection.execute("ALTER TABLE demo_requests ADD COLUMN project_id TEXT")
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_demo_requests_project_id ON demo_requests(project_id)"
            )
        stage_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(generation_stage_jobs)")
        }
        # Keep direct SQLite bootstrapping compatible with databases created by
        # releases before the stage-delivery migration. Alembic performs the
        # same forward-only schema upgrade in deployed environments.
        if stage_columns and "delivery_attempts" not in stage_columns:
            self.connection.execute(
                "ALTER TABLE generation_stage_jobs ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0"
            )
        if stage_columns and "claimed_at" not in stage_columns:
            self.connection.execute("ALTER TABLE generation_stage_jobs ADD COLUMN claimed_at TEXT")
        if stage_columns and "heartbeat_at" not in stage_columns:
            self.connection.execute(
                "ALTER TABLE generation_stage_jobs ADD COLUMN heartbeat_at TEXT"
            )
        user_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(users)")}
        if "password_hash" not in user_columns:
            self.connection.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        if "theme_preference" not in user_columns:
            self.connection.execute(
                "ALTER TABLE users ADD COLUMN theme_preference TEXT NOT NULL DEFAULT 'system'"
            )
        if "updated_at" not in user_columns:
            self.connection.execute("ALTER TABLE users ADD COLUMN updated_at TEXT")
        provider_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(provider_calls)")
        }
        if provider_columns and "model" not in provider_columns:
            self.connection.execute("ALTER TABLE provider_calls ADD COLUMN model TEXT")
        if provider_columns and "cost_class" not in provider_columns:
            self.connection.execute("ALTER TABLE provider_calls ADD COLUMN cost_class TEXT")

    def close(self) -> None:
        """Release SQLite handles for short-lived workers and test processes."""
        self.connection.close()

    def start_attempt(self, run_id: str, stage: str) -> int:
        ordinal = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM demo_attempts WHERE run_id=?", (run_id,)
            ).fetchone()[0]
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

    def save_json_artifact(self, table: str, run_id: str, payload: dict[str, Any]) -> None:
        allowed = {"demo_plans", "presentation_plans", "narration_scripts", "quality_reports"}
        if table not in allowed:
            raise ValueError("unsupported JSON artifact table")
        self.connection.execute(
            f"INSERT INTO {table} VALUES (?, ?, ?, ?)",
            (str(uuid4()), run_id, json.dumps(payload), datetime.now(UTC).isoformat()),
        )
        self.connection.commit()

    def replace_json_artifact(self, table: str, run_id: str, payload: dict[str, Any]) -> None:
        """Replace a singleton stage artifact on a same-run recovery attempt."""
        allowed = {"demo_plans", "presentation_plans", "narration_scripts", "quality_reports"}
        if table not in allowed:
            raise ValueError("unsupported JSON artifact table")
        self.connection.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
        self.connection.execute(
            f"INSERT INTO {table} VALUES (?, ?, ?, ?)",
            (str(uuid4()), run_id, json.dumps(payload), datetime.now(UTC).isoformat()),
        )
        self.connection.commit()

    def save_workflow_steps(self, run_id: str, steps: list[dict[str, Any]]) -> None:
        self.connection.executemany(
            "INSERT INTO workflow_steps VALUES (?, ?, ?, ?)",
            [
                (str(uuid4()), run_id, ordinal, json.dumps(step))
                for ordinal, step in enumerate(steps)
            ],
        )
        self.connection.commit()

    def save_interaction_events(self, run_id: str, events: list[dict[str, Any]]) -> None:
        self.connection.executemany(
            "INSERT INTO interaction_events VALUES (?, ?, ?, ?)",
            [
                (str(uuid4()), run_id, ordinal, json.dumps(event))
                for ordinal, event in enumerate(events)
            ],
        )
        self.connection.commit()

    def replace_interaction_events(self, run_id: str, events: list[dict[str, Any]]) -> None:
        self.connection.execute("DELETE FROM interaction_events WHERE run_id=?", (run_id,))
        self.connection.executemany(
            "INSERT INTO interaction_events VALUES (?, ?, ?, ?)",
            [
                (str(uuid4()), run_id, ordinal, json.dumps(event))
                for ordinal, event in enumerate(events)
            ],
        )
        self.connection.commit()

    def save_form_schema(self, run_id: str, schema: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO form_schemas VALUES (?, ?, ?, ?)",
            (str(uuid4()), run_id, json.dumps(schema), datetime.now(UTC).isoformat()),
        )
        self.connection.commit()

    def save_synthetic_dataset(self, run_id: str, dataset: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO synthetic_datasets VALUES (?, ?, ?, ?)",
            (str(uuid4()), run_id, json.dumps(dataset), datetime.now(UTC).isoformat()),
        )
        self.connection.commit()

    def save_browser_session(
        self, run_id: str, provider: str, external_session_id: str | None, status: str
    ) -> None:
        self.connection.execute(
            "INSERT INTO browser_sessions VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                run_id,
                provider,
                external_session_id,
                status,
                datetime.now(UTC).isoformat(),
            ),
        )
        self.connection.commit()

    def save_location(self, run_id: str, kind: str, location: str) -> None:
        self.connection.execute(
            "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?)",
            (str(uuid4()), run_id, kind, location, datetime.now(UTC).isoformat()),
        )
        self.connection.commit()

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
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """INSERT INTO run_artifact_documents
               (id, run_id, kind, payload_json, source_path, sha256, byte_size, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id, kind) DO UPDATE SET
                 payload_json=excluded.payload_json, source_path=excluded.source_path,
                 sha256=excluded.sha256, byte_size=excluded.byte_size,
                 updated_at=excluded.updated_at""",
            (
                str(uuid4()),
                run_id,
                kind,
                json.dumps(payload),
                source_path,
                sha256,
                byte_size,
                now,
                now,
            ),
        )
        self.connection.commit()

    def persist_run_documents(self, run_id: str, run_root: Path) -> list[str]:
        """Mirror all required architectural JSON evidence written so far.

        It is safe to call after every durable stage: absent future-stage
        files are ignored and a same-kind snapshot is atomically replaced.
        """
        paths: dict[str, str] = {
            "objective": "objective.json",
            "objective_understanding": "discovery/objective-understanding.json",
            "exploration_report": "exploration-report.json",
            "product_knowledge": "discovery/product-knowledge.json",
            "discovery_capability_resolutions": "discovery/capability-resolutions.json",
            "feature_graph": "feature-graph.json",
            "relevance_graph": "discovery/relevance-graph.json",
            "candidate_flows": "candidate-flows.json",
            "stagehand_observation": "discovery/stagehand-observation.json",
            "viewport_decision": "discovery/viewport-decision.json",
            "demo_brief": "planning/demo-brief.json",
            "validated_state_graph": "planning/validated-state-graph.json",
            "capability_resolutions": "planning/capability-resolutions.json",
            "demo_plan": "plan.json",
            # Runtime adaptation is an auditable plan version, not an
            # in-memory worker detail.  Mirror it when present so resumable
            # workers and API clients can distinguish the planned and
            # actually executed suffix.
            "effective_plan": "plan-effective.json",
            "adapted_state_graph": "planning/adapted-state-graph.json",
            "replan_decisions": "execution/replan-decisions.json",
            "editorial_brief": "presentation/editorial-brief.json",
            "storyboard": "presentation/storyboard.json",
            "validated_scene_plan": "presentation/validated-scene-plan.json",
            "actual_flow_storyboard": "presentation/actual-flow-storyboard.json",
            "editorial_script": "presentation/narration-script.json",
            "demo_trace": "execution/trace.json",
            "state_snapshots": "execution/state-snapshots.json",
            "action_attempts": "execution/action-attempts.json",
            "verification_results": "execution/verification-results.json",
            "execution_report": "qa/execution-report.json",
            "source_timing_alignment": "execution/source-timing-alignment.json",
            "source_edit_plan": "presentation/source-edit-plan.json",
            "repair_decision": "qa/repair-decision.json",
            "delivery_report": "qa/delivery-report.json",
            "editorial_report": "qa/editorial-report.json",
            "journey_report": "quality/journey-report.json",
            "gap_report": "qa/gap-report.json",
            "artifact_manifest": "artifact-manifest.json",
        }
        persisted: list[str] = []
        for kind, relative in paths.items():
            source = run_root / relative
            if not source.is_file() or source.stat().st_size == 0:
                continue
            try:
                raw = source.read_bytes()
                payload = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                # A partially-created/non-JSON file is never evidence.
                continue
            self.upsert_run_artifact_document(
                run_id,
                kind=kind,
                payload=payload,
                source_path=relative,
                sha256=hashlib.sha256(raw).hexdigest(),
                byte_size=len(raw),
            )
            persisted.append(kind)
        return persisted

    def run_artifact_documents(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT kind, payload_json, source_path, sha256, byte_size, created_at, updated_at
               FROM run_artifact_documents WHERE run_id=? ORDER BY kind""",
            (run_id,),
        ).fetchall()
        documents: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            documents.append(item)
        return documents

    def save_audio_asset(
        self,
        run_id: str,
        location: str,
        *,
        duration_seconds: float | None,
        provider: str | None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO audio_assets VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                run_id,
                location,
                duration_seconds,
                provider,
                datetime.now(UTC).isoformat(),
            ),
        )
        self.connection.commit()

    def save_video_render(
        self, run_id: str, location: str, *, status: str, metadata: dict[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO video_renders VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                run_id,
                location,
                status,
                json.dumps(metadata),
                datetime.now(UTC).isoformat(),
            ),
        )
        self.connection.commit()

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
        self.connection.execute(
            "INSERT INTO provider_calls "
            "(id, run_id, provider, operation, status, duration_ms, error_code, model, cost_class, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                run_id,
                provider,
                operation,
                status,
                duration_ms,
                error_code,
                model,
                cost_class,
                datetime.now(UTC).isoformat(),
            ),
        )
        self.connection.commit()

    def locations(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT kind, location, created_at FROM artifacts WHERE run_id=? ORDER BY created_at",
                (run_id,),
            ).fetchall()
        ]

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
        if credential_reference and (
            credential_reference.startswith("sk-") or " " in credential_reference
        ):
            raise ValueError("provider credential must be a secret reference, not a key")
        self.connection.execute(
            """INSERT INTO provider_configs VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_type, name) DO UPDATE SET credential_reference=excluded.credential_reference,
            active=excluded.active, priority=excluded.priority, settings_json=excluded.settings_json,
            updated_at=excluded.updated_at""",
            (
                str(uuid4()),
                provider_type,
                name,
                credential_reference,
                int(active),
                priority,
                json.dumps(settings or {}),
                datetime.now(UTC).isoformat(),
            ),
        )
        self.connection.commit()

    def provider_configs(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT provider_type, name, credential_reference, active, priority, settings_json, updated_at "
            "FROM provider_configs ORDER BY provider_type, priority, name"
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["active"] = bool(item["active"])
            item["settings"] = json.loads(item.pop("settings_json"))
            result.append(item)
        return result

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
        return dict(
            self.connection.execute(
                "SELECT * FROM demo_requests WHERE id = ?", (identifier,)
            ).fetchone()
        )

    def create_project(self, name: str, *, owner_id: str | None = None) -> dict[str, Any]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("project name is required")
        now = datetime.now(UTC).isoformat()
        identifier = str(uuid4())
        self.connection.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?)",
            (identifier, owner_id, normalized[:160], now, now),
        )
        self.connection.commit()
        return self.get_project(identifier)

    def create_user(
        self, *, email: str, password_hash: str, display_name: str | None
    ) -> dict[str, Any]:
        normalized = email.strip().lower()
        if not normalized:
            raise ValueError("email is required")
        now = datetime.now(UTC).isoformat()
        identifier = str(uuid4())
        try:
            self.connection.execute(
                "INSERT INTO users (id, email, display_name, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    normalized,
                    (display_name or "").strip()[:120] or None,
                    password_hash,
                    now,
                    now,
                ),
            )
        except (sqlite3.IntegrityError, IntegrityError) as error:
            raise ValueError("an account with that email already exists") from error
        self.connection.commit()
        return self.get_user(identifier)

    def get_user(self, user_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise KeyError(user_id)
        return dict(row)

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM users WHERE email=?", (email.strip().lower(),)
        ).fetchone()
        return dict(row) if row else None

    def update_user_preferences(self, user_id: str, *, theme_preference: str) -> dict[str, Any]:
        self.connection.execute(
            "UPDATE users SET theme_preference=?, updated_at=? WHERE id=?",
            (theme_preference, datetime.now(UTC).isoformat(), user_id),
        )
        self.connection.commit()
        return self.get_user(user_id)

    def create_session(self, user_id: str, expires_at: str) -> dict[str, Any]:
        identifier = str(uuid4())
        self.connection.execute(
            "INSERT INTO auth_sessions VALUES (?, ?, ?, ?, ?)",
            (identifier, user_id, expires_at, None, datetime.now(UTC).isoformat()),
        )
        self.connection.commit()
        return {"id": identifier, "user_id": user_id, "expires_at": expires_at}

    def active_session(self, session_id: str, user_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM auth_sessions WHERE id=? AND user_id=? AND revoked_at IS NULL AND expires_at > ?",
            (session_id, user_id, datetime.now(UTC).isoformat()),
        ).fetchone()
        return row is not None

    def revoke_session(self, session_id: str, user_id: str) -> None:
        self.connection.execute(
            "UPDATE auth_sessions SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
            (datetime.now(UTC).isoformat(), session_id, user_id),
        )
        self.connection.commit()

    def get_project(self, project_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise KeyError(project_id)
        return dict(row)

    def list_projects(self, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        query = """SELECT projects.*, COUNT(requests.id) AS request_count
            FROM projects LEFT JOIN demo_requests AS requests ON requests.project_id = projects.id"""
        params: tuple[Any, ...] = ()
        if owner_id is not None:
            query += " WHERE projects.owner_id=?"
            params = (owner_id,)
        query += " GROUP BY projects.id ORDER BY projects.updated_at DESC, projects.created_at DESC"
        rows = self.connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_project_for_user(self, project_id: str, user_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM projects WHERE id=? AND owner_id=?", (project_id, user_id)
        ).fetchone()
        if not row:
            raise KeyError(project_id)
        return dict(row)

    def rename_project_for_user(self, project_id: str, user_id: str, name: str) -> dict[str, Any]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("project name is required")
        self.get_project_for_user(project_id, user_id)
        self.connection.execute(
            "UPDATE projects SET name=?, updated_at=? WHERE id=? AND owner_id=?",
            (normalized[:160], datetime.now(UTC).isoformat(), project_id, user_id),
        )
        self.connection.commit()
        return self.get_project_for_user(project_id, user_id)

    def rename_project(self, project_id: str, name: str) -> dict[str, Any]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("project name is required")
        self.get_project(project_id)
        self.connection.execute(
            "UPDATE projects SET name=?, updated_at=? WHERE id=?",
            (normalized[:160], datetime.now(UTC).isoformat(), project_id),
        )
        self.connection.commit()
        return self.get_project(project_id)

    def ensure_user_project(self, user_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM projects WHERE owner_id=? AND name='My Product Demos' LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else self.create_project("My Product Demos", owner_id=user_id)

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

    def list_projects_legacy(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT projects.*, COUNT(requests.id) AS request_count
            FROM projects LEFT JOIN demo_requests AS requests ON requests.project_id = projects.id
            GROUP BY projects.id ORDER BY projects.updated_at DESC, projects.created_at DESC"""
        ).fetchall()
        return [dict(row) for row in rows]

    def ensure_local_project(self) -> dict[str, Any]:
        """Create a transparent local-only workspace for an unauthenticated studio."""
        row = self.connection.execute(
            "SELECT * FROM projects WHERE owner_id IS NULL AND name='My Product Demos' LIMIT 1"
        ).fetchone()
        return dict(row) if row else self.create_project("My Product Demos")

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
        return self.get_job(job["id"])

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
        return cursor.rowcount

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
        return cursor.rowcount

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

    def get_request(self, request_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM demo_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if not row:
            raise KeyError(request_id)
        return dict(row)

    def upsert_knowledge(
        self, product_key: str, evidence: dict[str, Any], confidence: float
    ) -> None:
        product_key = _canonical_product_key(product_key)
        now = datetime.now(UTC).isoformat()
        row = self.connection.execute(
            "SELECT version FROM product_knowledge WHERE product_key=?", (product_key,)
        ).fetchone()
        version = (row["version"] + 1) if row else 1
        if row:
            existing = self.connection.execute(
                "SELECT evidence_json FROM product_knowledge WHERE product_key=?", (product_key,)
            ).fetchone()
            existing_evidence = json.loads(existing["evidence_json"]) if existing else {}
            # Discovery refreshes route/DOM evidence, whereas successful action
            # evidence is accumulated after production execution. Do not discard
            # that cache merely because a discovery payload does not contain it.
            if "successful_actions" in existing_evidence and "successful_actions" not in evidence:
                evidence = {
                    **evidence,
                    "successful_actions": existing_evidence["successful_actions"],
                }
        evidence_payload = json.dumps(evidence)
        self.connection.execute(
            "INSERT INTO product_knowledge(id, product_key, version, evidence_json, confidence, last_verified_at) VALUES(?,?,?,?,?,?) ON CONFLICT(product_key) DO UPDATE SET version=excluded.version,evidence_json=excluded.evidence_json,confidence=excluded.confidence,last_verified_at=excluded.last_verified_at",
            (str(uuid4()), product_key, version, evidence_payload, confidence, now),
        )
        # Keep an immutable version ledger alongside the current snapshot. This
        # makes freshness/re-grounding auditable without overwriting the only
        # copy of older product knowledge.
        fingerprint = str(
            evidence.get("product_fingerprint")
            or evidence.get("fingerprint")
            or hashlib.sha256(evidence_payload.encode("utf-8")).hexdigest()[:32]
        )
        self.connection.execute(
            "INSERT INTO knowledge_versions(id, product_key, version, fingerprint, evidence_json, confidence, captured_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(product_key, version) DO UPDATE SET fingerprint=excluded.fingerprint,evidence_json=excluded.evidence_json,confidence=excluded.confidence,captured_at=excluded.captured_at",
            (str(uuid4()), product_key, version, fingerprint, evidence_payload, confidence, now),
        )
        self.connection.commit()

    def upsert_page_knowledge(
        self, product_key: str, pages: list[dict[str, Any]], *, confidence: float
    ) -> None:
        """Persist page-level knowledge separately from the product snapshot.

        Run artifacts remain the immutable audit record. This table is the
        reusable, freshness-stamped index used only after discovery has
        re-grounded it on the live product.
        """
        product_key = _canonical_product_key(product_key)
        product = self.connection.execute(
            "SELECT id FROM product_knowledge WHERE product_key=?", (product_key,)
        ).fetchone()
        if not product:
            raise KeyError(f"product knowledge has not been created: {product_key}")
        now = datetime.now(UTC).isoformat()
        for page in pages:
            url = _canonical_product_key(str(page.get("url", "")).strip())
            if not url:
                continue
            self.connection.execute(
                """INSERT INTO page_knowledge(id, product_knowledge_id, url, evidence_json, confidence, last_verified_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(product_knowledge_id, url) DO UPDATE SET
                  evidence_json=excluded.evidence_json, confidence=excluded.confidence,
                  last_verified_at=excluded.last_verified_at""",
                (str(uuid4()), product["id"], url, json.dumps(page), confidence, now),
            )
        self.connection.commit()

    def record_successful_actions(self, product_key: str, events: list[dict[str, Any]]) -> None:
        """Cache compact, non-secret action evidence for later DOM re-grounding."""
        product_key = _canonical_product_key(product_key)
        row = self.connection.execute(
            "SELECT evidence_json, confidence FROM product_knowledge WHERE product_key=?",
            (product_key,),
        ).fetchone()
        if not row:
            return
        evidence = json.loads(row["evidence_json"])
        cached = list(evidence.get("successful_actions", []))
        by_signature = {
            (
                item.get("kind"),
                item.get("target", {}).get("selector"),
                item.get("target", {}).get("name"),
            ): item
            for item in cached
        }
        for event in events:
            target = event.get("target") or {}
            if not event.get("success") or not target:
                continue
            # Values, before/after state, screenshots and URLs can contain user
            # data. A cache only needs semantic target identity and operation kind.
            item = {
                "kind": event.get("kind"),
                "intent": event.get("intent", ""),
                "target": {
                    key: target[key]
                    for key in ("name", "selector", "role", "test_id", "text")
                    if target.get(key) is not None
                },
            }
            signature = (item["kind"], item["target"].get("selector"), item["target"].get("name"))
            by_signature[signature] = item
        evidence["successful_actions"] = list(by_signature.values())[-60:]
        self.upsert_knowledge(product_key, evidence, float(row["confidence"]))

    def fresh_knowledge(
        self, product_key: str, max_age_seconds: int = 86_400
    ) -> dict[str, Any] | None:
        product_key = _canonical_product_key(product_key)
        row = self.connection.execute(
            "SELECT evidence_json, confidence, last_verified_at FROM product_knowledge WHERE product_key=?",
            (product_key,),
        ).fetchone()
        if not row:
            return None
        verified = datetime.fromisoformat(row["last_verified_at"])
        if (datetime.now(UTC) - verified).total_seconds() > max_age_seconds:
            return None
        return {
            "evidence": json.loads(row["evidence_json"]),
            "confidence": row["confidence"],
            "last_verified_at": row["last_verified_at"],
            "version": self.connection.execute(
                "SELECT version FROM product_knowledge WHERE product_key=?", (product_key,)
            ).fetchone()["version"],
        }

    def knowledge_versions(self, product_key: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """List immutable knowledge snapshots newest-first for audit/reuse decisions."""
        product_key = _canonical_product_key(product_key)
        rows = self.connection.execute(
            "SELECT id, product_key, version, fingerprint, confidence, captured_at FROM knowledge_versions WHERE product_key=? ORDER BY version DESC LIMIT ?",
            (product_key, max(1, min(100, int(limit)))),
        ).fetchall()
        return [dict(row) for row in rows]

    def save_understanding_preview(
        self, product_key: str, prompt: str, payload: dict[str, Any]
    ) -> None:
        """Persist a non-secret preflight result for prompt assistance reuse."""
        product_key = _canonical_product_key(product_key)
        prompt_hash = hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """INSERT INTO understanding_previews(id, product_key, prompt_hash, payload_json, created_at, updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(product_key, prompt_hash) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
            (str(uuid4()), product_key, prompt_hash, json.dumps(payload), now, now),
        )
        self.connection.commit()

    def fresh_understanding_preview(
        self, product_key: str, prompt: str, max_age_seconds: int = 86_400
    ) -> dict[str, Any] | None:
        """Return a recent preflight payload, or None when it needs re-grounding."""
        product_key = _canonical_product_key(product_key)
        prompt_hash = hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()
        row = self.connection.execute(
            "SELECT payload_json, updated_at FROM understanding_previews WHERE product_key=? AND prompt_hash=?",
            (product_key, prompt_hash),
        ).fetchone()
        if not row:
            return None
        updated = datetime.fromisoformat(row["updated_at"])
        if (datetime.now(UTC) - updated).total_seconds() > max_age_seconds:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def invalidate_knowledge(self, product_key: str) -> bool:
        """Remove a reusable product snapshot and all page snapshots atomically.

        Immutable run artifacts remain untouched; the next discovery is therefore
        forced to re-ground live knowledge rather than reusing stale cache data.
        """
        product_key = _canonical_product_key(product_key)
        row = self.connection.execute(
            "SELECT id FROM product_knowledge WHERE product_key=?", (product_key,)
        ).fetchone()
        if not row:
            return False
        self.connection.execute(
            "DELETE FROM page_knowledge WHERE product_knowledge_id=?", (row["id"],)
        )
        self.connection.execute("DELETE FROM product_knowledge WHERE id=?", (row["id"],))
        self.connection.commit()
        return True
