from __future__ import annotations

from pydantic import BaseModel, Field


class ProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
