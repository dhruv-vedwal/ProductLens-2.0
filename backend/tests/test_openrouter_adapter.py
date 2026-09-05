import httpx
import pytest

from productlens.contracts.models import ObjectiveSpec
from productlens.providers.errors import ProviderError
from productlens.providers.openrouter import OpenRouterProvider


class Client:
    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, *args, **kwargs):
        request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        response = httpx.Response(429, request=request)
        raise httpx.HTTPStatusError("rate limited", request=request, response=response)


@pytest.mark.asyncio
async def test_openrouter_maps_transport_error_to_safe_provider_error(monkeypatch):
    monkeypatch.setattr("productlens.providers.openrouter.httpx.AsyncClient", Client)
    with pytest.raises(ProviderError, match=r"openrouter provider failure \(429\)"):
        await OpenRouterProvider("key", "model").structured("plan", ObjectiveSpec)
