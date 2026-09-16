from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import create_engine, text

from app.persistence.schema import SCHEMA_SQL

if TYPE_CHECKING:
    from app.persistence.repository import RunRepository


class _DatabaseRow(Mapping[str, Any]):
    """Small cross-dialect row facade used by the legacy repository methods."""

    def __init__(self, row: Any):
        self._values = dict(row._mapping)
        self._ordered = tuple(self._values.values())

    def __getitem__(self, key: str | int) -> Any:
        return self._ordered[key] if isinstance(key, int) else self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


class _DatabaseResult:
    def __init__(self, result: Any):
        self._result = result
        self.rowcount: int = int(result.rowcount)

    def fetchone(self) -> _DatabaseRow | None:
        row = self._result.fetchone()
        return _DatabaseRow(row) if row is not None else None

    def fetchall(self) -> list[_DatabaseRow]:
        return [_DatabaseRow(row) for row in self._result.fetchall()]


class _PostgresConnection:
    """DB-API-shaped facade that makes the proven repository API portable.

    The repository intentionally retains its explicit commits and compact SQL. This
    facade converts its SQLite qmark parameters to SQLAlchemy named parameters,
    while PostgreSQL owns locking, transactions, and concurrent worker claims.
    """

    def __init__(self, database_url: str):
        # Bare PostgreSQL URLs make SQLAlchemy select psycopg2. ProductLens
        # installs psycopg v3, so normalize operator-provided URLs here.
        if database_url.startswith("postgres://"):
            database_url = "postgresql+psycopg://" + database_url.removeprefix("postgres://")
        elif database_url.startswith("postgresql://"):
            database_url = "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
        self.engine = create_engine(database_url, pool_pre_ping=True, future=True)
        self._connection = self.engine.connect()

    @staticmethod
    def _statement(
        statement: str, parameters: tuple[Any, ...] | dict[str, Any] | None
    ) -> tuple[Any, dict[str, Any]]:
        if not isinstance(parameters, tuple):
            return text(statement), parameters or {}
        binds: dict[str, Any] = {}
        parts = statement.split("?")
        if len(parts) - 1 != len(parameters):
            raise ValueError("database placeholder count does not match parameters")
        rendered = [parts[0]]
        for index, part in enumerate(parts[1:]):
            name = f"p{index}"
            binds[name] = parameters[index]
            rendered.extend((f":{name}", part))
        return text("".join(rendered)), binds

    def execute(
        self, statement: str, parameters: tuple[Any, ...] | dict[str, Any] | None = None
    ) -> _DatabaseResult:
        query, binds = self._statement(statement, parameters)
        return _DatabaseResult(self._connection.execute(query, binds))

    def executemany(
        self, statement: str, parameters: list[tuple[Any, ...] | dict[str, Any]]
    ) -> _DatabaseResult | None:
        if not parameters:
            return None
        query, _first = self._statement(statement, parameters[0])
        if isinstance(parameters[0], tuple):
            rendered = []
            for values in parameters:
                _, binds = self._statement(statement, values)
                rendered.append(binds)
            return _DatabaseResult(self._connection.execute(query, rendered))
        # SQLAlchemy's runtime accepts a list of mappings here, but its type
        # overload is narrower than the SQLite-compatible repository surface.
        return _DatabaseResult(self._connection.execute(query, parameters))  # type: ignore[arg-type]

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self) -> None:
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()
        self.engine.dispose()


