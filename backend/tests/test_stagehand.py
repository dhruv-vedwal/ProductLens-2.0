import asyncio

import pytest

from app.providers.errors import ProviderError
from app.providers.stagehand import StagehandProvider


@pytest.mark.asyncio
async def test_stagehand_timeout_terminates_the_observation_process(monkeypatch, tmp_path):
    bridge = tmp_path / "observe.mjs"
    bridge.write_text("// test bridge", encoding="utf-8")

    class Process:
        returncode = None
        terminated = False

        async def communicate(self, _payload):
            await asyncio.sleep(3600)

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    process = Process()

    async def create_process(*_args, **_kwargs):
        return process

    original_wait_for = asyncio.wait_for

    async def timed_wait_for(awaitable, timeout):
        if timeout == 90:
            awaitable.close()
            raise TimeoutError("observation timeout")
        return await original_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(asyncio, "wait_for", timed_wait_for)
    provider = StagehandProvider(
        bridge=bridge,
        browserbase_api_key="test-reference",
    )

    with pytest.raises(ProviderError, match="failed to start"):
        await provider.observe(
            url="https://example.test", instruction="Observe the product", environment="BROWSERBASE"
        )
    assert process.terminated
