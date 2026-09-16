import pytest

from app.credentials.service import CredentialError, EnvironmentCredentialService
from app.observability.logging import redact_prompt_text


def test_provider_prompt_redaction_keeps_objective_shape_without_secrets():
    prompt = (
        "Show the account workflow for admin@example.test; password=super-secret-value "
        "and token sk-1234567890abcdef are private."
    )
    redacted = redact_prompt_text(prompt)
    assert "admin@example.test" not in redacted
    assert "super-secret-value" not in redacted
    assert "sk-1234567890abcdef" not in redacted
    assert "account workflow" in redacted


def test_credential_service_resolves_only_opaque_reference(monkeypatch):
    monkeypatch.setenv("PRODUCTLENS_CREDENTIAL_DEMO_USERNAME", "demo@example.test")
    monkeypatch.setenv("PRODUCTLENS_CREDENTIAL_DEMO_PASSWORD", "not-in-artifacts")
    credentials = EnvironmentCredentialService().resolve("secret://productlens/demo")
    assert credentials.username == "demo@example.test"
    assert credentials.password == "not-in-artifacts"
    with pytest.raises(CredentialError):
        EnvironmentCredentialService().resolve("raw-password")


@pytest.mark.asyncio
async def test_authentication_fails_cleanly_when_login_needs_a_reference():
    class PasswordLocator:
        async def count(self):
            return 1

    class Page:
        def locator(self, _):
            return PasswordLocator()

    with pytest.raises(CredentialError, match="AUTH_REQUIRED"):
        await EnvironmentCredentialService().authenticate_if_required(Page(), None)


@pytest.mark.asyncio
async def test_authentication_clears_existing_values_then_types_sequentially(monkeypatch):
    monkeypatch.setenv("PRODUCTLENS_CREDENTIAL_DEMO_USERNAME", "demo@example.test")
    monkeypatch.setenv("PRODUCTLENS_CREDENTIAL_DEMO_PASSWORD", "safe-test-password")

    class Locator:
        def __init__(self):
            self.actions = []

        async def count(self):
            return 1

        async def click(self):
            self.actions.append("click")

        async def press(self, value):
            self.actions.append(("press", value))

        async def press_sequentially(self, value, *, delay):
            self.actions.append(("type", value, delay))

        async def wait_for(self, **_kwargs):
            return None

    username, password, submit = Locator(), Locator(), Locator()

    class Page:
        def locator(self, selector):
            if "password" in selector and "submit" not in selector:
                return password
            if "submit" in selector:
                return submit
            return username

        async def wait_for_timeout(self, _milliseconds):
            return None

        async def wait_for_function(self, *_args, **_kwargs):
            return None

    assert await EnvironmentCredentialService().authenticate_if_required(
        Page(), "secret://productlens/demo"
    )
    assert username.actions[:3] == ["click", ("press", "ControlOrMeta+A"), ("press", "Backspace")]
    assert password.actions[:3] == ["click", ("press", "ControlOrMeta+A"), ("press", "Backspace")]
    assert username.actions[-1] == ("type", "demo@example.test", 70)
    assert password.actions[-1] == ("type", "safe-test-password", 70)
