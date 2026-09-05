import pytest
from playwright.async_api import async_playwright

from productlens.contracts.models import ObservedElement, PageKnowledge
from productlens.presentation.viewport import choose_viewport, probe_viewport_candidates


def _element(name: str, tag: str = "button") -> ObservedElement:
    return ObservedElement(tag=tag, name=name, selector="button", actionable=True)


def test_viewport_selection_keeps_native_browser_zoom_and_records_evidence():
    decision = choose_viewport([_element("Create lead"), _element("Email", "input")], "Create lead")
    assert decision.browser_zoom_percent == 100
    assert decision.viewport.width == 1440
    assert any("objective-relevant" in item for item in decision.evidence)


def test_dense_application_uses_wider_capture_without_css_zoom():
    decision = choose_viewport([_element(f"Action {number}") for number in range(45)], "Review actions")
    assert decision.viewport.width == 1920


def test_full_walkthrough_does_not_assume_the_widest_native_capture_frame():
    decision = choose_viewport([_element("Timeline")], "Create a full walkthrough of each tab")
    assert (decision.viewport.width, decision.viewport.height, decision.browser_zoom_percent) == (1600, 1000, 100)
    assert decision.browser_zoom_percent == 100


def test_viewport_selection_accounts_for_the_densest_discovered_story_page():
    dense_page = PageKnowledge(
        url="https://example.test/settings", title="Settings", purpose="Configure settings",
        visible_sections=[f"Section {index}" for index in range(12)],
        actionable_controls=[f"Control {index}" for index in range(24)],
        fingerprint="settings-page",
    )
    decision = choose_viewport([_element("Home")], "Review settings", [dense_page])
    assert decision.viewport.width == 1920
    assert "discovered pages evaluated: 1" in decision.evidence
    assert decision.browser_zoom_percent == 100


@pytest.mark.asyncio
async def test_live_probe_prefers_readable_fixed_width_content_over_widest_viewport():
    """A live layout probe must correct a static full-walkthrough preference."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        await page.set_content("""
            <main style='width:min(960px, calc(100vw - 64px)); margin:0 auto'>
              <h1>Product overview</h1><p>Readable product context.</p>
              <button>Continue</button><button>Settings</button>
            </main>
        """)
        decision, probes = await probe_viewport_candidates(
            page, [_element("Continue"), _element("Settings")], "Create a full walkthrough"
        )
        await context.close()
        await browser.close()
    assert decision.viewport.width == 1440
    assert decision.browser_zoom_percent == 100
    assert [probe["viewport"]["width"] for probe in probes] == [1440, 1600, 1920]
    assert any("live responsive-layout probe" in evidence for evidence in decision.evidence)
