"""Associate requests with ProductLens studio projects.

Revision ID: 20260822_02
Revises: 20260822_01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op

revision = "20260822_02"
down_revision = "20260822_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        # Offline PostgreSQL SQL generation cannot inspect a live schema. At
        # this revision the preceding migration is authoritative. SQLite
        # cannot emit ALTER TABLE ADD CONSTRAINT, so its development SQL keeps
        # the nullable project reference/index while the online batch upgrade
        # creates the actual foreign key.
        op.add_column("demo_requests", sa.Column("project_id", sa.Text(), nullable=True))
        url = sa.engine.make_url(context.config.get_main_option("sqlalchemy.url"))
        if url.get_backend_name() != "sqlite":
            op.create_foreign_key("fk_demo_requests_project", "demo_requests", "projects", ["project_id"], ["id"])
        op.create_index("idx_demo_requests_project_id", "demo_requests", ["project_id"])
        return
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("demo_requests")}
    indexes = {index["name"] for index in inspector.get_indexes("demo_requests")}
    # Repository bootstrap supported pre-migration local databases during the
    # transition.  Do not rebuild a table merely to add the same column/index.
    if "project_id" in columns:
        if "idx_demo_requests_project_id" not in indexes:
            op.create_index("idx_demo_requests_project_id", "demo_requests", ["project_id"])
        return
    with op.batch_alter_table("demo_requests") as batch:
        batch.add_column(sa.Column("project_id", sa.Text(), nullable=True))
        batch.create_foreign_key("fk_demo_requests_project", "projects", ["project_id"], ["id"])
        batch.create_index("idx_demo_requests_project_id", ["project_id"])


def downgrade() -> None:
    with op.batch_alter_table("demo_requests") as batch:
        batch.drop_index("idx_demo_requests_project_id")
        batch.drop_constraint("fk_demo_requests_project", type_="foreignkey")
        batch.drop_column("project_id")
