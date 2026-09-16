from __future__ import annotations

from app.config.settings import Settings


def provider_readiness(settings: Settings) -> dict[str, bool]:
    return {
        "openrouter": bool(settings.openrouter_api_key),
        "elevenlabs": bool(settings.elevenlabs_api_key),
        "browserbase": bool(settings.browserbase_api_key),
        # Stagehand uses Browserbase Model Gateway by default. A configured
        # model is an optional override, not a readiness prerequisite.
        "stagehand": bool(settings.browserbase_api_key),
        "local_playwright": True,
    }
