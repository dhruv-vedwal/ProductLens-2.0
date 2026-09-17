from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.persistence.db import _DAO


class ArtifactsDAO(_DAO):
    """Domain persistence for artifacts."""

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