def connect_database(database: Path | str) -> tuple[str, _PostgresConnection | sqlite3.Connection]:
    """Open a SQLite or PostgreSQL connection with the repository dialect rules."""
    database_url = str(database)
    dialect = (
        "postgresql" if database_url.startswith(("postgresql", "postgres://")) else "sqlite"
    )
    if dialect == "postgresql":
        connection: _PostgresConnection | sqlite3.Connection = _PostgresConnection(database_url)
    else:
        database_path = Path(database_url.removeprefix("sqlite:///"))
        database_path.parent.mkdir(parents=True, exist_ok=True)
        # A local worker pool may briefly contend on the compare-and-set
        # claim update.  Let SQLite wait for the owning transaction rather
        # than surfacing a transient "database is locked" failure.
        sqlite_connection = sqlite3.connect(
            database_path, check_same_thread=False, timeout=30.0
        )
        sqlite_connection.row_factory = sqlite3.Row
        sqlite_connection.execute("PRAGMA foreign_keys = ON")
        sqlite_connection.execute("PRAGMA journal_mode = WAL")
        sqlite_connection.execute("PRAGMA busy_timeout = 30000")
        connection = sqlite_connection
    return dialect, connection


def bootstrap_schema(
    connection: _PostgresConnection | sqlite3.Connection, dialect: str, repo: RunRepository
) -> None:
    """Apply DDL and SQLite forward-compatible upgrades, then commit."""
    connection.executescript(SCHEMA_SQL)
    if dialect == "sqlite":
        upgrade_sqlite_schema(repo)
    connection.commit()


def upgrade_sqlite_schema(repo: RunRepository) -> None:
    columns = {row["name"] for row in repo._fetchall("PRAGMA table_info(demo_requests)")}
    if "project_id" not in columns:
        repo.connection.execute("ALTER TABLE demo_requests ADD COLUMN project_id TEXT")
        repo.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_demo_requests_project_id ON demo_requests(project_id)"
        )
    stage_columns = {
        row["name"]
        for row in repo._fetchall("PRAGMA table_info(generation_stage_jobs)")
    }
    # Keep direct SQLite bootstrapping compatible with databases created by
    # releases before the stage-delivery migration. Alembic performs the
    # same forward-only schema upgrade in deployed environments.
    if stage_columns and "delivery_attempts" not in stage_columns:
        repo.connection.execute(
            "ALTER TABLE generation_stage_jobs ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0"
        )
    if stage_columns and "claimed_at" not in stage_columns:
        repo.connection.execute("ALTER TABLE generation_stage_jobs ADD COLUMN claimed_at TEXT")
    if stage_columns and "heartbeat_at" not in stage_columns:
        repo.connection.execute(
            "ALTER TABLE generation_stage_jobs ADD COLUMN heartbeat_at TEXT"
        )
    user_columns = {row["name"] for row in repo._fetchall("PRAGMA table_info(users)")}
    if "password_hash" not in user_columns:
        repo.connection.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
    if "theme_preference" not in user_columns:
        repo.connection.execute(
            "ALTER TABLE users ADD COLUMN theme_preference TEXT NOT NULL DEFAULT 'system'"
        )
    if "updated_at" not in user_columns:
        repo.connection.execute("ALTER TABLE users ADD COLUMN updated_at TEXT")
    provider_columns = {
        row["name"] for row in repo._fetchall("PRAGMA table_info(provider_calls)")
    }
    if provider_columns and "model" not in provider_columns:
        repo.connection.execute("ALTER TABLE provider_calls ADD COLUMN model TEXT")
    if provider_columns and "cost_class" not in provider_columns:
        repo.connection.execute("ALTER TABLE provider_calls ADD COLUMN cost_class TEXT")


class _DAO:
    """Shared connection helpers; unknown attrs forward to the owning repository."""

    def __init__(self, repo: RunRepository) -> None:
        self._repo = repo

    @property
    def connection(self) -> _PostgresConnection | sqlite3.Connection:
        return self._repo.connection

    @property
    def dialect(self) -> str:
        return self._repo.dialect

    def _fetchone(
        self, statement: str, parameters: tuple[Any, ...] | dict[str, Any] | None = None
    ) -> Any:
        return self._repo._fetchone(statement, parameters)

    def _fetchall(
        self, statement: str, parameters: tuple[Any, ...] | dict[str, Any] | None = None
    ) -> list[Any]:
        return self._repo._fetchall(statement, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repo, name)
