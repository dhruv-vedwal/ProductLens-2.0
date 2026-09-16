from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator


class CredentialCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    username: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=320)
    project_id: str | None = None

    @field_validator("name")
    @classmethod
    def credential_name_shape(cls, value: str) -> str:
        normalized = value.strip().lower().replace(" ", "-")
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", normalized):
            raise ValueError("credential name must be letters, numbers, underscore, or hyphen")
        return normalized
