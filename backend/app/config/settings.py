from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_PROVIDER_ENV_NAMES = {
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL",
    "OPENROUTER_VISION_MODEL",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_VOICE_ID",
    "ELEVENLABS_TTS_MODEL",
    "BROWSERBASE_API_KEY",
    "BROWSERBASE_PROJECT_ID",
    "STAGEHAND_MODEL",
    "STAGEHAND_NODE",
    "PRODUCTLENS_STAGEHAND_OBSERVE_TIMEOUT_SECONDS",
    "PRODUCTLENS_BROKER_URL",
    "PRODUCTLENS_WORKER_MODE",
    "PRODUCTLENS_AUTH_SECRET",
    "PRODUCTLENS_AUTH_REQUIRED",
    "PRODUCTLENS_MULTIMODAL_REVIEW_ENABLED",
    "PRODUCTLENS_SESSION_TTL_SECONDS",
    "PRODUCTLENS_ARTIFACT_STORAGE",
    "PRODUCTLENS_S3_BUCKET",
    "PRODUCTLENS_S3_PREFIX",
    "PRODUCTLENS_S3_ENDPOINT_URL",
    "PRODUCTLENS_S3_REGION",
    "PRODUCTLENS_DATABASE_URL",
    "PRODUCTLENS_DATABASE",
    "OPENROUTER_STRUCTURED_TIMEOUT_SECONDS",
    "PRODUCTLENS_CLOUD_CAPTURE_TIMEOUT_SECONDS",
    "BROWSERBASE_SESSION_TIMEOUT_SECONDS",
}


def _backend_root() -> Path:
    """Return the backend package root (`.../backend`), not the monorepo root."""
    return Path(__file__).resolve().parents[2]


def _project_provider_environment() -> dict[str, str]:
    """Read the new application's local environment file, never legacy runtime state."""
    default_path = _backend_root() / ".env"
    source = Path(os.getenv("PRODUCTLENS_ENV_FILE", default_path))
    if not source.is_file():
        return {}
    values: dict[str, str] = {}
    for line in source.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in _PROVIDER_ENV_NAMES and value.strip():
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _setting_value(
    key: str,
    environment: Mapping[str, str],
    default: str | None = None,
) -> str | None:
    """Resolve process environment first, then the project-local env file."""
    return os.getenv(key) or environment.get(key) or default


def _boolean_setting(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().casefold() not in {"0", "false", "no", "off"}


def _bounded_int(
    value: str | None, *, default: int, minimum: int, maximum: int | None = None
) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed) if maximum is not None else parsed)


def _bounded_float(
    value: str | None,
    *,
    default: float,
    minimum: float,
    maximum: float | None = None,
) -> float:
    try:
        parsed = float(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed) if maximum is not None else parsed)


