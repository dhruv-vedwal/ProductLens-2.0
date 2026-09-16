from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


class UnderstandingRequest(BaseModel):
    """Small non-recording scan used before a user commits to generation."""

    url: HttpUrl
    prompt: str = Field(default="", max_length=2_000)
    audience: str | None = Field(default=None, max_length=120)
    max_pages: int = Field(default=3, ge=1, le=4)
    use_stagehand: bool = True
    force_refresh: bool = False


class KnowledgeInvalidationRequest(BaseModel):
    url: HttpUrl
