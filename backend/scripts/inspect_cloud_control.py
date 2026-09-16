"""Read-only Browserbase inspection for an observed semantic control.

This is an operational diagnostic, not a product workflow. It avoids clicks,
typing, screenshots, and trace persistence so an actionability failure can be
classified before a production/rehearsal retry is considered.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from playwright.async_api import async_playwright

from app.config.settings import Settings
from app.credentials.service import EnvironmentCredentialService
from app.providers.browserbase import BrowserbaseProvider


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect a visible Browserbase control without interacting"
    )
    parser.add_argument("--url", required=True)
    parser.add_argument("--control", required=True)
    parser.add_argument("--credential-reference")
    return parser.parse_args()


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
            await page.goto(args.url, wait_until="domcontentloaded")
            authenticated = await credentials.authenticate_if_required(
                page, args.credential_reference
            )
            if authenticated and page.url.rstrip("/") != args.url.rstrip("/"):
                await page.goto(args.url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1_500)
            locator = page.get_by_role("button", name=args.control, exact=True)
            items = []
            for index in range(await locator.count()):
                target = locator.nth(index)
                box = await target.bounding_box()
                details = await target.evaluate(
                    """element => ({
                        tag: element.tagName,
                        text: (element.innerText || '').trim(),
                        disabled: Boolean(element.disabled),
                        ariaDisabled: element.getAttribute('aria-disabled'),
                        pointerEvents: getComputedStyle(element).pointerEvents,
                        zIndex: getComputedStyle(element).zIndex,
                        outer: element.outerHTML.slice(0, 600)
                    })"""
                )
                covering = None
                if box:
                    covering = await page.evaluate(
                        """({x,y}) => {
                            const node = document.elementFromPoint(x, y);
                            return node ? {
                                tag: node.tagName,
                                text: (node.innerText || node.getAttribute('aria-label') || '').trim().slice(0, 180),
                                outer: node.outerHTML.slice(0, 600)
                            } : null;
                        }""",
                        {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2},
                    )
                items.append(
                    {
                        "index": index,
                        "visible": await target.is_visible(),
                        "enabled": await target.is_enabled(),
                        "box": box,
                        "details": details,
                        "covering_element": covering,
                    }
                )
            return {"url": page.url, "control": args.control, "count": len(items), "items": items}
        finally:
            if remote is not None:
                await remote.close()
            await provider.close_session(session.session_id)


def main() -> None:
    print(json.dumps(asyncio.run(inspect(parse_args())), indent=2))


if __name__ == "__main__":
    main()