@dataclass(frozen=True)
class Settings:
    artifact_root: Path
    database_path: Path
    database_url: str
    openrouter_api_key: str | None
    openrouter_model: str
    openrouter_vision_model: str | None
    multimodal_review_enabled: bool
    elevenlabs_api_key: str | None
    elevenlabs_voice_id: str
    elevenlabs_tts_model: str
    browserbase_api_key: str | None
    browserbase_project_id: str | None
    stagehand_model: str | None
    stagehand_node: str
    caption_only: bool
    broker_url: str | None
    worker_mode: str
    auth_secret: str
    auth_required: bool
    session_ttl_seconds: int
    artifact_storage: str
    s3_bucket: str | None
    s3_prefix: str
    s3_endpoint_url: str | None
    s3_region: str | None
    cloud_capture_timeout_seconds: int
    browserbase_session_timeout_seconds: int
    stagehand_observe_timeout_seconds: float
    repair_wall_clock_budget_seconds: int

    @classmethod
    def from_environment(cls) -> Settings:
        project_environment = _project_provider_environment()
        root = Path(os.getenv("PRODUCTLENS_ARTIFACT_ROOT", "artifacts")).resolve()
        auth_secret = _setting_value("PRODUCTLENS_AUTH_SECRET", project_environment)
        if not auth_secret or len(auth_secret) < 32:
            raise RuntimeError(
                "PRODUCTLENS_AUTH_SECRET must be a private value of at least 32 characters"
            )
        database_value = _setting_value(
            "PRODUCTLENS_DATABASE", project_environment, str(root / "productlens.sqlite3")
        ) or str(root / "productlens.sqlite3")
        database_url = _setting_value("PRODUCTLENS_DATABASE_URL", project_environment)
        if not database_url:
            database_path = Path(database_value).resolve()
            database_url = f"sqlite:///{database_path.as_posix()}"
        elif database_url.startswith("sqlite:///"):
            database_path = Path(database_url.removeprefix("sqlite:///"))
        else:
            # A path remains available for local-only artifacts and health output;
            # it is never used to select a production database dialect.
            database_path = Path(database_value).resolve()
        return cls(
            artifact_root=root,
            database_path=database_path,
            database_url=database_url,
            openrouter_api_key=_setting_value("OPENROUTER_API_KEY", project_environment),
            openrouter_model=_setting_value(
                "OPENROUTER_MODEL", project_environment, "google/gemini-2.5-flash"
            )
            or "google/gemini-2.5-flash",
            openrouter_vision_model=_setting_value("OPENROUTER_VISION_MODEL", project_environment),
            multimodal_review_enabled=_boolean_setting(
                _setting_value("PRODUCTLENS_MULTIMODAL_REVIEW_ENABLED", project_environment),
                default=False,
            ),
            elevenlabs_api_key=_setting_value("ELEVENLABS_API_KEY", project_environment),
            elevenlabs_voice_id=_setting_value(
                "ELEVENLABS_VOICE_ID", project_environment, "21m00Tcm4TlvDq8ikWAM"
            )
            or "21m00Tcm4TlvDq8ikWAM",
            elevenlabs_tts_model=_setting_value(
                "ELEVENLABS_TTS_MODEL", project_environment, "eleven_multilingual_v2"
            )
            or "eleven_multilingual_v2",
            browserbase_api_key=_setting_value("BROWSERBASE_API_KEY", project_environment),
            browserbase_project_id=_setting_value("BROWSERBASE_PROJECT_ID", project_environment),
            stagehand_model=_setting_value("STAGEHAND_MODEL", project_environment),
            stagehand_node=_setting_value("STAGEHAND_NODE", project_environment, "node") or "node",
            # Voice is opt-in so unavailable credits never prevent a silent demo.
            caption_only=_boolean_setting(
                _setting_value("PRODUCTLENS_CAPTION_ONLY", project_environment), default=True
            ),
            broker_url=_setting_value("PRODUCTLENS_BROKER_URL", project_environment),
            worker_mode=_setting_value("PRODUCTLENS_WORKER_MODE", project_environment, "polling")
            or "polling",
            auth_secret=auth_secret,
            auth_required=_boolean_setting(
                _setting_value("PRODUCTLENS_AUTH_REQUIRED", project_environment), default=True
            ),
            session_ttl_seconds=_bounded_int(
                _setting_value("PRODUCTLENS_SESSION_TTL_SECONDS", project_environment),
                default=604800,
                minimum=60,
            ),
            artifact_storage=(
                _setting_value("PRODUCTLENS_ARTIFACT_STORAGE", project_environment, "local")
                or "local"
            ).lower(),
            s3_bucket=_setting_value("PRODUCTLENS_S3_BUCKET", project_environment),
            s3_prefix=(
                _setting_value("PRODUCTLENS_S3_PREFIX", project_environment, "productlens-runs")
                or "productlens-runs"
            ).strip("/"),
            s3_endpoint_url=_setting_value("PRODUCTLENS_S3_ENDPOINT_URL", project_environment),
            s3_region=_setting_value("PRODUCTLENS_S3_REGION", project_environment),
            # Leave time to flush the CDP screencast and download the native
            # Browserbase recording before a provider's 15-minute session cap.
            cloud_capture_timeout_seconds=max(
                30,
                _bounded_int(
                    _setting_value(
                        "PRODUCTLENS_CLOUD_CAPTURE_TIMEOUT_SECONDS", project_environment
                    ),
                    default=840,
                    minimum=30,
                ),
            ),
            browserbase_session_timeout_seconds=_bounded_int(
                _setting_value("BROWSERBASE_SESSION_TIMEOUT_SECONDS", project_environment),
                default=1800,
                minimum=60,
                maximum=1800,
            ),
            stagehand_observe_timeout_seconds=_bounded_float(
                _setting_value(
                    "PRODUCTLENS_STAGEHAND_OBSERVE_TIMEOUT_SECONDS", project_environment
                ),
                default=105.0,
                minimum=30.0,
                maximum=180.0,
            ),
            repair_wall_clock_budget_seconds=_bounded_int(
                _setting_value(
                    "PRODUCTLENS_REPAIR_WALL_CLOCK_BUDGET_SECONDS",
                    project_environment,
                ),
                default=3600,
                minimum=60,
                maximum=21_600,
            ),
        )
