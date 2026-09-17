"""Thin FastAPI application entrypoint for ProductLens 2.0."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps_state import (
    credential_service,
    dispatch_generation_job,
    jobs,
    preflight_service,
    repository,
    resolve_cloud_discovery,
    settings,
    url_generator,
)
from app.api.modules.auth.routes import router as auth_router
from app.api.modules.credentials.routes import router as credentials_router
from app.api.modules.projects.routes import router as projects_router
from app.api.modules.runs.routes import router as runs_router
from app.api.modules.runs.schemas import GenerationRequest, RetryRequest
from app.api.modules.system.routes import router as system_router
from app.api.modules.understanding.routes import router as understanding_router

app = FastAPI(title="ProductLens 2.0 Generation Engine")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
    ],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)
app.include_router(auth_router)
app.include_router(system_router)
app.include_router(understanding_router)
app.include_router(credentials_router)
app.include_router(projects_router)
app.include_router(runs_router)

__all__ = [
    "GenerationRequest",
    "RetryRequest",
    "app",
    "credential_service",
    "dispatch_generation_job",
    "jobs",
    "preflight_service",
    "repository",
    "resolve_cloud_discovery",
    "settings",
    "url_generator",
]
