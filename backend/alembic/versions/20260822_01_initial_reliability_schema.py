"""Create the ProductLens reliability-first persistence schema.

Revision ID: 20260822_01
Revises:
Create Date: 2026-08-22
"""

from __future__ import annotations

from alembic import op

revision = "20260822_01"
down_revision = None
branch_labels = None
depends_on = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_requests (
  id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, url TEXT NOT NULL,
  objective TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS demo_plans (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workflow_steps (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), ordinal INTEGER NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS browser_sessions (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), provider TEXT NOT NULL, external_session_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_lineage (parent_run_id TEXT NOT NULL REFERENCES demo_runs(id), retry_run_id TEXT PRIMARY KEY REFERENCES demo_runs(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS interaction_events (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), ordinal INTEGER NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS presentation_plans (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS narration_scripts (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quality_reports (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), kind TEXT NOT NULL, location TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS provider_configs (id TEXT PRIMARY KEY, provider_type TEXT NOT NULL, name TEXT NOT NULL, credential_reference TEXT, active INTEGER NOT NULL, priority INTEGER NOT NULL, settings_json TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(provider_type, name));
CREATE TABLE IF NOT EXISTS generation_jobs (id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES demo_runs(id), kind TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL, delivery_attempts INTEGER NOT NULL DEFAULT 0, error_code TEXT, queued_at TEXT NOT NULL, claimed_at TEXT, completed_at TEXT);
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, display_name TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, owner_id TEXT REFERENCES users(id), name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS page_knowledge (id TEXT PRIMARY KEY, product_knowledge_id TEXT NOT NULL REFERENCES product_knowledge(id), url TEXT NOT NULL, evidence_json TEXT NOT NULL, confidence REAL NOT NULL, last_verified_at TEXT NOT NULL, UNIQUE(product_knowledge_id, url));
CREATE TABLE IF NOT EXISTS form_schemas (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS synthetic_datasets (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audio_assets (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), location TEXT NOT NULL, duration_seconds REAL, provider TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS video_renders (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id), location TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS provider_calls (id TEXT PRIMARY KEY, run_id TEXT REFERENCES demo_runs(id), provider TEXT NOT NULL, operation TEXT NOT NULL, status TEXT NOT NULL, duration_ms INTEGER, error_code TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_demo_runs_created_at ON demo_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_demo_runs_request_id ON demo_runs(request_id);
CREATE INDEX IF NOT EXISTS idx_demo_attempts_run_id ON demo_attempts(run_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_artifacts_run_id ON artifacts(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_generation_jobs_status ON generation_jobs(status, queued_at);
CREATE INDEX IF NOT EXISTS idx_provider_calls_run_id ON provider_calls(run_id, created_at);
"""


def upgrade() -> None:
    for statement in _SCHEMA.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    # Generation evidence is intentionally retained; schema rollback is not a data-deletion API.
    raise NotImplementedError("ProductLens migrations are forward-only")
