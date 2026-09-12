"""Record provider model and cost class for operational diagnostics.

Revision ID: 20260911_07
Revises: 20260830_06
Create Date: 2026-09-11
"""

from __future__ import annotations

from alembic import op


revision = "20260911_07"
down_revision = "20260830_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Provider telemetry is additive and non-secret.  Existing rows remain
    # valid with NULL metadata; new calls can report the selected model and
    # coarse cost class without putting credentials in the database.
    op.execute("ALTER TABLE provider_calls ADD COLUMN model TEXT")
    op.execute("ALTER TABLE provider_calls ADD COLUMN cost_class TEXT")


def downgrade() -> None:
    raise NotImplementedError("ProductLens migrations are forward-only")
