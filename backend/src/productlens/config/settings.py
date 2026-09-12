from __future__ import annotations

import os
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
    "PRODUCTLENS_CLOUD_CAPTURE_TIMEOUT_SECONDS",
    "BROWSERBASE_SESSION_TIMEOUT_SECONDS",
}


def _project_provider_environment() -> dict[str, str]:
    """Read the new application's local environment file, never legacy runtime state."""
    default_path = Path(__file__).resolve().parents[3] / ".env"
    source = Path(os.getenv("PRODUCTLENS_ENV_FILE", default_path))
    if not source.is_file():
        return {}
    values: dict[str, str] = {}
    for line in source.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in _PROVIDER_ENV_NAMES and value.strip():
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


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

    @classmethod
    def from_environment(cls) -> Settings:
        project_environment = _project_provider_environment()
        value = lambda key, default=None: os.getenv(key) or project_environment.get(key) or default
        root = Path(os.getenv("PRODUCTLENS_ARTIFACT_ROOT", "artifacts")).resolve()
        auth_secret = value("PRODUCTLENS_AUTH_SECRET")
        if not auth_secret or len(auth_secret) < 32:
            raise RuntimeError(
                "PRODUCTLENS_AUTH_SECRET must be a private value of at least 32 characters"
            )
        database_value = value("PRODUCTLENS_DATABASE", str(root / "productlens.sqlite3"))
        database_url = value("PRODUCTLENS_DATABASE_URL")
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
            openrouter_api_key=value("OPENROUTER_API_KEY"),
            openrouter_model=value("OPENROUTER_MODEL", "openrouter/free"),
            openrouter_vision_model=value("OPENROUTER_VISION_MODEL"),
            multimodal_review_enabled=value("PRODUCTLENS_MULTIMODAL_REVIEW_ENABLED", "false").lower()
            in {"1", "true", "yes"},
            elevenlabs_api_key=value("ELEVENLABS_API_KEY"),
            elevenlabs_voice_id=value("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
            elevenlabs_tts_model=value("ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2"),
            browserbase_api_key=value("BROWSERBASE_API_KEY"),
            browserbase_project_id=value("BROWSERBASE_PROJECT_ID"),
            stagehand_model=value("STAGEHAND_MODEL"),
            stagehand_node=value("STAGEHAND_NODE", "node"),
            # Voice is opt-in so unavailable credits never prevent a silent demo.
            caption_only=value("PRODUCTLENS_CAPTION_ONLY", "true").lower()
            not in {"0", "false", "no"},
            broker_url=value("PRODUCTLENS_BROKER_URL"),
            worker_mode=value("PRODUCTLENS_WORKER_MODE", "polling"),
            auth_secret=auth_secret,
            auth_required=value("PRODUCTLENS_AUTH_REQUIRED", "true").lower() not in {"0", "false", "no"},
            session_ttl_seconds=int(value("PRODUCTLENS_SESSION_TTL_SECONDS", "604800")),
            artifact_storage=value("PRODUCTLENS_ARTIFACT_STORAGE", "local").lower(),
            s3_bucket=value("PRODUCTLENS_S3_BUCKET"),
            s3_prefix=value("PRODUCTLENS_S3_PREFIX", "productlens-runs").strip("/"),
            s3_endpoint_url=value("PRODUCTLENS_S3_ENDPOINT_URL"),
            s3_region=value("PRODUCTLENS_S3_REGION"),
            # Leave time to flush the CDP screencast and download the native
            # Browserbase recording before a provider's 15-minute session cap.
            cloud_capture_timeout_seconds=max(
                30, int(value("PRODUCTLENS_CLOUD_CAPTURE_TIMEOUT_SECONDS", "840"))
            ),
            browserbase_session_timeout_seconds=max(
                60, min(1800, int(value("BROWSERBASE_SESSION_TIMEOUT_SECONDS", "1800")))
            ),
        )
