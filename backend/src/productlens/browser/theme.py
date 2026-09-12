"""Evidence-based discovery of a site's own theme control.

Theme is a presentation preference, not a workflow route.  The helper keeps
the renderer from depending on one application's button label while still
requiring a visible, semantic control before clicking anything.
"""

from __future__ import annotations

import re
from typing import Any

from playwright.async_api import Error as PlaywrightError


_THEME_HINT = re.compile(r"\b(?:theme|dark|light|appearance|contrast)\b", re.IGNORECASE)


async def discover_theme_control(page: Any) -> Any | None:
    """Return a visible semantic theme control discovered from its metadata."""
    controls = page.locator("button,[role='button']")
    try:
        count = await controls.count()
    except PlaywrightError:
        return None
    for index in range(count):
        control = controls.nth(index)
        try:
            if not await control.is_visible():
                continue
            metadata = await control.evaluate(
                """element => [
                    element.getAttribute('aria-label'),
                    element.getAttribute('title'),
                    element.getAttribute('data-testid'),
                    element.textContent,
                ].filter(Boolean).join(' ')"""
            )
        except PlaywrightError:
            continue
        if _THEME_HINT.search(str(metadata or "")):
            return control
    return None
