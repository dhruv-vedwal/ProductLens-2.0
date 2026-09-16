from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class LLMProvider(Protocol):
    """Provider boundary for schema-constrained reasoning.

    The planner owns the schema and validation contract; a provider only
    turns a prompt into an instance of that schema. Keeping this generic
    prevents callers from falling back to untyped dictionaries at the
    planning boundary.
    """

    async def structured(self, prompt: str, schema: type[SchemaT]) -> SchemaT: ...


class TTSProvider(Protocol):
    async def synthesize(self, text: str, voice: str | None = None) -> bytes: ...


class BrowserProvider(Protocol):
    async def create_session(self) -> str: ...
