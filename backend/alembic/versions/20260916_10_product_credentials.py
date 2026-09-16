"""Durable encrypted product credentials for authenticated demos.

Revision ID: 20260916_10
Revises: 20260915_09
"""

from __future__ import annotations

from alembic import context, op

revision = "20260916_10"
down_revision = "20260915_09"
branch_labels = None
depends_on = None


_DDL = """
CREATE TABLE IF NOT EXISTS product_credentials (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES users(id),
  project_id TEXT REFERENCES projects(id),
  name TEXT NOT NULL,
  reference TEXT UNIQUE NOT NULL,
  username_ciphertext TEXT NOT NULL,
  password_ciphertext TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(owner_id, name)
);
CREATE INDEX IF NOT EXISTS idx_product_credentials_owner_id
  ON product_credentials(owner_id, updated_at DESC);
"""


def upgrade() -> None:
    if context.is_offline_mode():
        for statement in _DDL.split(";"):
            if statement.strip():
                op.execute(statement)
        return
    for statement in _DDL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_product_credentials_owner_id")
    op.execute("DROP TABLE IF EXISTS product_credentials")
