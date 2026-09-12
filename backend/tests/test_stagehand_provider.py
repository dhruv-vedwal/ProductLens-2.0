import json
from pathlib import Path

import pytest

from productlens.providers.stagehand import StagehandCandidate, StagehandProvider, _origin_label, _same_origin


def test_stagehand_bridge_declares_v4_dependency():
    package = __import__("json").loads((__import__("pathlib").Path(__file__).resolve().parents[1] / "stagehand" / "package.json").read_text())
    assert package["dependencies"]["@browserbasehq/stagehand"].startswith("^4.")
    assert package["dependencies"]["zod"].startswith("^4.")


def test_stagehand_bridge_uses_the_v4_browser_and_create_api():
    bridge = (Path(__file__).resolve().parents[1] / "stagehand" / "observe.mjs").read_text()
    assert "browserbase.launch" in bridge
    assert "localBrowser.launch" in bridge
    assert "Stagehand.create" in bridge
    assert "browserbase.connect" in bridge
    assert "localBrowser.connect" in bridge
    assert "browserbaseConnectUrl" in bridge
    assert "browserbaseSessionID" in bridge
    assert "extensionId: read.stagehandExtensionId" in bridge
    assert "transient extension" in bridge
    assert "stagehand.extract" in bridge
    assert "new Stagehand" not in bridge
    # Model Gateway is selected by browserbase.launch; Stagehand.create must
    # not receive a second/placeholder provider key.
    assert "stagehandOptions.model" in bridge
    assert "apiKey: process.env.BROWSERBASE_API_KEY" not in bridge.split("Stagehand.create", 1)[1]


def test_stagehand_origin_check_allows_only_secure_redirects():
    assert _same_origin("http://example.test", "https://example.test/")
    assert _same_origin("https://example.test", "https://example.test/path")
    assert _same_origin("https://example.test", "https://example.test:443/path")
    assert not _same_origin("https://example.test", "http://example.test/")
    assert not _same_origin("https://example.test", "https://other.test/")


def test_stagehand_origin_diagnostic_never_contains_path_or_query():
    assert _origin_label("https://example.test/private?token=secret") == "https://example.test:443"


class Process:
    returncode = 0

    async def communicate(self, payload):
        assert json.loads(payload)["environment"] == "LOCAL"
        return (
            b'{"version":2,"environment":"LOCAL","observedUrl":"https://example.test/page","candidates":[{"selector":"#invite","description":"Invite","method":"click","arguments":[]}],"analysis":{"visibleSections":["Team members"],"meaningfulControls":["Invite member"],"safeNextActions":["Open member details"]},"metrics":{"totalPromptTokens":0}}',
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
    assert observation.analysis is not None
    assert observation.analysis.visible_sections == ["Team members"]
    assert observation.analysis.safe_next_actions == ["Open member details"]


@pytest.mark.asyncio
async def test_stagehand_keeps_observed_actions_when_advisory_extraction_fails(monkeypatch, tmp_path: Path):
    bridge = tmp_path / "observe.mjs"
    bridge.write_text("// bridge")

    class PartialProcess(Process):
        async def communicate(self, payload):
            return (
                b'{"version":2,"environment":"LOCAL","observedUrl":"https://example.test",'
                b'"candidates":[{"selector":"#details","description":"Open details",'
                b'"method":"click","arguments":[]}],"analysis":null,'
                b'"analysisError":"No object generated: response did not match schema","metrics":{}}',
                b"",
            )

    async def create(*args, **kwargs):
        return PartialProcess()

    monkeypatch.setattr("productlens.providers.stagehand.asyncio.create_subprocess_exec", create)
    observation = await StagehandProvider(bridge=bridge).observe(
        url="https://example.test", instruction="find details"
    )
    assert observation.candidates[0].selector == "#details"
    assert observation.analysis is None
    assert observation.analysis_error and "schema" in observation.analysis_error


@pytest.mark.asyncio
async def test_cloud_observation_forwards_existing_cdp_endpoint(monkeypatch, tmp_path: Path):
    bridge = tmp_path / "observe.mjs"
    bridge.write_text("// bridge")

    class CloudProcess(Process):
        async def communicate(self, payload):
            body = json.loads(payload)
            assert body["environment"] == "BROWSERBASE"
            assert body["browserbaseSessionID"] == "session-1"
            assert body["browserbaseConnectUrl"] == "wss://connect.browserbase.com/session-1"
            assert body["stagehandExtensionId"] == "extension-1"
            return (b'{"version":2,"environment":"BROWSERBASE","observedUrl":"https://example.test","candidates":[],"metrics":{}}', b"")

    async def create(*args, **kwargs):
        return CloudProcess()

    monkeypatch.setattr("productlens.providers.stagehand.asyncio.create_subprocess_exec", create)
    observation = await StagehandProvider(
        bridge=bridge, browserbase_api_key="test-key", browserbase_project_id="project-1"
    ).observe(
        url="https://example.test",
        instruction="observe",
        environment="BROWSERBASE",
        browserbase_session_id="session-1",
        browserbase_connect_url="wss://connect.browserbase.com/session-1",
        browserbase_extension_id="extension-1",
    )
    assert observation.environment == "BROWSERBASE"


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


@pytest.mark.asyncio
async def test_stagehand_observed_action_reuses_a_candidate_without_a_free_form_instruction(monkeypatch, tmp_path: Path):
    bridge = tmp_path / "observe.mjs"
    bridge.write_text("// bridge")

    class ActionProcess(Process):
        async def communicate(self, payload):
            body = json.loads(payload)
            assert body["mode"] == "act_observed"
            assert "instruction" not in body
            assert body["action"]["selector"] == "#details"
            return (
                b'{"version":3,"mode":"act_observed","environment":"LOCAL","observedUrl":"https://example.test/page","result":{"success":true,"message":"opened","action":"Open details"}}',
                b"",
            )

    async def create(*args, **kwargs):
        return ActionProcess()

    monkeypatch.setattr("productlens.providers.stagehand.asyncio.create_subprocess_exec", create)
    result = await StagehandProvider(bridge=bridge).act_observed(
        url="https://example.test",
        candidate=StagehandCandidate(selector="#details", description="Open details", method="click", arguments=[]),
    )

    assert result.success
    assert result.action == "Open details"
    assert result.observed_url == "https://example.test/page"
