"""Deterministic, non-sensitive form values derived from observed field semantics."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from productlens.contracts.models import OperationKind, SemanticOperation


class SyntheticDataError(ValueError):
    pass


_IDENTITIES = (
    ("Aarav", "Mehta", "Northstar Systems"),
    ("Maya", "Shah", "Cedar Labs"),
    ("Riya", "Kapoor", "Harbor Analytics"),
)


def _comparable(value: object) -> str:
    """Normalize display formatting before checking a generated value.

    A phone can be visibly formatted with spaces, a country prefix, or
    punctuation. Comparing only raw strings would accidentally reuse an
    observed customer value in a supposedly isolated demo record.
    """
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _candidate_value(field: str, *, seed: int, attempt: int) -> str:
    first, last, company = _IDENTITIES[(seed + attempt) % len(_IDENTITIES)]
    fallback_identity = attempt >= len(_IDENTITIES)
    suffix = "" if not fallback_identity else f" {attempt // len(_IDENTITIES) + 1}"
    if "email" in field:
        local_suffix = "" if attempt < len(_IDENTITIES) else f"{attempt // len(_IDENTITIES) + 1}"
        if fallback_identity:
            return f"demo.contact{local_suffix}@productlens.test"
        return f"{first.lower()}.{last.lower()}@{company.lower().replace(' ', '')}.test"
    if any(token in field for token in ("phone", "mobile", "telephone")):
        # Reserve a deterministic non-production-looking sequence while
        # changing the suffix on a collision. Country prefixes are a product
        # policy and intentionally remain outside the generic generator.
        return f"98{(seed + attempt * 7_919) % 100_000_000:08d}"
    if "company" in field or "organization" in field:
        return f"Demo Workspace{suffix}" if fallback_identity else company
    if "first name" in field:
        return f"Demo{suffix}" if fallback_identity else first
    if "last name" in field or "surname" in field:
        return f"Contact{suffix}" if fallback_identity else last
    if "name" in field:
        return f"Demo Contact{suffix}" if fallback_identity else f"{first} {last}"
    if "date" in field or re.search(r"\b(?:dd|mm|yyyy)[-/]", field):
        # Keep generated dates close to today.  Product UIs commonly expose a
        # bounded booking/calendar window; a year-ahead synthetic date is not
        # safer, it is simply rejected as invalid.  The value remains
        # deterministic per field/attempt and never reuses observed data.
        value = datetime.now(UTC).date() + timedelta(days=7 + (seed + attempt) % 8)
        # Respect the format exposed by the observed control. Design-system
        # text pickers commonly advertise dd-mm-yyyy while native date inputs
        # require ISO; the field label/placeholder is the only source used.
        if re.search(r"dd[-/]mm[-/]yyyy", field):
            return value.strftime("%d-%m-%Y")
        return value.isoformat()
    if "time" in field:
        return "10:30"
    if "number" in field or "quantity" in field or "size" in field:
        return str(10 + (seed + attempt) % 90)
    if "website" in field or "url" in field:
        return f"https://northstar-demo-{attempt + 1}.example"
    return f"Demo Workspace{suffix}" if fallback_identity else f"{company} demo"


def value_for(
    operation: SemanticOperation, *, product_key: str, forbidden_values: set[str] | None = None
) -> str:
    """Generate a plausible demo value; never generate authentication secrets."""
    if operation.target is None:
        raise SyntheticDataError("A target is required for synthetic data")
    field = operation.target.name.lower()
    if any(token in field for token in ("password", "passcode", "otp", "verification code")):
        raise SyntheticDataError("Authentication values must come from a secret reference")
    seed = int(sha256(f"{product_key}:{operation.target.name}".encode()).hexdigest()[:8], 16)
    # Tiny tokens (table indexes, boolean fragments, punctuation) are not
    # meaningful observed values. Treating them as substring matches makes
    # every generated phone/name collide with a page containing ``1`` or ``0``
    # and can exhaust the bounded generator on otherwise valid forms. Keep
    # only values long enough to identify a real field value, while still
    # comparing formatted variants of names, emails, and phone numbers.
    forbidden = {
        normalized
        for value in (forbidden_values or set())
        if (normalized := _comparable(value)) and len(normalized) >= 4
    }
    # Busy demo accounts can expose hundreds of existing values; keep the
    # generator bounded but large enough to find an isolated candidate without
    # falling back to a real customer value.
    for attempt in range(128):
        candidate = _candidate_value(field, seed=seed, attempt=attempt)
        normalized = _comparable(candidate)
        calendar_field = "date" in field or "time" in field or bool(
            re.search(r"\b(?:dd|mm|yyyy)[-/]", field)
        )
        if normalized and (
            calendar_field
            or not any(
                normalized == value
                or (
                    len(value) >= 8
                    and (normalized in value or value in normalized)
                )
                for value in forbidden
            )
        ):
            return candidate
    raise SyntheticDataError(
        f"Could not generate an isolated value for {operation.target.name!r} distinct from observed product data"
    )


def hydrate_operations(
    operations: list[SemanticOperation],
    *,
    product_key: str,
    forbidden_values: set[str] | None = None,
) -> tuple[list[SemanticOperation], dict[str, str]]:
    """Fill missing typed values and retain a non-secret dataset for auditability."""
    fill_kinds = {
        OperationKind.FILL_TEXT,
        OperationKind.FILL_EMAIL,
        OperationKind.FILL_PHONE,
        OperationKind.SELECT_DATE,
    }
    dataset: dict[str, str] = {}
    hydrated: list[SemanticOperation] = []
    for operation in operations:
        if operation.kind in fill_kinds and operation.value in (None, ""):
            value = value_for(operation, product_key=product_key, forbidden_values=forbidden_values)
            dataset[operation.target.name if operation.target else operation.id] = value
            hydrated.append(
                operation.model_copy(
                    update={
                        "value": value,
                        # The verification contract owns the same generated value as
                        # the typed operation. Leaving ``expected=None`` made a form
                        # look compiled while guaranteeing a false postcondition at
                        # production time.
                        "postconditions": [
                            condition.model_copy(update={"expected": value})
                            if condition.kind == "value" and condition.expected in (None, "")
                            else condition
                            for condition in operation.postconditions
                        ],
                    }
                )
            )
        else:
            hydrated.append(operation)
    return hydrated, dataset
