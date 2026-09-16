from app.providers.errors import ProviderError


def test_provider_credit_failure_is_explicit_and_non_retryable():
    error = ProviderError("browserbase", 402, "payment required")
    assert error.failure_code == "PROVIDER_CREDIT_REQUIRED"
    assert error.retryable is False


def test_transient_provider_failure_remains_retryable():
    error = ProviderError("browserbase", 503, "temporary outage")
    assert error.failure_code == "PROVIDER_UNAVAILABLE"
    assert error.retryable is True
