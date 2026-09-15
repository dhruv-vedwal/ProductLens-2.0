"""Track liveness of long-running generation stages.

Revision ID: 20260913_08
Revises: 20260911_07
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op

revision = "20260913_08"
down_revision = "20260911_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        with op.batch_alter_table("generation_stage_jobs") as batch:
            batch.add_column(sa.Column("heartbeat_at", sa.Text(), nullable=True))
        return
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("generation_stage_jobs")}
    if "heartbeat_at" not in columns:
        with op.batch_alter_table("generation_stage_jobs") as batch:
            batch.add_column(sa.Column("heartbeat_at", sa.Text(), nullable=True))


def downgrade() -> None:
    raise NotImplementedError("ProductLens migrations are forward-only")
