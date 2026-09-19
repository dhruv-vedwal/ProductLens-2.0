"""Entity-specific outcome witnesses.

A changed URL plus a UI placeholder is not proof that a created record
retained the demonstrated identity. Callers must require at least two
independently matched, non-placeholder field values.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# Shared empty-state / unset-control copy — not product field names or flows.
_PLACEHOLDER_VALUES = frozenset(
    {
        "unassigned",
        "default",
        "none",
        "n/a",
        "na",
        "select",
        "select an option",
        "choose",
        "choose an option",
        "-",
        "--",
        "null",
        "not specified",
        "verified created record",
    }
)

_PLACEHOLDER_PHRASE = re.compile(
    r"^(?:"
    r"select(?:\s+an?)?\s+option"
    r"|choose(?:\s+an?)?\s+option"
    r"|not\s+specified"
    r"|n/?a"
    r")$",
    re.IGNORECASE,
)


def is_specific_entity_value(value: object) -> bool:
    cleaned = " ".join(str(value or "").strip().casefold().split())
    if not cleaned or cleaned in _PLACEHOLDER_VALUES:
        return False
    if _PLACEHOLDER_PHRASE.fullmatch(cleaned):
        return False
    # Punctuation-only / dash placeholders.
    if re.fullmatch(r"[\W_]+", cleaned):
        return False
    return True


def specific_entity_fields(fields: Mapping[object, object] | None) -> dict[str, str]:
    if not isinstance(fields, Mapping):
        return {}
    return {
        str(field).strip(): str(value).strip()
        for field, value in fields.items()
        if str(field).strip() and is_specific_entity_value(value)
    }
