from datetime import UTC, datetime
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from app.contracts.models import (
    DemoTrace,
    DiscoveryBudget,
    OperationKind,
    Postcondition,
    SemanticOperation,
    Target,
)
from app.discovery.live import LiveDiscovery
from app.execution.engine import ExecutionEngine
from app.execution.playwright_adapter import PlaywrightAdapter
from app.planning.capability_resolution import resolve_capabilities


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


@pytest.mark.asyncio
async def test_canvas_keypress_accepts_grounded_target_with_plain_text_value():
    """Planner encodings with a grounded canvas target must type, not press text as a key."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 900, "height": 600})
        await page.set_content(
            """
            <canvas id="surface" width="800" height="500"></canvas>
            <textarea id="editor" aria-label="Canvas text editor"
              style="position:fixed;left:20px;top:20px;width:240px;height:40px"></textarea>
            """
        )
        await page.locator("#editor").focus()
        result = await PlaywrightAdapter(page).execute(
            SemanticOperation(
                kind=OperationKind.KEY_PRESS,
                intent="Type the observed canvas label",
                target=Target(name="drawing surface", selector="canvas"),
                value="User Interface",
            )
        )
        assert result["scope"] == "focused-editable"
        assert await page.locator("#editor").input_value() == "User Interface"
        await browser.close()


@pytest.mark.asyncio
async def test_canvas_text_hand_off_reuses_observed_placement_when_focus_is_lost():
    """A trace checkpoint must not make every canvas label land at the center."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 900, "height": 600})
        await page.set_content(
            """
            <canvas id="surface" width="800" height="500"
              style="position:fixed;left:40px;top:40px;width:800px;height:500px"></canvas>
            <script>
              const surface = document.querySelector('#surface');
              surface.addEventListener('click', event => {
                const editor = document.createElement('textarea');
                editor.dataset.x = String(event.clientX);
                editor.dataset.y = String(event.clientY);
                editor.style = `position:fixed;left:${event.clientX}px;top:${event.clientY}px`;
                editor.addEventListener('keydown', e => {
                  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                    const label = document.createElement('span');
                    label.dataset.x = editor.dataset.x;
                    label.dataset.y = editor.dataset.y;
                    label.textContent = editor.value;
                    document.body.append(label);
                    editor.remove();
                  }
                });
                document.body.append(editor);
                editor.focus();
              });
            </script>
            """
        )
        adapter = PlaywrightAdapter(page)
        target = Target(name="drawing surface", selector="canvas")
        await adapter.execute(
            SemanticOperation(
                kind=OperationKind.POINTER_SEQUENCE,
                intent="Place the first text label",
                target=target,
                value={
                    "pattern": "text_placement",
                    "relative_points": [{"x": 0.2, "y": 0.2}, {"x": 0.22, "y": 0.2}],
                },
            )
        )
        # A browser checkpoint/screenshot may steal focus before the next
        # operation. The adapter must restore the exact observed placement.
        await page.evaluate("document.activeElement && document.activeElement.blur()")
        await adapter.execute(
            SemanticOperation(
                kind=OperationKind.KEY_PRESS,
                intent="Type the first canvas label",
                target=target,
                value="First",
            )
        )
        labels = await page.locator("span").all()
        assert len(labels) == 1
        assert await labels[0].get_attribute("data-x") == "200"
        assert await labels[0].get_attribute("data-y") == "140"
        await browser.close()


@pytest.mark.asyncio
async def test_idempotent_state_control_is_not_toggled_when_already_selected():
    """A continuation must observe selected controls instead of clicking them off."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content(
            """
            <button id="tool" aria-pressed="true"
              onclick="this.setAttribute('aria-pressed', 'false'); window.clicks=(window.clicks||0)+1">
              Selection
            </button>
            """
        )
        trace = DemoTrace(
            run_id="idempotent-control",
            objective="Observe the selected control",
            started_at=datetime.now(UTC),
        )
        event = await ExecutionEngine(PlaywrightAdapter(page), trace).run(
            SemanticOperation(
                kind=OperationKind.CLICK,
                intent="Select the already active tool",
                target=Target(name="Selection", selector="#tool", role="button"),
                postconditions=[
                    Postcondition(
                        kind="test_state",
                        expected="selected",
                        target=Target(name="Selection", selector="#tool", role="button"),
                    )
                ],
            )
        )
        assert event.success
        assert event.recovery and event.recovery[-1]["strategy"] == (
            "state_already_satisfied_before_dispatch"
        )
        assert await page.locator("#tool").get_attribute("aria-pressed") == "true"
        assert await page.evaluate("window.clicks || 0") == 0
        await browser.close()


@pytest.mark.asyncio
async def test_semantic_state_phrase_is_normalized_before_browser_verification():
    """Human-readable planner state phrases remain portable, not JS errors."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content(
            '<button id="tool" aria-pressed="true">Rectangle</button>'
        )
        trace = DemoTrace(
            run_id="semantic-state-phrase",
            objective="Observe a selected drawing control",
            started_at=datetime.now(UTC),
        )
        event = await ExecutionEngine(PlaywrightAdapter(page), trace).run(
            SemanticOperation(
                kind=OperationKind.CLICK,
                intent="Use the observed rectangle tool",
                target=Target(name="Rectangle", selector="#tool", role="button"),
                postconditions=[
                    Postcondition(
                        kind="test_state",
                        expected="Rectangle tool is active",
                        target=Target(name="Rectangle", selector="#tool", role="button"),
                    )
                ],
            )
        )
        assert event.success
        await browser.close()
