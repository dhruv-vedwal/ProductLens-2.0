from __future__ import annotations

from typing import Protocol


class LLMProvider(Protocol):
    async def structured(self, prompt: str, schema: type): ...


class TTSProvider(Protocol):
    async def synthesize(self, text: str, voice: str) -> bytes: ...


class BrowserProvider(Protocol):
    async def create_session(self) -> str: ...
