"""Single composition root shared by API controllers and durable workers."""

from __future__ import annotations

from productlens.config.settings import Settings
from productlens.persistence.repository import RunRepository
from productlens.planning.production import ProductionPlanningService
from productlens.providers.browserbase import BrowserbaseProvider
from productlens.providers.elevenlabs import ElevenLabsProvider
from productlens.providers.openrouter import OpenRouterProvider
from productlens.providers.stagehand import StagehandProvider
from productlens.services.generation import UrlGenerationService
from productlens.services.jobs import DemoJobService
from productlens.storage import LocalArtifactStorage, S3ArtifactStorage


def build_job_service(settings: Settings | None = None) -> tuple[RunRepository, DemoJobService]:
    settings = settings or Settings.from_environment()
    repository = RunRepository(settings.database_url)
    speech = (
        ElevenLabsProvider(settings.elevenlabs_api_key, settings.elevenlabs_voice_id, settings.elevenlabs_tts_model)
        if settings.elevenlabs_api_key and not settings.caption_only
        else None
    )
    planner_provider = (
        OpenRouterProvider(settings.openrouter_api_key, settings.openrouter_model)
        if settings.openrouter_api_key
        else None
    )
    generator = (
        UrlGenerationService(
            ProductionPlanningService(planner_provider),
            speech,
            BrowserbaseProvider(
                settings.browserbase_api_key,
                settings.browserbase_project_id,
                session_timeout_seconds=settings.browserbase_session_timeout_seconds,
            )
            if settings.browserbase_api_key
            else None,
            StagehandProvider(
                model=settings.stagehand_model,
                node=settings.stagehand_node,
                browserbase_api_key=settings.browserbase_api_key,
            )
            if settings.browserbase_api_key
            else None,
            cloud_capture_timeout_seconds=settings.cloud_capture_timeout_seconds,
        )
        if planner_provider
        else None
    )
    if settings.artifact_storage == "local":
        artifact_storage = LocalArtifactStorage(settings.artifact_root)
    elif settings.artifact_storage in {"s3", "s3_compatible"}:
        artifact_storage = S3ArtifactStorage(
            bucket=settings.s3_bucket or "", prefix=settings.s3_prefix,
            endpoint_url=settings.s3_endpoint_url, region_name=settings.s3_region,
        )
    else:
        raise RuntimeError("PRODUCTLENS_ARTIFACT_STORAGE must be local, s3, or s3_compatible")
    return repository, DemoJobService(
        repository, settings.artifact_root, speech_provider=speech,
        url_generator=generator, artifact_storage=artifact_storage,
    )
