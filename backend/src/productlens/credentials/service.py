from __future__ import annotations

import os
import re
from dataclasses import dataclass

from playwright.async_api import Error as PlaywrightError


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrowserCredentials:
    username: str
    password: str


class EnvironmentCredentialService:
    """Resolve only opaque references; credential values never enter run payloads."""

    _REFERENCE = re.compile(r"^secret://productlens/([a-zA-Z0-9_-]{1,64})$")

    def resolve(self, reference: str) -> BrowserCredentials:
        match = self._REFERENCE.fullmatch(reference)
        if not match:
            raise CredentialError("credentials must use secret://productlens/<name> references")
        name = match.group(1).upper().replace("-", "_")
        username = os.getenv(f"PRODUCTLENS_CREDENTIAL_{name}_USERNAME")
        password = os.getenv(f"PRODUCTLENS_CREDENTIAL_{name}_PASSWORD")
        if not username or not password:
            raise CredentialError("credential reference is not available to the browser worker")
        return BrowserCredentials(username=username, password=password)

    async def authenticate_if_required(self, page, reference: str | None) -> bool:
        password = page.locator('input[type="password"]')
        if await password.count() == 0:
            return False
        if not reference:
            raise CredentialError("AUTH_REQUIRED: a browser credential reference is required")
        credentials = self.resolve(reference)
        username = page.locator('input[type="email"], input[name*="user" i], input[name*="email" i]')
        if await username.count() != 1 or await password.count() != 1:
            raise CredentialError("AUTH_UNSUPPORTED: could not uniquely identify login inputs")
        await username.fill(credentials.username)
        await password.fill(credentials.password)
        submit = page.locator('button[type="submit"], input[type="submit"]')
        if await submit.count() != 1:
            raise CredentialError("AUTH_UNSUPPORTED: could not uniquely identify login submit control")
        await submit.click()
        try:
            await password.wait_for(state="hidden", timeout=8_000)
        except PlaywrightError as error:
            raise CredentialError("AUTH_FAILURE: login did not reach an authenticated state") from error
        return True
