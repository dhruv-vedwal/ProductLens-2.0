"""Record provider model and cost class for operational diagnostics.

Revision ID: 20260911_07
Revises: 20260830_06
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op

revision = "20260911_07"
down_revision = "20260830_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Provider telemetry is additive and non-secret.  Existing rows remain
    # valid with NULL metadata; new calls can report the selected model and
    # coarse cost class without putting credentials in the database.
    if context.is_offline_mode():
        op.add_column("provider_calls", sa.Column("model", sa.Text(), nullable=True))
        op.add_column("provider_calls", sa.Column("cost_class", sa.Text(), nullable=True))
        return
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("provider_calls")}
    if "model" not in columns:
        op.add_column("provider_calls", sa.Column("model", sa.Text(), nullable=True))
    if "cost_class" not in columns:
        op.add_column("provider_calls", sa.Column("cost_class", sa.Text(), nullable=True))


def downgrade() -> None:
    raise NotImplementedError("ProductLens migrations are forward-only")
