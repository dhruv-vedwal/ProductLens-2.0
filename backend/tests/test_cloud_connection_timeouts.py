"""Guard browser-provider calls from becoming unbounded worker hangs."""

import ast
from pathlib import Path

import pytest

from productlens.planning.production import ProductionPlanningService
from productlens.services.generation import UrlGenerationService


def test_every_cloud_cdp_connection_has_a_native_playwright_timeout():
    source = Path("src/productlens/services/generation.py").read_text(encoding="utf-8")
    calls = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "connect_over_cdp"
    ]
    assert calls
    assert all(any(keyword.arg == "timeout" for keyword in call.keywords) for call in calls)


def test_cloud_discovery_bounds_viewport_navigation_and_authentication_before_exploration():
    source = Path("src/productlens/services/generation.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    bounded_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "wait_for"
    ]
    bounded_source = "\n".join(ast.unparse(node) for node in bounded_calls)

    assert "page.set_viewport_size" in bounded_source
    assert "page.goto" in bounded_source
    assert "self.credential_service.authenticate_if_required" in bounded_source


def test_cloud_discovery_bounds_cdp_and_provider_teardown_after_a_failure():
    source = Path("src/productlens/services/generation.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    bounded_source = "\n".join(
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "wait_for"
    )

    assert "remote.close" in bounded_source
    assert "self.browserbase_provider.close_session" in bounded_source


@pytest.mark.asyncio
async def test_cloud_lease_guard_releases_session_outside_the_cdp_operation():
    class Provider:
        released: list[str] = []

        async def close_session(self, session_id: str) -> None:
            self.released.append(session_id)

    provider = Provider()
    service = UrlGenerationService(
        ProductionPlanningService(None), browserbase_provider=provider,
    )

    await service._release_cloud_session_after(
        "session-1", seconds=0, reason="test", run_id="run-1",
    )

    assert provider.released == ["session-1"]
