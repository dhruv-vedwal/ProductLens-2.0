"""Keep immutable, versioned product-knowledge snapshots."""

from __future__ import annotations

from alembic import context, op

revision = "20260915_09"
down_revision = "20260913_08"
branch_labels = None
depends_on = None


_DDL = """
CREATE TABLE IF NOT EXISTS knowledge_versions (
  id TEXT PRIMARY KEY,
  product_key TEXT NOT NULL,
  version INTEGER NOT NULL,
  fingerprint TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  confidence REAL NOT NULL,
  captured_at TEXT NOT NULL,
  UNIQUE(product_key, version)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_versions_key
  ON knowledge_versions(product_key, version DESC);
"""


def upgrade() -> None:
    # This migration is intentionally idempotent for databases that were
    # bootstrapped by RunRepository before Alembic was introduced.
    if context.is_offline_mode():
        for statement in _DDL.split(";"):
            if statement.strip():
                op.execute(statement)
        return
    for statement in _DDL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("ProductLens migrations are forward-only")
