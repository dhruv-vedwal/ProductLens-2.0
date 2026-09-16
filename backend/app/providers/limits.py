"""Process-local provider backpressure controls.

Durable database leases coordinate workers across processes; these bounded
semaphores prevent one worker from flooding a provider with simultaneous
requests. Limits are deployment configuration, never product logic.
"""

from __future__ import annotations

import os


def provider_limit(provider: str, default: int = 2) -> int:
    key = (
        "PRODUCTLENS_"
        + "_".join(part for part in provider.upper().split() if part)
        + "_CONCURRENCY"
    )
    try:
        value = int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(1, min(100, value))
