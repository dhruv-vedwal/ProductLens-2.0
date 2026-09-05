from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from productlens.providers.browserbase import BrowserbaseProvider


@dataclass(frozen=True)
class BrowserSession:
    provider: Literal["local_playwright", "browserbase"]
    session_id: str | None


class BrowserSessionService:
    def __init__(self, browserbase: BrowserbaseProvider | None = None):
        self.browserbase = browserbase

    async def create(self, *, cloud: bool) -> BrowserSession:
        if cloud:
            if not self.browserbase:
                raise RuntimeError("Browserbase is not configured")
            return BrowserSession("browserbase", await self.browserbase.create_session())
        return BrowserSession("local_playwright", None)
