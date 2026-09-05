"""Add account credentials, preferences, and revocable signed sessions.

Revision ID: 20260823_05
Revises: 20260822_04
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op

revision = "20260823_05"
down_revision = "20260822_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        # The prior revisions define the exact baseline in an offline script;
        # emit forward-only DDL rather than attempting unsupported inspection.
        op.add_column("users", sa.Column("password_hash", sa.Text(), nullable=True))
        op.add_column("users", sa.Column("theme_preference", sa.Text(), nullable=False, server_default="system"))
        op.add_column("users", sa.Column("updated_at", sa.Text(), nullable=True))
        op.create_table(
            "auth_sessions",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("expires_at", sa.Text(), nullable=False),
            sa.Column("revoked_at", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),
        )
        op.create_index("idx_projects_owner_id", "projects", ["owner_id", "updated_at"])
        op.create_index("idx_auth_sessions_user_id", "auth_sessions", ["user_id", "expires_at"])
        return
    inspector = sa.inspect(op.get_bind())
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    with op.batch_alter_table("users") as batch:
        if "password_hash" not in user_columns:
            batch.add_column(sa.Column("password_hash", sa.Text(), nullable=True))
        if "theme_preference" not in user_columns:
            batch.add_column(sa.Column("theme_preference", sa.Text(), nullable=False, server_default="system"))
        if "updated_at" not in user_columns:
            batch.add_column(sa.Column("updated_at", sa.Text(), nullable=True))
    if "auth_sessions" not in inspector.get_table_names():
        op.create_table(
            "auth_sessions",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("expires_at", sa.Text(), nullable=False),
            sa.Column("revoked_at", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),
        )
    indexes = {index["name"] for index in inspector.get_indexes("projects")}
    if "idx_projects_owner_id" not in indexes:
        op.create_index("idx_projects_owner_id", "projects", ["owner_id", "updated_at"])
    op.create_index("idx_auth_sessions_user_id", "auth_sessions", ["user_id", "expires_at"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("idx_auth_sessions_user_id", table_name="auth_sessions", if_exists=True)
    op.drop_table("auth_sessions", if_exists=True)
    op.drop_index("idx_projects_owner_id", table_name="projects", if_exists=True)
