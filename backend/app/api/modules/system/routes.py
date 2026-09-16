from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api import deps_state as api
from app.providers.readiness import provider_readiness

router = APIRouter(tags=["system"])


@router.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        **{
            f"provider_{name}": str(ready).lower()
            for name, ready in provider_readiness(api.settings).items()
        },
    }


@router.get("/readiness")
def readiness() -> dict:
    """Report whether generation modes can start without exposing credentials."""
    providers = provider_readiness(api.settings)
    return {
        "database_dialect": api.settings.database_url.split(":", 1)[0],
        "database_ready": True,
        "providers": providers,
        "fixture_generation_ready": True,
        "live_generation_ready": bool(providers["openrouter"]),
        "narration_ready": bool(providers["elevenlabs"]) and not api.settings.caption_only,
        "caption_only": api.settings.caption_only,
        "cloud_browser_ready": bool(providers["browserbase"]),
        "worker_mode": api.settings.worker_mode,
        "queue_ready": (
            bool(api.settings.broker_url) if api.settings.worker_mode == "dramatiq" else True
        ),
    }


@router.get("/providers")
def providers(_: dict = Depends(api.current_user)) -> list[dict]:
    """Expose provider wiring metadata only; keys are never an API response."""
    return api.repository.provider_configs()
