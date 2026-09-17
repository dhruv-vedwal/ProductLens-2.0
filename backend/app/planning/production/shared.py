"""Shared URL helpers and planning validation errors."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from app.contracts.models import (
    OperationKind,
)
from app.urls import canonical_product_url


def _canonical_url(value: str) -> str:
    """Normalize equivalent browser URL spellings for planning transitions."""
    return canonical_product_url(value)


def _route_key(value: str) -> tuple[str, str, str]:
    """Return the transport-independent route identity used for navigation.

    Query parameters commonly encode filters, pagination, or SPA state.  They
    must be preserved in evidence and postconditions, but they must not make a
    route look unobserved when the same visible control exposed its canonical
    path without those parameters.
    """
    parsed = urlsplit(_canonical_url(value))
    return parsed.scheme, parsed.netloc, parsed.path


def _semantic_words(value: str) -> set[str]:
    """Return meaningful words for conservative semantic target matching."""
    return {token for token in re.findall(r"[a-z0-9]+", (value or "").casefold()) if len(token) > 2}


class PlanningValidationError(ValueError):
    pass


SIDE_EFFECTING = {OperationKind.SUBMIT, OperationKind.CHECK, OperationKind.UNCHECK}
NO_POSTCONDITION_REQUIRED = {
    OperationKind.SCROLL_TO,
    OperationKind.READ_VALUE,
    # Keyboard text/commit gestures are verified by the following scene
    # witness (DOM/scene-graph or explicit state transition), not by a
    # brittle key-label visibility assertion.
    OperationKind.KEY_PRESS,
}

__all__ = [
    "NO_POSTCONDITION_REQUIRED",
    "SIDE_EFFECTING",
    "PlanningValidationError",
    "_canonical_url",
    "_route_key",
    "_semantic_words",
]
