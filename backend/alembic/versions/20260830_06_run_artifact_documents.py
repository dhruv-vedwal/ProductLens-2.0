"""Persist generic, versioned architectural evidence for every run.

Revision ID: 20260830_06
Revises: 20260823_05
Create Date: 2026-08-30
"""

from __future__ import annotations

from alembic import op


revision = "20260830_06"
down_revision = "20260823_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Forward-only migration: existing run files are retained and may be
    # mirrored lazily on their next resume/QA stage.
    op.execute(
        """CREATE TABLE IF NOT EXISTS run_artifact_documents (
          id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES demo_runs(id),
          kind TEXT NOT NULL, payload_json TEXT NOT NULL, source_path TEXT NOT NULL,
          sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, UNIQUE(run_id, kind)
        )"""
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_artifact_documents_run_id "
        "ON run_artifact_documents(run_id, updated_at)"
    )


def downgrade() -> None:
    raise NotImplementedError("ProductLens migrations are forward-only")
