from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

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

    @staticmethod
    def _project_environment_value(key: str) -> str | None:
        """Read one secret from the ignored project env file as a fallback.

        Settings already supports this file, but credentials are resolved at
        the browser boundary and must not be copied into global process state.
        Keeping the lookup local also makes workers started from another shell
        behave exactly like the API process.
        """
        source = Path(
            os.getenv(
                "PRODUCTLENS_ENV_FILE",
                Path(__file__).resolve().parents[3] / ".env",
            )
        )
        if not source.is_file():
            return None
        for line in source.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == key and value.strip():
                return value.strip().strip('"').strip("'")
        return None

    def resolve(self, reference: str) -> BrowserCredentials:
        match = self._REFERENCE.fullmatch(reference)
        if not match:
            raise CredentialError("credentials must use secret://productlens/<name> references")
        name = match.group(1).upper().replace("-", "_")
        username_key = f"PRODUCTLENS_CREDENTIAL_{name}_USERNAME"
        password_key = f"PRODUCTLENS_CREDENTIAL_{name}_PASSWORD"
        username = os.getenv(username_key) or self._project_environment_value(username_key)
        password = os.getenv(password_key) or self._project_environment_value(password_key)
        if not username or not password:
            raise CredentialError("credential reference is not available to the browser worker")
        return BrowserCredentials(username=username, password=password)

    async def authenticate_if_required(
        self,
        page,
        reference: str | None,
        *,
        action_observer: Callable[[str, str, str, str], Awaitable[None]] | None = None,
    ) -> bool:
        """Authenticate and optionally report redacted login UI actions."""
        # Public products often keep a sign-in dialog/template in the DOM
        # while the visitor experience is already available.  Only a visible
        # password control is evidence that authentication is required; hidden
        # controls must not turn an otherwise demoable URL into AUTH_REQUIRED.
        password = page.locator('input[type="password"]:visible')
        if await password.count() == 0:
            return False
        if not reference:
            raise CredentialError("AUTH_REQUIRED: a browser credential reference is required")
        credentials = self.resolve(reference)
        username = page.locator(
            'input[type="email"]:visible, input[name*="user" i]:visible, input[name*="email" i]:visible'
        )
        if await username.count() != 1 or await password.count() != 1:
            raise CredentialError("AUTH_UNSUPPORTED: could not uniquely identify login inputs")
        # Authentication is part of a product demo when the user requests it.
        # Use the same deliberate input behaviour as the semantic executor so
        # the source recording contains real typing and cursor timing rather
        # than an unexplained pasted value. Values remain absent from traces.
        await username.click()
        await username.press("ControlOrMeta+A")
        await username.press("Backspace")
        await username.press_sequentially(credentials.username, delay=70)
        if action_observer is not None:
            await action_observer("auth:username", "FillEmail", "Email address", 'input[type="email"]')
        # Keep the filled state on screen long enough for a caption-led
        # recording to communicate the step.  This is deliberately a small,
        # provider-neutral presentation hold; it does not expose the value or
        # make authentication depend on a fixed network delay.
        await page.wait_for_timeout(5_000)
        await password.click()
        await password.press("ControlOrMeta+A")
        await password.press("Backspace")
        await password.press_sequentially(credentials.password, delay=70)
        if action_observer is not None:
            await action_observer("auth:password", "FillText", "Password", 'input[type="password"]')
        await page.wait_for_timeout(5_000)
        submit = page.locator('button[type="submit"]:visible, input[type="submit"]:visible')
        if await submit.count() != 1:
            raise CredentialError("AUTH_UNSUPPORTED: could not uniquely identify login submit control")
        # CAPTCHA/anti-bot widgets commonly enable the submit control only
        # after their token is solved.  Filling the credentials and clicking
        # immediately races that asynchronous state and used to surface as a
        # generic Playwright timeout.  Wait for the observed control to become
        # enabled, then classify an unresolved challenge explicitly.
        try:
            await page.wait_for_function(
                """() => [...document.querySelectorAll('button[type="submit"], input[type="submit"]')].some(el => {
                    const r = el.getBoundingClientRect();
                    return !el.disabled && r.width > 0 && r.height > 0;
                })""",
                timeout=45_000,
            )
        except PlaywrightError as error:
            raise CredentialError(
                "AUTH_CAPTCHA_UNSOLVED: login submit remained disabled after credential fill"
            ) from error
        await submit.click()
        if action_observer is not None:
            await action_observer("auth:submit", "Submit", "Sign in", 'button[type="submit"], input[type="submit"]')
        await page.wait_for_timeout(5_000)
        try:
            # Remote identity providers often complete the redirect after the
            # button click has returned.  A short local-only wait turns a
            # valid Browserbase CAPTCHA/login path into a false failure;
            # retain a bounded, provider-neutral authenticated-state window.
            await password.wait_for(state="hidden", timeout=30_000)
        except PlaywrightError as error:
            # Preserve only structural failure classification. Never inspect
            # alert text here: identity providers can echo an email address or
            # other sensitive context in an authentication error.
            try:
                captcha_count = await page.locator(
                    "iframe[src*='captcha' i],[data-sitekey],[data-captcha]"
                ).count()
            except PlaywrightError:
                captcha_count = 0
            code = (
                "AUTH_CAPTCHA_UNSOLVED_AFTER_SUBMIT"
                if captcha_count
                else "AUTH_LOGIN_STATE_UNCHANGED"
            )
            raise CredentialError(f"{code}: login did not reach an authenticated state") from error
        return True
