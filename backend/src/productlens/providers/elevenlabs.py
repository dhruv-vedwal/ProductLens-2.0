from __future__ import annotations

import httpx

from productlens.providers.errors import ProviderError


class ElevenLabsProvider:
    def __init__(self, api_key: str, voice_id: str, model: str = "eleven_multilingual_v2"):
        self.api_key, self.voice_id, self.model = api_key, voice_id, model

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        voice_id = voice or self.voice_id
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": self.api_key, "accept": "audio/mpeg"},
                json={"text": text, "model_id": self.model},
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                raise ProviderError(
                    "elevenlabs",
                    error.response.status_code,
                    "speech synthesis request was rejected",
                ) from error
        return response.content
