"""Deterministic, non-sensitive form values derived from observed field semantics."""

from __future__ import annotations

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


def value_for(operation: SemanticOperation, *, product_key: str) -> str:
    """Generate a plausible demo value; never generate authentication secrets."""
    if operation.target is None:
        raise SyntheticDataError("A target is required for synthetic data")
    field = operation.target.name.lower()
    if any(token in field for token in ("password", "passcode", "otp", "verification code")):
        raise SyntheticDataError("Authentication values must come from a secret reference")
    seed = int(sha256(f"{product_key}:{operation.target.name}".encode()).hexdigest()[:8], 16)
    first, last, company = _IDENTITIES[seed % len(_IDENTITIES)]
    if "email" in field:
        return f"{first.lower()}.{last.lower()}@{company.lower().replace(' ', '')}.test"
    if any(token in field for token in ("phone", "mobile", "telephone")):
        return f"+91 98{seed % 10_000_000:07d}"
    if "company" in field or "organization" in field:
        return company
    if "first name" in field:
        return first
    if "last name" in field or "surname" in field:
        return last
    if "name" in field:
        return f"{first} {last}"
    if "date" in field:
        return (datetime.now(UTC).date() + timedelta(days=7 + seed % 14)).isoformat()
    if "time" in field:
        return "10:30"
    if "number" in field or "quantity" in field or "size" in field:
        return str(10 + seed % 90)
    if "website" in field or "url" in field:
        return "https://northstar.example"
    return f"{company} demo"


def hydrate_operations(
    operations: list[SemanticOperation], *, product_key: str
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
            value = value_for(operation, product_key=product_key)
            dataset[operation.target.name if operation.target else operation.id] = value
            hydrated.append(operation.model_copy(update={"value": value}))
        else:
            hydrated.append(operation)
    return hydrated, dataset
