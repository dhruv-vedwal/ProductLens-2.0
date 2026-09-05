"""Add observable delivery leases to the durable stage queue.

Revision ID: 20260822_04
Revises: 20260822_03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260822_04"
down_revision = "20260822_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("generation_stage_jobs") as batch:
        batch.add_column(sa.Column("delivery_attempts", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("claimed_at", sa.Text(), nullable=True))


def downgrade() -> None:
    raise NotImplementedError("ProductLens migrations are forward-only")
