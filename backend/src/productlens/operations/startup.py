"""Migration-first startup and dependency validation for deployed processes.

The API and workers deliberately do not run migrations themselves: concurrent
replicas must never race a schema upgrade.  Run this module once as the
deployment migration job, then start API/worker replicas only after it exits
successfully.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Any

from sqlalchemy import create_engine

from productlens.config.settings import Settings
from productlens.observability.readiness import probe_broker, probe_database, probe_object_storage
from productlens.storage import S3ArtifactStorage
from productlens.workers.broker import configure_broker


def run_migrations() -> None:
    """Apply Alembic revisions before application replicas become ready."""
    completed = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=False)
    if completed.returncode:
        raise RuntimeError(f"alembic upgrade head failed with exit code {completed.returncode}")


def deployment_readiness(settings: Settings | None = None) -> dict[str, dict[str, Any]]:
    """Probe configured runtime dependencies without exposing credentials."""
    settings = settings or Settings.from_environment()
    engine = None
    connection = None
    try:
        database_url = settings.database_url
        # The deployment image installs psycopg v3; normalize the convenient
        # bare URL form so readiness uses the same driver as the repository and
        # Alembic paths.
        if database_url.startswith("postgres://"):
            database_url = "postgresql+psycopg://" + database_url.removeprefix("postgres://")
        elif database_url.startswith("postgresql://"):
            database_url = "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
        engine = create_engine(database_url, pool_pre_ping=True, future=True)
        # SQLAlchemy's ``raw_connection`` returns a pooled proxy, not a
        # context-manager in every supported version/dialect. Close it
        # explicitly so a healthy PostgreSQL deployment is not misreported as
        # unavailable during readiness validation.
        connection = engine.raw_connection()
        database = probe_database(connection)
    except Exception as error:  # pragma: no cover - driver/network dependent  # noqa: BLE001
        database = {"ready": False, "provider": "database", "error": str(error)}
    finally:
        if connection is not None:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
        if engine is not None:
            engine.dispose()

    broker: dict[str, Any]
    if settings.worker_mode == "dramatiq":
        if not settings.broker_url:
            broker = {
                "ready": False,
                "provider": "RabbitmqBroker",
                "error": "PRODUCTLENS_BROKER_URL is required",
            }
        else:
            broker = probe_broker(configure_broker(settings))
    else:
        broker = {"ready": True, "provider": "polling", "mode": "polling"}

    if settings.artifact_storage == "local":
        settings.artifact_root.mkdir(parents=True, exist_ok=True)
        storage: dict[str, Any] = {
            "ready": True,
            "provider": "LocalArtifactStorage",
            "root": str(settings.artifact_root),
        }
    else:
        try:
            storage = probe_object_storage(
                S3ArtifactStorage(
                    bucket=settings.s3_bucket or "",
                    prefix=settings.s3_prefix,
                    endpoint_url=settings.s3_endpoint_url,
                    region_name=settings.s3_region,
                )
            )
        except (
            Exception  # noqa: BLE001
        ) as error:  # pragma: no cover - optional boto/network dependent
            storage = {"ready": False, "provider": "S3ArtifactStorage", "error": str(error)}
    return {"database": database, "broker": broker, "object_storage": storage}


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare and validate a ProductLens deployment")
    parser.add_argument("--skip-migrations", action="store_true")
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="return non-zero unless every configured dependency is ready",
    )
    args = parser.parse_args()
    if not args.skip_migrations:
        run_migrations()
    report = deployment_readiness()
    print(report, flush=True)
    if args.require_ready and not all(item["ready"] for item in report.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
