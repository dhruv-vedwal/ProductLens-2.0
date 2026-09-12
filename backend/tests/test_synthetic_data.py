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
    assert str(values[1].value).isdigit() and len(str(values[1].value)) == 10
    assert dataset["Company name"]


def test_synthetic_data_never_generates_authentication_values():
    with pytest.raises(SyntheticDataError):
        value_for(operation("Password"), product_key="https://app.example")


def test_synthetic_values_do_not_reuse_observed_customer_data_even_when_formatted_differently():
    phone = operation("Mobile phone", OperationKind.FILL_PHONE)
    generated = value_for(
        phone,
        product_key="https://crm.example",
        forbidden_values={"98 1234 5678", "Riya Kapoor", "riya.kapoor@harboranalytics.test"},
    )
    assert generated != "9812345678"
    assert generated.isdigit()


def test_hydration_accepts_observed_value_exclusions():
    values, dataset = hydrate_operations(
        [operation("Customer name")],
        product_key="https://crm.example",
        forbidden_values={"Aarav Mehta", "Maya Shah", "Riya Kapoor"},
    )
    assert values[0].value not in {"Aarav Mehta", "Maya Shah", "Riya Kapoor"}
    assert dataset["Customer name"] == values[0].value


def test_short_inventory_tokens_do_not_exhaust_phone_generation():
    generated = value_for(
        operation("Phone", OperationKind.FILL_PHONE),
        product_key="https://crm.example",
        forbidden_values={"0", "1", "2", "PATIENT", "916350063871"},
    )
    assert generated.isdigit() and len(generated) == 10
