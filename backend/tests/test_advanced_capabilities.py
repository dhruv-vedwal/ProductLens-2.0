from datetime import UTC, datetime
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from productlens.contracts.models import (
    DemoTrace,
    DiscoveryBudget,
    OperationKind,
    Postcondition,
    SemanticOperation,
    Target,
)
from productlens.discovery.live import LiveDiscovery
from productlens.execution.engine import ExecutionEngine
from productlens.execution.playwright_adapter import PlaywrightAdapter
from productlens.planning.capability_resolution import resolve_capabilities


@pytest.mark.asyncio
async def test_discovery_exposes_unfamiliar_interaction_surfaces_without_recipe_names():
    fixture = (
        Path(__file__).resolve().parents[2]
        / "productlens-test-htmls"
        / "gate8-capabilities"
        / "index.html"
    ).as_uri()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        await page.goto(fixture)
        context = await LiveDiscovery().discover(
            page,
            "Explore rich text records embedded drawing graph",
            DiscoveryBudget(max_pages=1, max_actions=24, max_time_seconds=40),
        )
        resolution = resolve_capabilities(
            "edit rich text records embedded shadow surface drawing graph", context
        )
        kinds = {candidate.kind for candidate in resolution.candidates}
        assert {
            "rich_text",
            "table",
            "iframe",
            "shadow_dom",
            "canvas",
            "graph",
            "drag_drop",
        } <= kinds
        assert resolution.unresolved == []
        assert any(item.draggable for item in context.elements)
        assert any(item.dropzone for item in context.elements)
        assert any(item.shadow_host for item in context.elements)
        await browser.close()


@pytest.mark.asyncio
async def test_generic_kernel_executes_rich_text_iframe_shadow_canvas_and_graph_fixture():
    """The capability inventory must be executable, not merely descriptive."""
    fixture = (
        Path(__file__).resolve().parents[2]
        / "productlens-test-htmls"
        / "gate8-capabilities"
        / "index.html"
    ).as_uri()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        await page.goto(fixture)
        trace = DemoTrace(
            run_id="advanced-kernel",
            objective="Exercise observed capabilities",
            started_at=datetime.now(UTC),
        )
        engine = ExecutionEngine(PlaywrightAdapter(page), trace)
        await engine.run(
            SemanticOperation(
                kind=OperationKind.FILL_TEXT,
                intent="Write the observed project note",
                target=Target(name="Notes editor", role="textbox", selector="#rich-editor"),
                value="A grounded project note",
                postconditions=[
                    Postcondition(
                        kind="value",
                        expected="A grounded project note",
                        target=Target(name="Notes editor", role="textbox", selector="#rich-editor"),
                    )
                ],
            )
        )
        await engine.run(
            SemanticOperation(
                kind=OperationKind.CLICK,
                intent="Save the note",
                target=Target(name="Save note", role="button", selector="#save-note"),
                postconditions=[
                    Postcondition(
                        kind="test_state", expected="window.__testState.richTextSaved === true"
                    )
                ],
            )
        )
        await engine.run(
            SemanticOperation(
                kind=OperationKind.CLICK,
                intent="Reveal the embedded result",
                target=Target(name="Embedded action", role="button"),
                postconditions=[
                    Postcondition(
                        kind="text",
                        expected="Embedded result visible",
                        target=Target(name="Embedded result", text="Embedded result visible"),
                    )
                ],
            )
        )
        await engine.run(
            SemanticOperation(
                kind=OperationKind.CLICK,
                intent="Reveal the shadow result",
                target=Target(name="Reveal shadow result", role="button"),
                postconditions=[
                    Postcondition(kind="test_state", expected="window.__testState.shadow === true")
                ],
            )
        )
        await engine.run(
            SemanticOperation(
                kind=OperationKind.SCROLL_TO,
                intent="Bring the drawing surface into view",
                target=Target(name="Drawing surface", selector="#drawing-surface"),
            )
        )
        await engine.run(
            SemanticOperation(
                kind=OperationKind.POINTER_SEQUENCE,
                intent="Draw the observed reversible stroke",
                target=Target(name="Drawing surface", selector="#drawing-surface"),
                value={"pattern": "short_reversible_stroke"},
                postconditions=[
                    Postcondition(kind="test_state", expected="window.__testState.drawn === true")
                ],
            )
        )
        await engine.run(
            SemanticOperation(
                kind=OperationKind.DRAG,
                intent="Connect the observed graph nodes",
                target=Target(name="Source node", selector="#source-node"),
                value={
                    "destination": Target(
                        name="Connection target", selector="#connection-drop"
                    ).model_dump(mode="json")
                },
                postconditions=[
                    Postcondition(
                        kind="test_state", expected="window.__testState.connected === true"
                    )
                ],
            )
        )
        assert all(event.success for event in trace.events)
        assert len(trace.events) == 7
        await browser.close()
