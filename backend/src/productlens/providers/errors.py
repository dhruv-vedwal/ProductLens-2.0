from __future__ import annotations


class ProviderError(RuntimeError):
    def __init__(self, provider: str, status_code: int | None, message: str):
        self.provider = provider
        self.status_code = status_code
        self.retryable = status_code is None or status_code == 429 or status_code >= 500
        self.failure_code = (
            "PROVIDER_CREDIT_REQUIRED" if status_code == 402
            else "PROVIDER_RATE_LIMITED" if status_code == 429
            else "PROVIDER_UNAVAILABLE" if status_code is not None and status_code >= 500
            else "PROVIDER_FAILURE"
        )
        super().__init__(f"{provider} provider failure ({status_code or 'network'}): {message}")
