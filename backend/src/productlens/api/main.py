from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, HttpUrl, field_validator

from productlens.artifacts.store import RunArtifacts
from productlens.auth.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from productlens.config.settings import Settings
from productlens.observability.logging import configure_logging
from productlens.providers.readiness import provider_readiness
from productlens.services.runtime import build_job_service
from productlens.workers.tasks import process_generation_job

configure_logging()
settings = Settings.from_environment()
repository, jobs = build_job_service(settings)
for provider_type, name, configured, reference in (
    ("llm", "openrouter", bool(settings.openrouter_api_key), "env://OPENROUTER_API_KEY"),
    ("tts", "elevenlabs", bool(settings.elevenlabs_api_key) and not settings.caption_only, "env://ELEVENLABS_API_KEY"),
    ("browser", "browserbase", bool(settings.browserbase_api_key), "env://BROWSERBASE_API_KEY"),
    # Stagehand uses the same Browserbase key and Model Gateway by default;
    # STAGEHAND_MODEL only overrides the selected gateway model.
    ("browser", "stagehand", bool(settings.browserbase_api_key), "env://BROWSERBASE_API_KEY"),
):
    repository.upsert_provider_config(
        provider_type=provider_type,
        name=name,
        credential_reference=reference if configured else None,
        active=configured,
    )
url_generator = jobs.url_generator
app = FastAPI(title="ProductLens 2.0 Generation Engine")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000", "http://localhost:3001",
        "http://127.0.0.1:3000", "http://127.0.0.1:3001",
    ],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)


def dispatch_generation_job(job_id: str) -> None:
    """Publish only when a durable Dramatiq broker is configured.

    The local polling worker consumes the persisted outbox directly. This keeps the
    development path durable and avoids pretending that a process-local stub queue
    is a production delivery guarantee.
    """
    if settings.worker_mode == "dramatiq" and settings.broker_url:
        process_generation_job.send(job_id)


class FixtureRequest(BaseModel):
    gate: int = Field(ge=1, le=6)
    objective: str = Field(min_length=3)
    render: bool = True
    project_id: str | None = None


class GenerationRequest(BaseModel):
    request_id: str | None = None
    url: HttpUrl
    objective: str = Field(min_length=3, max_length=2_000)
    allow_external_side_effects: bool = False
    cloud_discovery: bool = False
    stagehand_assist: bool = False
    max_pages: int = Field(default=6, ge=1, le=12)
    render: bool = True
    project_id: str | None = None
    credential_reference: str | None = None
    audience: str = Field(default="product prospect", min_length=2, max_length=120)
    target_duration_seconds: int = Field(default=120, ge=30, le=300)

    @field_validator("credential_reference")
    @classmethod
    def credential_must_be_an_opaque_reference(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("secret://productlens/"):
            raise ValueError("credential_reference must be an opaque ProductLens secret reference")
        return value


class ProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class SignupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=256)
    display_name: str | None = Field(default=None, max_length=120)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
            raise ValueError("enter a valid email address")
        return normalized


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class PreferenceRequest(BaseModel):
    theme_preference: Literal["system", "light", "dark"]


