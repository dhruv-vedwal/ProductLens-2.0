"""Single composition root shared by API controllers and durable workers."""

from __future__ import annotations

from app.config.settings import Settings
from app.credentials.crypto import decrypt_secret
from app.credentials.service import BrowserCredentials, EnvironmentCredentialService
from app.persistence.repository import RunRepository
from app.planning.production import ProductionPlanningService
from app.providers.browserbase import BrowserbaseProvider
from app.providers.elevenlabs import ElevenLabsProvider
from app.providers.openrouter import OpenRouterProvider, OpenRouterVisualReviewer
from app.providers.stagehand import StagehandProvider
from app.services.generation import UrlGenerationService
from app.services.jobs import DemoJobService
from app.storage import LocalArtifactStorage, S3ArtifactStorage


def _vault_credentials(
    repository: RunRepository, auth_secret: str, reference: str
) -> BrowserCredentials | None:
    row = repository.get_product_credential_secrets_by_reference(reference)
    if row is None:
        return None
    try:
        return BrowserCredentials(
            username=decrypt_secret(row["username_ciphertext"], auth_secret),
            password=decrypt_secret(row["password_ciphertext"], auth_secret),
        )
    except ValueError:
        return None


def build_job_service(settings: Settings | None = None) -> tuple[RunRepository, DemoJobService]:
    settings = settings or Settings.from_environment()
    repository = RunRepository(settings.database_url)
    speech = (
        ElevenLabsProvider(
            settings.elevenlabs_api_key, settings.elevenlabs_voice_id, settings.elevenlabs_tts_model
        )
        if settings.elevenlabs_api_key and not settings.caption_only
        else None
    )
    planner_provider = (
        OpenRouterProvider(settings.openrouter_api_key, settings.openrouter_model)
        if settings.openrouter_api_key
        else None
    )
    visual_reviewer = (
        OpenRouterVisualReviewer(settings.openrouter_api_key, settings.openrouter_vision_model)
        if settings.multimodal_review_enabled
        and settings.openrouter_api_key
        and settings.openrouter_vision_model
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
            # Stagehand is useful in both environments.  Local runs use its
            # local browser mode; cloud runs attach it to the Browserbase
            # session.  Keeping the provider available without a Browserbase
            # key prevents an environment-specific intelligence gap while
            # the generation layer still treats its output as advisory and
            # re-grounds every suggestion in Playwright evidence.
            StagehandProvider(
                model=settings.stagehand_model,
                node=settings.stagehand_node,
                browserbase_api_key=settings.browserbase_api_key,
                browserbase_project_id=settings.browserbase_project_id,
                openrouter_api_key=settings.openrouter_api_key,
                openrouter_model=settings.openrouter_model,
            ),
            credential_service=EnvironmentCredentialService(
                auth_secret=settings.auth_secret,
                vault_lookup=lambda reference: _vault_credentials(
                    repository, settings.auth_secret, reference
                ),
            ),
            visual_reviewer=visual_reviewer,
            cloud_capture_timeout_seconds=settings.cloud_capture_timeout_seconds,
            stagehand_observe_timeout_seconds=settings.stagehand_observe_timeout_seconds,
        )
        if planner_provider
        else None
    )
    if settings.artifact_storage == "local":
        artifact_storage = LocalArtifactStorage(settings.artifact_root)
    elif settings.artifact_storage in {"s3", "s3_compatible"}:
        artifact_storage = S3ArtifactStorage(
            bucket=settings.s3_bucket or "",
            prefix=settings.s3_prefix,
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
        )
    else:
        raise RuntimeError("PRODUCTLENS_ARTIFACT_STORAGE must be local, s3, or s3_compatible")
    return repository, DemoJobService(
        repository,
        settings.artifact_root,
        speech_provider=speech,
        url_generator=generator,
        artifact_storage=artifact_storage,
    )
