"""Read-only check for whether a rehearsal's generated record is visibly present.

The command is intentionally conservative: it loads the discovered source
page, authenticates through the configured opaque reference, and checks the
current visible text for the deterministic synthetic values. It does not
search, click, type, change filters, or disclose those values in output.
An absent match is inconclusive rather than proof that a submitted record was
not created.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from productlens.config.settings import Settings
from productlens.contracts.models import ActionCapability
from productlens.credentials.service import EnvironmentCredentialService
from productlens.planning.capabilities import compile_rehearsal_operations
from productlens.planning.synthetic import hydrate_operations
from productlens.providers.browserbase import BrowserbaseProvider


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only visible synthetic-record presence check"
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--request-file", required=True)
    parser.add_argument("--artifact-root", default="artifacts")
    return parser.parse_args()


async def inspect(args: argparse.Namespace) -> dict[str, object]:
    request = json.loads(Path(args.request_file).read_text(encoding="utf-8"))
    context_path = (
        Path(args.artifact_root) / "runs" / args.run_id / "discovery" / "product-context.json"
    )
    context = json.loads(context_path.read_text(encoding="utf-8"))
    capability = next(
        ActionCapability.model_validate(item)
        for item in context.get("capabilities", [])
        if item.get("kind") == "form"
    )
    _operations, dataset = hydrate_operations(
        compile_rehearsal_operations(capability), product_key=capability.source_url
    )
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
            browser_context = remote.contexts[0]
            page = (
                browser_context.pages[0]
                if browser_context.pages
                else await browser_context.new_page()
            )
            await page.set_viewport_size({"width": 1440, "height": 900})
            page.set_default_navigation_timeout(90_000)
            await page.goto(capability.source_url, wait_until="domcontentloaded")
            authenticated = await credentials.authenticate_if_required(
                page, request.get("credential_reference")
            )
            if authenticated and page.url.rstrip("/") != capability.source_url.rstrip("/"):
                await page.goto(capability.source_url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=7_000)
            except PlaywrightError:
                await page.wait_for_timeout(700)
            visible = (await page.locator("body").inner_text()).casefold()
            present = []
            for field, value in dataset.items():
                compact = re.sub(r"[^a-z0-9]", "", value.casefold())
                visible_compact = re.sub(r"[^a-z0-9]", "", visible)
                present.append(
                    {
                        "field": field,
                        "visible_match": len(compact) >= 5 and compact in visible_compact,
                    }
                )
            return {
                "run_id": args.run_id,
                "source_url": capability.source_url,
                "checked_fields": present,
                "any_visible_match": any(item["visible_match"] for item in present),
                "result": "visible_match"
                if any(item["visible_match"] for item in present)
                else "inconclusive",
            }
        finally:
            if remote is not None:
                await remote.close()
            await provider.close_session(session.session_id)


def main() -> None:
    print(json.dumps(asyncio.run(inspect(parse_args())), indent=2))


if __name__ == "__main__":
    main()
