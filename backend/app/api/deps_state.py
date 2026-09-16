"""Shared API runtime state and helpers used by route modules.

Route modules import from here (not from ``main``) to avoid circular imports.
Tests that monkeypatch auth behaviour should patch ``settings`` on this module.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import HTTPException

from app.auth.deps import bearer_only_factory, bearer_or_query_factory
from app.auth.security import create_access_token
from app.config.settings import Settings
from app.credentials.service import EnvironmentCredentialService
from app.observability.logging import configure_logging
from app.services.preflight import PreflightService
from app.services.runtime import build_job_service
from app.workers.tasks import process_generation_job

configure_logging()
settings = Settings.from_environment()
repository, jobs = build_job_service(settings)
credential_service = EnvironmentCredentialService(auth_secret=settings.auth_secret)
for provider_type, name, configured, reference in (
    ("llm", "openrouter", bool(settings.openrouter_api_key), "env://OPENROUTER_API_KEY"),
    (
        "tts",
        "elevenlabs",
        bool(settings.elevenlabs_api_key) and not settings.caption_only,
        "env://ELEVENLABS_API_KEY",
    ),
    ("browser", "browserbase", bool(settings.browserbase_api_key), "env://BROWSERBASE_API_KEY"),
    # Stagehand uses the same Browserbase key in cloud mode and Model Gateway
    # in both modes.  Its local browser mode does not require Browserbase, so
    # advertise it whenever an LLM is configured; cloud readiness remains
    # visible through the separate Browserbase provider row.
    ("browser", "stagehand", bool(settings.openrouter_api_key), "env://OPENROUTER_API_KEY"),
):
    repository.upsert_provider_config(
        provider_type=provider_type,
        name=name,
        credential_reference=reference if configured else None,
        active=configured,
    )
url_generator = jobs.url_generator
preflight_service = PreflightService(
    generator=url_generator,
    artifact_root=settings.artifact_root,
    stagehand_provider=getattr(url_generator, "stagehand_provider", None),
    repository=repository,
)

current_user = bearer_only_factory(repository, lambda: settings)
current_user_bearer_or_query = bearer_or_query_factory(repository, lambda: settings)


def resolve_cloud_discovery(requested: bool | None, *, browserbase_configured: bool) -> bool:
    """Resolve the capability-aware cloud default at the API boundary."""
    return browserbase_configured if requested is None else requested


def dispatch_generation_job(job_id: str) -> None:
    """Publish only when a durable Dramatiq broker is configured.

    The local polling worker consumes the persisted outbox directly. This keeps the
    development path durable and avoids pretending that a process-local stub queue
    is a production delivery guarantee.
    """
    if settings.worker_mode == "dramatiq" and settings.broker_url:
        process_generation_job.send(job_id)


def public_user(user: dict) -> dict[str, str | None]:
    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user.get("display_name"),
        "theme_preference": user.get("theme_preference") or "system",
    }


def issue_session(user: dict) -> dict[str, object]:
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.session_ttl_seconds)
    session = repository.create_session(user["id"], expires_at.isoformat())
    return {
        "access_token": create_access_token(
            user_id=user["id"],
            session_id=session["id"],
            secret=settings.auth_secret,
            ttl_seconds=settings.session_ttl_seconds,
        ),
        "token_type": "bearer",
        "expires_at": expires_at.isoformat(),
        "user": public_user(user),
    }


def owned_run_or_404(run_id: str, user: dict) -> dict:
    try:
        return (
            repository.get_run_for_user(run_id, user["id"])
            if settings.auth_required
            else repository.get_run(run_id)
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="run not found") from error


def owned_artifact_path(run_id: str, user: dict, *, kind: str) -> Path:
    owned_run_or_404(run_id, user)
    for artifact in repository.locations(run_id):
        if artifact["kind"] != kind:
            continue
        path = Path(artifact["location"]).resolve()
        try:
            path.relative_to(settings.artifact_root.resolve())
        except ValueError as error:
            raise HTTPException(status_code=403, detail="invalid artifact location") from error
        if path.exists():
            return path
    raise HTTPException(status_code=404, detail=f"{kind.replace('_', ' ')} is not available")
