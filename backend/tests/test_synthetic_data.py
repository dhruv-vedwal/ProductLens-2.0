import pytest

from productlens.contracts.models import OperationKind, SemanticOperation, Target
from productlens.planning.synthetic import SyntheticDataError, hydrate_operations, value_for


def operation(name: str, kind: OperationKind = OperationKind.FILL_TEXT) -> SemanticOperation:
    return SemanticOperation(kind=kind, intent=f"Fill {name}", target=Target(name=name))


def test_semantic_synthetic_data_is_deterministic_and_typed():
    values, dataset = hydrate_operations(
        [
            operation("Work email", OperationKind.FILL_EMAIL),
            operation("Mobile phone", OperationKind.FILL_PHONE),
            operation("Company name"),
        ],
        product_key="https://crm.example",
    )
    assert values[0].value.endswith(".test")
    assert str(values[1].value).startswith("+91")
    assert dataset["Company name"]


def test_synthetic_data_never_generates_authentication_values():
    with pytest.raises(SyntheticDataError):
        value_for(operation("Password"), product_key="https://app.example")
