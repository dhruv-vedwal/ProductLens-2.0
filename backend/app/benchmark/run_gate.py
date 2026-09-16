"""Executable browser gates. No external provider or network is required."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from playwright.async_api import async_playwright

from app.artifacts.store import RunArtifacts
from app.benchmark.domain_fixture_operations import create_booking, create_lead
from app.benchmark.fixtures import file_url
from app.contracts.models import (
    DemoTrace,
    OperationKind,
    Postcondition,
    SemanticOperation,
    Target,
)
from app.execution.engine import ExecutionEngine
from app.execution.playwright_adapter import PlaywrightAdapter


def op(
    kind: OperationKind, intent: str, test_id: str, value=None, *, post=None
) -> SemanticOperation:
    return SemanticOperation(
        kind=kind,
        intent=intent,
        target=Target(name=test_id, test_id=test_id),
        value=value,
        postconditions=post or [],
    )


async def engine_for(page, objective: str, artifacts: RunArtifacts) -> ExecutionEngine:
    return ExecutionEngine(
        PlaywrightAdapter(page),
        DemoTrace(run_id=str(uuid4()), objective=objective, started_at=datetime.now(UTC)),
        artifacts,
    )


async def gate1(page, artifacts: RunArtifacts) -> DemoTrace:
    await page.goto(file_url("gate1-primitives", "index.html"))
    engine = await engine_for(page, "Verify browser primitives", artifacts)
    # This fixture resets its in-page state on a round trip, so prove navigation first.
    await engine.run(op(OperationKind.CLICK, "Navigate to page two", "nav-link"))
    await page.get_by_test_id("nav-return").click()
    await page.wait_for_function("() => window.__testState.completed.navigation")
    actions = [
        op(OperationKind.CLICK, "Click", "click-btn"),
        op(OperationKind.FILL_TEXT, "Fill name", "fill-name", "Sarah Mitchell"),
        op(OperationKind.FILL_EMAIL, "Fill email", "fill-email", "sarah@acme.test"),
        op(OperationKind.SELECT_OPTION, "Select country", "select-country", "us"),
        op(OperationKind.CHECK, "Check subscription", "checkbox-subscribe"),
        op(OperationKind.CHOOSE_RADIO, "Choose enterprise", "radio-enterprise"),
        op(OperationKind.SELECT_DATE, "Set date", "date-single", "2026-09-15"),
        op(OperationKind.SELECT_DATE, "Set start date", "date-start", "2026-09-15"),
        op(OperationKind.SELECT_DATE, "Set end date", "date-end", "2026-09-18"),
        op(OperationKind.FILL_TEXT, "Search combobox", "combobox-input", "Sarah"),
    ]
    for action in actions:
        await engine.run(action)
    await engine.run(
        SemanticOperation(
            kind=OperationKind.CLICK,
            intent="Choose combobox option",
            target=Target(name="Sarah Mitchell", text="Sarah Mitchell"),
        )
    )
    await engine.run(op(OperationKind.FILL_PHONE, "Fill masked phone", "phone-input", "5551234567"))
    await engine.run(op(OperationKind.SCROLL_TO, "Real scroll into reveal", "scroll-target"))
    await engine.run(op(OperationKind.CLICK, "Confirm scroll reveal", "reveal-btn"))
    await engine.run(op(OperationKind.OPEN_MODAL, "Open modal", "modal-open"))
    await engine.run(op(OperationKind.CLICK, "Confirm modal", "modal-confirm"))
    await engine.run(op(OperationKind.SEARCH, "Search leads", "search-input", "Sarah"))
    await engine.run(op(OperationKind.APPLY_FILTER, "Filter active", "filter-active"))
    await engine.run(op(OperationKind.FILL_TEXT, "Fill submit name", "submit-name", "Sarah"))
    await engine.run(
        op(OperationKind.FILL_EMAIL, "Fill submit email", "submit-email", "sarah@acme.test")
    )
    await engine.run(op(OperationKind.SUBMIT, "Submit form", "submit-btn"))
    await engine.run(
        op(
            OperationKind.CLICK,
            "Start async wait",
            "wait-btn",
            post=[
                Postcondition(
                    kind="test_state",
                    expected="window.__testState.completed.wait",
                    timeout_ms=4_000,
                )
            ],
        )
    )
    await page.wait_for_function("() => Object.values(window.__testState.completed).every(Boolean)")
    return engine.complete()


async def gate2(page, artifacts: RunArtifacts) -> DemoTrace:
    await page.goto(file_url("gate2-workflow", "crm.html"))
    engine = await engine_for(page, "Create a lead and dependent booking", artifacts)
    e2e_lead = create_lead("Sarah Mitchell", "Acme Systems", "Website")
    e2e_lead[-1].postconditions = [
        Postcondition(
            kind="visible", expected=True, target=Target(name="Created lead", test_id="lead-row-0")
        )
    ]
    for action in e2e_lead:
        await engine.run(action)
    for action in create_booking(0, "Product demo", "2026-09-15", "2:00 PM"):
        await engine.run(action)
    await page.wait_for_function(
        "() => window.__testState.bookings.length > 0 && window.__testState.leads[0].hasBooking"
    )
    return engine.complete()


async def gate3(page, artifacts: RunArtifacts) -> DemoTrace:
    await page.goto(file_url("gate3-trace", "state-transitions.html"))
    engine = await engine_for(page, "Capture meaningful state transitions", artifacts)
    actions = [
        op(OperationKind.CLICK, "Enable notifications", "toggle"),
        op(OperationKind.CLICK, "Increase quantity", "qty-plus"),
        op(OperationKind.CLICK, "Advance item status", "advance-status"),
        op(OperationKind.CLICK, "Complete first task", "complete-task-0"),
        op(
            OperationKind.CLICK,
            "Complete upload",
            "start-upload",
            post=[
                Postcondition(
                    kind="test_state",
                    expected="window.__testState.events.some(e => e.action === 'upload_complete')",
                    timeout_ms=4_000,
                )
            ],
        ),
    ]
    for action in actions:
        await engine.run(action)
    fixture_events = await page.evaluate("() => window.__testState.events")
    expected = {"toggle", "stepper_increment", "status_advance", "task_complete", "upload_complete"}
    observed = {event["action"] for event in fixture_events}
    if not expected.issubset(observed):
        raise RuntimeError(f"Fixture semantic transitions missing: {expected - observed}")
    # The trace owns the same semantic intent and explicit before/after snapshot per action.
    if len(engine.trace.events) != len(actions) or any(
        not event.success for event in engine.trace.events
    ):
        raise RuntimeError("DemoTrace is incomplete")
    return engine.complete()


async def gate4(page, artifacts: RunArtifacts) -> DemoTrace:
    await page.goto(file_url("gate4-presentation", "dashboard.html"))
    engine = await engine_for(page, "Show dashboard actions with purposeful framing", artifacts)
    await engine.run(
        op(
            OperationKind.CLICK,
            "Open the compact profile control",
            "profile-btn",
            post=[Postcondition(kind="test_state", expected="window.__testState.profileClicked")],
        )
    )
    await engine.run(op(OperationKind.SCROLL_TO, "Bring row action into view", "edit-0"))
    await engine.run(op(OperationKind.CLICK, "Edit an account row", "edit-0"))
    await engine.run(
        op(
            OperationKind.OPEN_MODAL,
            "Open quick action menu",
            "fab",
            post=[
                Postcondition(
                    kind="visible",
                    expected=True,
                    target=Target(name="Quick action", test_id="quick-modal"),
                )
            ],
        )
    )
    await engine.run(op(OperationKind.CLOSE_MODAL, "Close quick action menu", "quick-close"))
    return engine.complete()


async def gate5(page, artifacts: RunArtifacts) -> DemoTrace:
    await page.goto(file_url("gate5-discovery", "hub.html"))
    engine = await engine_for(page, "Invite a new teammate", artifacts)
    await engine.run(
        SemanticOperation(
            kind=OperationKind.OPEN_NAVIGATION_ITEM,
            intent="Navigate directly to Users for teammate invitation",
            target=Target(name="Users", selector='nav a[href="section-users.html"]'),
            postconditions=[Postcondition(kind="url", expected="**/section-users.html")],
        )
    )
    await engine.run(
        op(
            OperationKind.FILL_EMAIL,
            "Enter teammate email",
            "invite-email",
            "new.teammate@northwind.test",
        )
    )
    await engine.run(
        op(OperationKind.SELECT_OPTION, "Choose teammate role", "invite-role", "Member")
    )
    await engine.run(
        op(
            OperationKind.SUBMIT,
            "Send teammate invitation",
            "invite-btn",
            post=[
                Postcondition(
                    kind="test_state", expected="window.__testState.invited !== undefined"
                )
            ],
        )
    )
    if "section-billing.html" in page.url or "section-settings.html" in page.url:
        raise RuntimeError("Targeted discovery visited an excluded area")
    return engine.complete()


async def gate6(page, artifacts: RunArtifacts) -> DemoTrace:
    await page.goto(file_url("gate6-e2e", "login.html"))
    engine = await engine_for(page, "Authenticate, create lead, and create booking", artifacts)
    await engine.run(
        op(OperationKind.FILL_EMAIL, "Enter login email", "login-email", "demo@northwind.test")
    )
    await engine.run(
        op(OperationKind.FILL_TEXT, "Enter login password", "login-password", "demo1234")
    )
    await engine.run(
        op(OperationKind.CHECK, "Complete authentication precondition", "login-captcha")
    )
    await engine.run(
        op(
            OperationKind.SUBMIT,
            "Authenticate",
            "login-btn",
            post=[Postcondition(kind="url", expected="**/dashboard.html")],
        )
    )
    await engine.run(
        SemanticOperation(
            kind=OperationKind.OPEN_NAVIGATION_ITEM,
            intent="Open Leads",
            target=Target(name="Leads", selector='nav a[href="leads.html"]'),
            postconditions=[Postcondition(kind="url", expected="**/leads.html")],
        )
    )
    e2e_lead = create_lead("Sarah Mitchell", "Acme Systems", "Website")
    e2e_lead[-1].postconditions = [
        Postcondition(
            kind="visible",
            expected=True,
            target=Target(name="Created lead", test_id="lead-row-0"),
        )
    ]
    for action in e2e_lead:
        await engine.run(action)
    await engine.run(
        SemanticOperation(
            kind=OperationKind.OPEN_NAVIGATION_ITEM,
            intent="Open Bookings",
            target=Target(name="Bookings", selector='nav a[href="bookings.html"]'),
            postconditions=[Postcondition(kind="url", expected="**/bookings.html")],
        )
    )
    for action in [
        op(OperationKind.OPEN_MODAL, "Open booking form", "new-booking-btn"),
        op(
            OperationKind.SELECT_OPTION,
            "Associate booking with created lead",
            "booking-lead",
            "Sarah Mitchell",
        ),
        op(OperationKind.CHOOSE_RADIO, "Choose Product demo", "booking-modal", post=[]),
        op(OperationKind.SELECT_DATE, "Choose booking date", "booking-date", "2026-09-15"),
        op(OperationKind.SELECT_OPTION, "Choose booking time", "booking-time", "2:00 PM"),
        op(
            OperationKind.SUBMIT,
            "Create booking",
            "booking-save",
            post=[
                Postcondition(
                    kind="test_state", expected="window.__testState.goldenBenchmarkPass === true"
                )
            ],
        ),
    ]:
        if action.intent == "Choose Product demo":
            action.target = Target(
                name="Product demo", selector='input[name="service"][value="Product demo"]'
            )
        await engine.run(action)
    return engine.complete()


async def _run_legacy(gate: int) -> DemoTrace:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            if gate == 1:
                return await gate1(page)
            if gate == 2:
                return await gate2(page)
            if gate == 3:
                return await gate3(page)
            raise ValueError("Only gates 1–3 are implemented; do not skip the reliability sequence")
        finally:
            await browser.close()


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate", type=int, required=True)
    parser.add_argument("--render", action="store_true")
    from app.benchmark.runner import run

    args = parser.parse_args()
    result = asyncio.run(run(args.gate, render_final=args.render))
    print(result.model_dump_json(indent=2))
