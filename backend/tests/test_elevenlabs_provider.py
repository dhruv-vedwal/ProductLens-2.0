import httpx
import pytest

from app.providers.elevenlabs import ElevenLabsProvider
from app.providers.errors import ProviderError


class Response:
    status_code = 402

    def raise_for_status(self):
        request = httpx.Request("POST", "https://api.elevenlabs.io/v1/text-to-speech/voice")
        raise httpx.HTTPStatusError("payment required", request=request, response=self)


class Client:
    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, *args, **kwargs):
        return Response()


@pytest.mark.asyncio
async def test_elevenlabs_maps_rejected_synthesis_to_safe_provider_error(monkeypatch):
    monkeypatch.setattr("app.providers.elevenlabs.httpx.AsyncClient", Client)
    with pytest.raises(ProviderError, match=r"elevenlabs provider failure \(402\)"):
        await ElevenLabsProvider("key", "voice").synthesize("hello")