class RetryRequest(BaseModel):
    """Explicit retry configuration; safe defaults prevent replaying side effects."""

    allow_external_side_effects: bool = False
    cloud_discovery: bool | None = None
    stagehand_assist: bool | None = None
    max_pages: int | None = Field(default=None, ge=1, le=12)
    render: bool | None = None
    credential_reference: str | None = None
    audience: str | None = Field(default=None, min_length=2, max_length=120)
    target_duration_seconds: int | None = Field(default=None, ge=30, le=300)
    retry_from_stage: Literal["PLANNING", "EXECUTION", "NARRATION", "RENDER", "VIDEO_QA"] | None = None

    @field_validator("credential_reference")
    @classmethod
    def retry_credential_must_be_an_opaque_reference(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("secret://productlens/"):
            raise ValueError("credential_reference must be an opaque ProductLens secret reference")
        return value


class RetentionRequest(BaseModel):
    older_than_seconds: int = Field(default=0, ge=0, le=31_536_000)


class KnowledgeInvalidationRequest(BaseModel):
    url: HttpUrl


def public_user(user: dict) -> dict[str, str | None]:
    return {
        "id": user["id"], "email": user["email"], "display_name": user.get("display_name"),
        "theme_preference": user.get("theme_preference") or "system",
    }


def current_user(authorization: str | None = Header(default=None)) -> dict:
    if not settings.auth_required:
        return {"id": "local-studio", "email": "local@productlens.invalid", "display_name": "Local Studio"}
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="sign in to access your ProductLens workspace")
    claims = decode_access_token(authorization.removeprefix("Bearer ").strip(), settings.auth_secret)
    if not claims or not repository.active_session(claims["sid"], claims["sub"]):
        raise HTTPException(status_code=401, detail="your session has expired; sign in again")
    try:
        return repository.get_user(claims["sub"])
    except KeyError as error:
        raise HTTPException(status_code=401, detail="account is unavailable") from error


def issue_session(user: dict) -> dict[str, object]:
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.session_ttl_seconds)
    session = repository.create_session(user["id"], expires_at.isoformat())
    return {
        "access_token": create_access_token(
            user_id=user["id"], session_id=session["id"], secret=settings.auth_secret,
            ttl_seconds=settings.session_ttl_seconds,
        ),
        "token_type": "bearer", "expires_at": expires_at.isoformat(), "user": public_user(user),
    }


@app.post("/auth/signup", status_code=201)
def signup(payload: SignupRequest) -> dict[str, object]:
    try:
        user = repository.create_user(
            email=payload.email, password_hash=hash_password(payload.password), display_name=payload.display_name
        )
        repository.ensure_user_project(user["id"])
        return issue_session(user)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/auth/login")
def login(payload: LoginRequest) -> dict[str, object]:
    user = repository.get_user_by_email(payload.email)
    if not user or not verify_password(payload.password, user.get("password_hash")):
        raise HTTPException(status_code=401, detail="email or password is incorrect")
    return issue_session(user)


@app.get("/auth/me")
def me(user: dict = Depends(current_user)) -> dict[str, str | None]:
    return public_user(user)


@app.post("/auth/logout", status_code=204)
def logout(authorization: str | None = Header(default=None), user: dict = Depends(current_user)) -> None:
    if authorization:
        claims = decode_access_token(authorization.removeprefix("Bearer ").strip(), settings.auth_secret)
        if claims:
            repository.revoke_session(claims["sid"], user["id"])


@app.patch("/auth/preferences")
def preferences(payload: PreferenceRequest, user: dict = Depends(current_user)) -> dict[str, str | None]:
    return public_user(repository.update_user_preferences(user["id"], theme_preference=payload.theme_preference))


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        **{
            f"provider_{name}": str(ready).lower()
            for name, ready in provider_readiness(settings).items()
        },
    }


@app.get("/readiness")
def readiness() -> dict:
    """Report whether generation modes can start without exposing credentials."""
    providers = provider_readiness(settings)
    return {
        "database_dialect": settings.database_url.split(":", 1)[0],
        "database_ready": True,
        "providers": providers,
        "fixture_generation_ready": True,
        "live_generation_ready": bool(providers["openrouter"]),
        "narration_ready": bool(providers["elevenlabs"]) and not settings.caption_only,
        "caption_only": settings.caption_only,
        "cloud_browser_ready": bool(providers["browserbase"]),
        "worker_mode": settings.worker_mode,
        "queue_ready": bool(settings.broker_url) if settings.worker_mode == "dramatiq" else True,
    }


@app.get("/providers")
def providers(_: dict = Depends(current_user)) -> list[dict]:
    """Expose provider wiring metadata only; keys are never an API response."""
    return repository.provider_configs()


