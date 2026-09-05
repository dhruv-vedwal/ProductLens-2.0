import os
import subprocess
import sys
from pathlib import Path


def test_sqlite_offline_migration_sql_is_generatable():
    """Development migration validation must not require a live database."""
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "generation_stage_jobs" in result.stdout


def test_postgresql_offline_migration_sql_keeps_project_foreign_key():
    root = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PRODUCTLENS_DATABASE_URL": "postgresql://user:password@db.invalid/productlens"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "fk_demo_requests_project" in result.stdout


def test_postgresql_runtime_url_uses_installed_psycopg_v3(monkeypatch):
    from productlens.persistence.repository import _PostgresConnection

    captured = {}

    class Connection:
        def close(self):
            pass

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            pass

    def fake_create_engine(url, **_kwargs):
        captured["url"] = url
        return Engine()

    monkeypatch.setattr("productlens.persistence.repository.create_engine", fake_create_engine)
    _PostgresConnection("postgresql://user:password@db.invalid/productlens")
    assert captured["url"].startswith("postgresql+psycopg://")
