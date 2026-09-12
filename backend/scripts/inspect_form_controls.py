"""Inspect a reversible form/control state in Browserbase without submitting.

This diagnostic is intentionally product-neutral: it opens one named visible
control, reports its accessible/DOM form evidence, and optionally opens one
named selector to report its visible choices. It never types, chooses, submits,
or records the application.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from productlens.config.settings import Settings
from productlens.credentials.service import EnvironmentCredentialService
from productlens.discovery.live import LiveDiscovery
from productlens.providers.browserbase import BrowserbaseProvider


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect a visible Browserbase form without mutations")
    parser.add_argument("--url")
    parser.add_argument("--entry", help="Visible control that reversibly opens the form")
    parser.add_argument("--field", help="Optional accessible combobox/field whose choices should be observed")
    parser.add_argument("--credential-reference")
    parser.add_argument(
        "--request-file",
        help="Optional normal job-request JSON; reads only url and opaque credential reference.",
    )
    parser.add_argument(
        "--run-id",
        help="Optional persisted run ID; reuses a discovered form capability's exact source URL and entry name.",
    )
    parser.add_argument("--artifact-root", default="artifacts")
    args = parser.parse_args()
    if args.request_file:
        request = json.loads(Path(args.request_file).read_text(encoding="utf-8"))
        args.url = args.url or request.get("url")
        args.credential_reference = args.credential_reference or request.get("credential_reference")
    if args.run_id:
        context_path = Path(args.artifact_root) / "runs" / args.run_id / "discovery" / "product-context.json"
        context = json.loads(context_path.read_text(encoding="utf-8"))
        capabilities = [item for item in context.get("capabilities", []) if item.get("kind") == "form"]
        if args.entry:
            capabilities = [item for item in capabilities if item.get("purpose") == args.entry]
        if len(capabilities) != 1:
            parser.error("--run-id must resolve exactly one discovered form capability; provide --entry to disambiguate")
        capability = capabilities[0]
        args.url = capability.get("source_url") or args.url
        args.entry = (capability.get("entry_target") or {}).get("name") or args.entry
    if not args.url or not args.entry:
        parser.error("--url/--entry or --run-id with one discovered form capability is required")
    return args


async def _visible_locator(locator):
    for index in range(await locator.count()):
        candidate = locator.nth(index)
        if await candidate.is_visible():
            return candidate
    return None


async def inspect(args: argparse.Namespace) -> dict[str, object]:
    settings = Settings.from_environment()
    if not settings.browserbase_api_key:
        raise RuntimeError("Browserbase is not configured")
    provider = BrowserbaseProvider(
        settings.browserbase_api_key,
        settings.browserbase_project_id,
        session_timeout_seconds=settings.browserbase_session_timeout_seconds,
    )
    credentials = EnvironmentCredentialService()
    async with async_playwright() as playwright:
        session = await provider.create_session_info()
        remote = None
        try:
            remote = await playwright.chromium.connect_over_cdp(session.connect_url)
            context = remote.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
            await page.set_viewport_size({"width": 1440, "height": 900})
            # A cold cloud browser can take longer than Playwright's 30-second
            # default before an authenticated SPA reaches DOMContentLoaded.
            # This diagnostic remains bounded and non-mutating, but should not
            # misclassify provider startup latency as missing form evidence.
            page.set_default_navigation_timeout(90_000)
            await page.goto(args.url, wait_until="domcontentloaded")
            authenticated = await credentials.authenticate_if_required(page, args.credential_reference)
            if authenticated and page.url.rstrip("/") != args.url.rstrip("/"):
                await page.goto(args.url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=7_000)
            except PlaywrightError:
                await page.wait_for_timeout(700)
            entry_locator = page.get_by_role("button", name=args.entry, exact=True)
            try:
                await entry_locator.first.wait_for(state="visible", timeout=7_000)
            except PlaywrightError:
                pass
            entry = await _visible_locator(entry_locator)
            if entry is None:
                raise RuntimeError(f"Visible entry control not found: {args.entry}")
            await entry.click()
            await page.wait_for_timeout(700)
            scope = await _visible_locator(page.get_by_role("dialog")) or await _visible_locator(page.locator("form"))
            if scope is None:
                raise RuntimeError("Entry control did not open a visible dialog or form")
            fields = await scope.locator(
                "input,select,textarea,[contenteditable='true'],[role='combobox'],[role='textbox'],[role='searchbox']"
            ).evaluate_all(
                """nodes => nodes.map(node => {
                    const text = value => (value || '').replace(/\\s+/g, ' ').trim();
                    const ancestors = [];
                    let parent = node.parentElement;
                    for (let depth = 0; parent && depth < 5; depth += 1, parent = parent.parentElement) {
                        ancestors.push({
                            tag: parent.tagName.toLowerCase(), className: parent.className || '',
                            role: parent.getAttribute('role') || '', text: text(parent.innerText).slice(0, 260),
                        });
                    }
                    return {
                        tag: node.tagName.toLowerCase(), role: node.getAttribute('role') || '',
                        name: node.getAttribute('aria-label') || node.getAttribute('name') || node.getAttribute('placeholder') || '',
                        required: node.required || node.getAttribute('aria-required') === 'true',
                        describedBy: node.getAttribute('aria-describedby') || '',
                        outer: node.outerHTML.slice(0, 700), ancestors,
                    };
                })"""
            )
            discovery_schema = await LiveDiscovery()._enrich_choice_options(
                page, await LiveDiscovery()._form_schema_from_scope(scope, page.url)
            )
            choices: list[str] = []
            if args.field:
                field = await _visible_locator(page.get_by_role("combobox", name=args.field, exact=True))
                if field is None:
                    field = await _visible_locator(page.get_by_label(args.field, exact=True))
                if field is None:
                    raise RuntimeError(f"Visible field not found: {args.field}")
                await field.click()
                try:
                    await field.press("ArrowDown")
                except PlaywrightError:  # A reversible expansion optimization only.
                    pass
                await page.wait_for_timeout(700)
                choices = await page.locator("[role='option'], [role='listbox'] li, [role='menuitem'], [role='menu'] li, li").evaluate_all(
                    """nodes => nodes.filter(node => !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length))
                        .map(node => (node.innerText || '').replace(/\\s+/g, ' ').trim())
                        .filter(Boolean).slice(0, 60)"""
                )
            return {
                "url": page.url,
                "entry": args.entry,
                "field": args.field,
                "fields": fields,
                "discovery_schema": discovery_schema.model_dump(mode="json"),
                "choices": choices,
            }
        finally:
            if remote is not None:
                await remote.close()
            await provider.close_session(session.session_id)


def main() -> None:
    print(json.dumps(asyncio.run(inspect(parse_args())), indent=2))


if __name__ == "__main__":
    main()