@app.get("/projects")
def list_projects(user: dict = Depends(current_user)) -> list[dict]:
    """List studio workspaces and their request counts."""
    if settings.auth_required:
        repository.ensure_user_project(user["id"])
        return repository.list_projects(owner_id=user["id"])
    repository.ensure_local_project()
    return repository.list_projects()


@app.post("/projects")
def create_project(payload: ProjectRequest, user: dict = Depends(current_user)) -> dict:
    try:
        return repository.create_project(payload.name, owner_id=user["id"] if settings.auth_required else None)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.patch("/projects/{project_id}")
def rename_project(project_id: str, payload: ProjectRequest, user: dict = Depends(current_user)) -> dict:
    try:
        if settings.auth_required:
            return repository.rename_project_for_user(project_id, user["id"], payload.name)
        return repository.rename_project(project_id, payload.name)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="project not found") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/fixture-runs")
async def create_fixture_run(
    payload: FixtureRequest,
    user: dict = Depends(current_user),
) -> dict[str, str]:
    try:
        project_id = payload.project_id or (repository.ensure_user_project(user["id"])["id"] if settings.auth_required else repository.ensure_local_project()["id"])
        if settings.auth_required:
            repository.get_project_for_user(project_id, user["id"])
        request = repository.create_request(
            str(uuid4()),
            f"fixture://gate-{payload.gate}",
            payload.objective,
            project_id,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="project not found") from error
    run, created = repository.create_idempotent_run(request["id"], str(settings.artifact_root))
    if created:
        job = repository.enqueue_job(run["id"], "fixture", payload.model_dump())
        dispatch_generation_job(job["id"])
    return {"run_id": run["id"], "status": run["status"]}


@app.post("/runs")
async def create_generation_run(
    payload: GenerationRequest,
    user: dict = Depends(current_user),
) -> dict[str, str]:
    if url_generator is None:
        raise HTTPException(
            status_code=503,
            detail="OpenRouter structured planning is not configured; configure an active LLM provider first",
        )
    try:
        project_id = payload.project_id or (repository.ensure_user_project(user["id"])["id"] if settings.auth_required else repository.ensure_local_project()["id"])
        if settings.auth_required:
            repository.get_project_for_user(project_id, user["id"])
        request = repository.create_request(
            payload.request_id or str(uuid4()),
            str(payload.url),
            payload.objective,
            project_id,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="project not found") from error
    run, created = repository.create_idempotent_run(request["id"], str(settings.artifact_root))
    if created:
        job = repository.enqueue_job(run["id"], "url", payload.model_dump(mode="json"))
        dispatch_generation_job(job["id"])
    return {"run_id": run["id"], "status": run["status"]}


