import pytest
from playwright.async_api import async_playwright

from productlens.contracts.models import ActionCapability, FormSchema, Target
from productlens.planning.rehearsal import derive_outcome_witness
from productlens.services.generation import (
    _rehearsal_outcome_candidates,
    _rehearsal_post_submit_state,
)


@pytest.mark.asyncio
async def test_outcome_capture_finds_a_generated_value_in_a_stable_card_layout():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content(
            '<main><div data-testid="record-card">Demo record K7Q9P</div></main>'
        )
        observed = await _rehearsal_outcome_candidates(
            page,
            submitted_values=["Demo record K7Q9P"],
        )
        await context.close()
        await browser.close()

    capability = ActionCapability(
        kind="form",
        purpose="New record",
        source_url="https://example.test/records",
        entry_target=Target(name="New record", selector="#new"),
        form_schema=FormSchema(source_url="https://example.test/records"),
        submit_target=Target(name="Save", selector="#save"),
    )
    witnessed = derive_outcome_witness(
        capability,
        before_text="Records New record Save",
        observed=observed,
        submitted_values=["Demo record K7Q9P"],
    )

    assert witnessed is not None
    assert witnessed.verified
    assert witnessed.outcome_target.name == "verified created record"


@pytest.mark.asyncio
async def test_post_submit_diagnostic_reports_only_structural_validation_state():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content(
            '<div role="dialog"><input aria-invalid="true"><div role="alert">Required</div></div>'
        )
        state = await _rehearsal_post_submit_state(page)
        await context.close()
        await browser.close()

    assert state == {
        "visible_dialog_count": 1,
        "invalid_field_count": 1,
        "visible_alert_count": 1,
    }
