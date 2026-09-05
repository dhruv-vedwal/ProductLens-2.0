"""Persist resumable ProductLens generation stage checkpoints.

Revision ID: 20260822_03
Revises: 20260822_02
"""

from __future__ import annotations

from alembic import op

revision = "20260822_03"
down_revision = "20260822_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE IF NOT EXISTS generation_stage_jobs (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id),
        stage TEXT NOT NULL, ordinal INTEGER NOT NULL, status TEXT NOT NULL,
        error_code TEXT, started_at TEXT, completed_at TEXT, updated_at TEXT NOT NULL,
        UNIQUE(run_id, stage), UNIQUE(run_id, ordinal))"""
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_generation_stage_jobs_status "
        "ON generation_stage_jobs(status, updated_at)"
    )


def downgrade() -> None:
    raise NotImplementedError("ProductLens migrations are forward-only")