@app.post("/runs/{run_id}/retry")
async def retry_generation_run(
    run_id: str, payload: RetryRequest, user: dict = Depends(current_user),
) -> dict[str, str]:
    """Retry only a failed run, retaining its original evidence and lineage."""
    try:
        previous = repository.get_run_for_user(run_id, user["id"]) if settings.auth_required else repository.get_run(run_id)
        request = repository.get_request(previous["request_id"])
        previous_job = repository.job_for_run(run_id)
        retry = repository.create_retry_run(run_id, str(settings.artifact_root))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="run not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    retry_from_stage = payload.retry_from_stage
    if retry_from_stage is None:
        decision_path = settings.artifact_root / "runs" / run_id / "qa" / "repair-decision.json"
        if decision_path.exists():
            try:
                decision = json.loads(decision_path.read_text(encoding="utf-8"))
                retry_from_stage = {
                    "PLANNING": "PLANNING",
                    "PRODUCTION_EXECUTION": "EXECUTION",
                    "NARRATION": "NARRATION",
                    "PRESENTATION_PLANNED": "NARRATION",
                    "RENDERING": "RENDER",
                    "VIDEO_QA": "VIDEO_QA",
                }.get(decision.get("retry_from_stage"))
            except (OSError, ValueError):
                # A malformed diagnostic must never silently select a stage;
                # fall back to the explicit full retry path instead.
                retry_from_stage = None
    if retry_from_stage and request["url"].startswith("fixture://gate-"):
        raise HTTPException(status_code=422, detail="targeted retry is currently available for URL runs only")
    if request["url"].startswith("fixture://gate-"):
        gate = int(request["url"].rsplit("-", maxsplit=1)[1])
        job = repository.enqueue_job(
            retry["id"],
            "fixture",
            {
                "gate": gate,
                "objective": request["objective"],
                "render": payload.render
                if payload.render is not None
                else bool(((previous_job or {}).get("payload", {})).get("render", True)),
            },
        )
    else:
        if url_generator is None:
            raise HTTPException(status_code=503, detail="OpenRouter structured planning is not configured")
        original = (previous_job or {}).get("payload", {})
        # Retain the prior run's non-secret intent/settings so the retry remains
        # comparable. Side effects are intentionally *not* inherited: a user
        # must opt in again for every replayable external mutation.
        job_payload = {
            "allow_external_side_effects": payload.allow_external_side_effects,
            "cloud_discovery": payload.cloud_discovery
            if payload.cloud_discovery is not None
            else bool(original.get("cloud_discovery", False)),
            "stagehand_assist": payload.stagehand_assist
            if payload.stagehand_assist is not None
            else bool(original.get("stagehand_assist", False)),
            "max_pages": payload.max_pages
            if payload.max_pages is not None
            else int(original.get("max_pages", 6)),
            "render": payload.render if payload.render is not None else bool(original.get("render", True)),
            "credential_reference": payload.credential_reference
            if payload.credential_reference is not None
            else original.get("credential_reference"),
            "audience": payload.audience or original.get("audience", "product prospect"),
            "target_duration_seconds": payload.target_duration_seconds
            or int(original.get("target_duration_seconds", 120)),
            # Editorial/narration repairs regenerate only the script and its
            # evidence-bound storyboard from the existing trace. Other retry
            # boundaries retain their approved presentation artifact.
            "refresh_editorial": retry_from_stage == "NARRATION",
        }
        job = repository.enqueue_job(retry["id"], "url", job_payload)
    if retry_from_stage:
        try:
            RunArtifacts.clone_for_targeted_retry(
                settings.artifact_root, run_id, retry["id"], start_stage=retry_from_stage
            )
            repository.prepare_targeted_retry(retry["id"], retry_from_stage)
            repository.copy_run_evidence(run_id, retry["id"], through_stage=retry_from_stage)
        except (FileNotFoundError, KeyError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
    dispatch_generation_job(job["id"])
    return {"run_id": retry["id"], "status": retry["status"]}


def _owned_run_or_404(run_id: str, user: dict) -> dict:
    try:
        return repository.get_run_for_user(run_id, user["id"]) if settings.auth_required else repository.get_run(run_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="run not found") from error


@app.get("/runs/{run_id}/stages")
def get_run_stages(run_id: str, user: dict = Depends(current_user)) -> dict:
    """Inspect durable checkpoints before deciding whether retry or resume is safe."""
    run = _owned_run_or_404(run_id, user)
    return {"run": run, "job": repository.job_for_run(run_id), "stages": repository.stage_jobs(run_id)}


@app.post("/runs/{run_id}/resume")
def resume_generation_run(run_id: str, user: dict = Depends(current_user)) -> dict:
    _owned_run_or_404(run_id, user)
    try:
        job = repository.resume_run(run_id)
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    dispatch_generation_job(job["id"])
    return {"run_id": run_id, "job_id": job["id"], "status": "QUEUED"}


@app.post("/runs/{run_id}/cancel")
def cancel_generation_run(run_id: str, user: dict = Depends(current_user)) -> dict:
    _owned_run_or_404(run_id, user)
    try:
        run = repository.cancel_run(run_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"run_id": run_id, "status": run["status"]}


@app.delete("/runs/{run_id}", status_code=204)
def delete_empty_run(run_id: str, user: dict = Depends(current_user)) -> None:
    _owned_run_or_404(run_id, user)
    try:
        # Refuse to remove a database row if an unregistered filesystem
        # artifact remains. That file may be the only crash evidence left by a
        # terminated browser/render process.
        RunArtifacts.remove_empty_run_directory(settings.artifact_root, run_id)
        repository.delete_empty_terminal_run(run_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/runs/retention/purge-empty")
def purge_empty_runs(payload: RetentionRequest, user: dict = Depends(current_user)) -> dict:
    # User-specific retention avoids deleting another workspace's diagnostics.
    candidates = (
        repository.list_runs_for_user(user["id"], limit=100, offset=0)
        if settings.auth_required else repository.list_runs(limit=100, offset=0)
    )
    cutoff = datetime.now(UTC) - timedelta(seconds=payload.older_than_seconds)
    deleted: list[str] = []
    for candidate in candidates:
        try:
            if datetime.fromisoformat(candidate["updated_at"]) <= cutoff:
                RunArtifacts.remove_empty_run_directory(settings.artifact_root, candidate["id"])
                repository.delete_empty_terminal_run(candidate["id"])
                deleted.append(candidate["id"])
        except ValueError:
            continue
    return {"deleted_run_ids": deleted}


@app.post("/knowledge/invalidate")
def invalidate_product_knowledge(payload: KnowledgeInvalidationRequest, user: dict = Depends(current_user)) -> dict:
    product_key = str(payload.url)
    equivalent_keys = tuple(dict.fromkeys((product_key, product_key.rstrip("/"))))
    if settings.auth_required and not any(
        repository.user_has_product_access(key, user["id"]) for key in equivalent_keys
    ):
        raise HTTPException(status_code=404, detail="product knowledge not found in your workspace")
    invalidated = any(repository.invalidate_knowledge(key) for key in equivalent_keys)
    return {"url": product_key, "invalidated": invalidated}


@app.get("/runs")
def list_runs(
    limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0), user: dict = Depends(current_user)
) -> dict:
    return {
        "items": repository.list_runs_for_user(user["id"], limit=limit, offset=offset) if settings.auth_required else repository.list_runs(limit=limit, offset=offset),
        "limit": limit,
        "offset": offset,
    }


@app.get("/runs/{run_id}")
def get_run(run_id: str, user: dict = Depends(current_user)) -> dict:
    try:
        return repository.get_run_for_user(run_id, user["id"]) if settings.auth_required else repository.get_run(run_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="run not found") from error


@app.get("/runs/{run_id}/details")
def get_run_details(run_id: str, user: dict = Depends(current_user)) -> dict:
    try:
        if settings.auth_required:
            repository.get_run_for_user(run_id, user["id"])
        return repository.run_details(run_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="run not found") from error


@app.get("/runs/{run_id}/artifacts")
def get_artifacts(run_id: str, user: dict = Depends(current_user)) -> list[dict[str, str]]:
    try:
        repository.get_run_for_user(run_id, user["id"]) if settings.auth_required else repository.get_run(run_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="run not found") from error
    return repository.locations(run_id)


@app.get("/runs/{run_id}/video")
def get_final_video(run_id: str, user: dict = Depends(current_user)) -> FileResponse:
    if settings.auth_required:
        try:
            repository.get_run_for_user(run_id, user["id"])
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
    for artifact in repository.locations(run_id):
        if artifact["kind"] != "final_video":
            continue
        path = Path(artifact["location"]).resolve()
        try:
            path.relative_to(settings.artifact_root.resolve())
        except ValueError as error:
            raise HTTPException(status_code=403, detail="invalid artifact location") from error
        if path.exists():
            return FileResponse(path, media_type="video/mp4", filename=f"productlens-{run_id}.mp4")
    raise HTTPException(status_code=404, detail="final video is not available")
