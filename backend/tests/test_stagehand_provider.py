import json
from pathlib import Path

import pytest

from productlens.providers.stagehand import StagehandProvider


def test_stagehand_bridge_declares_v4_dependency():
    package = __import__("json").loads((__import__("pathlib").Path(__file__).resolve().parents[1] / "stagehand" / "package.json").read_text())
    assert package["dependencies"]["@browserbasehq/stagehand"].startswith("^4.")


def test_stagehand_bridge_uses_the_v4_browser_and_create_api():
    bridge = (Path(__file__).resolve().parents[1] / "stagehand" / "observe.mjs").read_text()
    assert "browserbase.launch" in bridge
    assert "localBrowser.launch" in bridge
    assert "Stagehand.create" in bridge
    assert "new Stagehand" not in bridge
    # Model Gateway is selected by browserbase.launch; Stagehand.create must
    # not receive a second/placeholder provider key.
    assert "stagehandOptions.model" in bridge
    assert "apiKey: process.env.BROWSERBASE_API_KEY" not in bridge.split("Stagehand.create", 1)[1]


class Process:
    returncode = 0

    async def communicate(self, payload):
        assert json.loads(payload)["environment"] == "LOCAL"
        return (
            b'{"version":1,"environment":"LOCAL","observedUrl":"https://example.test/page","candidates":[{"selector":"#invite","description":"Invite","method":"click","arguments":[]}],"metrics":{"totalPromptTokens":0}}',
            b"",
        )


@pytest.mark.asyncio
async def test_stagehand_observation_is_normalized_without_execution(monkeypatch, tmp_path: Path):
    bridge = tmp_path / "observe.mjs"
    bridge.write_text("// bridge")

    async def create(*args, **kwargs):
        assert kwargs["env"]
        return Process()

    monkeypatch.setattr("productlens.providers.stagehand.asyncio.create_subprocess_exec", create)
    observation = await StagehandProvider(model="openai/gpt-4o", bridge=bridge).observe(
        url="https://example.test", instruction="find invite"
    )
    assert observation.candidates[0].selector == "#invite"
    assert observation.metrics["totalPromptTokens"] == 0
    assert observation.observed_url == "https://example.test/page"


@pytest.mark.asyncio
async def test_stagehand_rejects_observation_that_leaves_requested_origin(monkeypatch, tmp_path: Path):
    bridge = tmp_path / "observe.mjs"
    bridge.write_text("// bridge")

    class WrongOriginProcess(Process):
        async def communicate(self, payload):
            return (b'{"version":1,"environment":"BROWSERBASE","observedUrl":"https://attacker.test","candidates":[],"metrics":{}}', b"")

    async def create(*args, **kwargs):
        return WrongOriginProcess()

    monkeypatch.setattr("productlens.providers.stagehand.asyncio.create_subprocess_exec", create)
    with pytest.raises(Exception, match="invalid observation"):
        await StagehandProvider(bridge=bridge, browserbase_api_key="test-key").observe(
            url="https://example.test", instruction="observe", environment="BROWSERBASE"
        )


@pytest.mark.asyncio
async def test_stagehand_cloud_observation_requires_browserbase_key(tmp_path: Path):
    bridge = tmp_path / "observe.mjs"
    bridge.write_text("// bridge")
    with pytest.raises(Exception, match="Browserbase API key"):
        await StagehandProvider(model="openai/gpt-4o", bridge=bridge).observe(
            url="https://example.test", instruction="find invite", environment="BROWSERBASE"
        )
