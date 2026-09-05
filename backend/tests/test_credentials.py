import pytest

from productlens.credentials.service import CredentialError, EnvironmentCredentialService


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
        async def count(self): return 1

    class Page:
        def locator(self, _): return PasswordLocator()

    with pytest.raises(CredentialError, match="AUTH_REQUIRED"):
        await EnvironmentCredentialService().authenticate_if_required(Page(), None)
