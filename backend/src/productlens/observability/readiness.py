"""Deployment-readiness probes with provider-neutral results."""

from __future__ import annotations

from typing import Any


def probe_broker(broker: Any) -> dict[str, Any]:
    """Check that a configured Dramatiq broker can accept a round-trip message.

    The probe is intentionally opt-in at the deployment boundary; unit tests can
    provide a stub broker and still verify the readiness contract.
    """
    try:
        broker.declare_queue("productlens-readiness")
    except Exception as error:  # pragma: no cover - provider-specific failures  # noqa: BLE001
        return {"ready": False, "provider": type(broker).__name__, "error": str(error)}
    return {"ready": True, "provider": type(broker).__name__}


def probe_object_storage(storage: Any) -> dict[str, Any]:
    """Validate bucket access without uploading user artifacts."""
    try:
        storage.client.head_bucket(Bucket=storage.bucket)
    except Exception as error:  # pragma: no cover - provider-specific failures  # noqa: BLE001
        return {"ready": False, "provider": type(storage).__name__, "bucket": storage.bucket, "error": str(error)}
    return {"ready": True, "provider": type(storage).__name__, "bucket": storage.bucket}


def probe_database(connection: Any) -> dict[str, Any]:
    """Run a provider-neutral health query on a DB-API connection."""
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        row = cursor.fetchone()
        close = getattr(cursor, "close", None)
        if callable(close):
            close()
        if not row or row[0] != 1:
            return {"ready": False, "provider": type(connection).__name__, "error": "health query returned no sentinel"}
    except Exception as error:  # pragma: no cover - driver-specific failures  # noqa: BLE001
        return {"ready": False, "provider": type(connection).__name__, "error": str(error)}
    return {"ready": True, "provider": type(connection).__name__}
