import pytest

from app.contracts.models import (
    FormField,
    ObservedElement,
    OperationKind,
    SemanticOperation,
    Target,
)
from app.execution.playwright_adapter import PlaywrightAdapter
from app.planning.capabilities import _operation_for
from app.planning.forms import infer_form_schema


def test_accessible_combobox_is_a_typed_form_field_and_requires_an_observed_choice():
    schema = infer_form_schema(
        [ObservedElement(tag="input", role="combobox", name="Branch", selector="#branch")],
        "https://example.test/form",
    )
    assert schema.fields[0].control_type == "combobox"
    operation = _operation_for(
        FormField(name="Branch", selector="#branch", control_type="combobox", options=["North"]),
        "https://example.test/form",
    )
    assert operation.kind is OperationKind.SELECT_OPTION
    assert operation.value == "North"


class _Option:
    def __init__(self) -> None:
        self.clicked = False

    async def is_visible(self):
        return True

    async def click(self):
        self.clicked = True


class _Options:
    def __init__(self, option: _Option) -> None:
        self.option = option

    async def count(self):
        return 1

    def nth(self, _index: int):
        return self.option


class _Combobox:
    def __init__(self) -> None:
        self.clicked = False

    async def select_option(self, _value: str):
        raise AttributeError("custom combobox is not a native select")

    async def click(self):
        self.clicked = True


class _Page:
    def __init__(self) -> None:
        self.control = _Combobox()
        self.option = _Option()

    def get_by_test_id(self, _value: str):
        return self.control

    def get_by_role(self, role: str, **_kwargs):
        assert role == "option"
        return _Options(self.option)


@pytest.mark.asyncio
async def test_custom_combobox_uses_the_observed_accessible_option_when_native_selection_is_unavailable():
    page = _Page()
    operation = SemanticOperation(
        kind=OperationKind.SELECT_OPTION,
        intent="Choose branch",
        target=Target(name="Branch", test_id="branch"),
        value="North",
    )

    await PlaywrightAdapter(page).execute(operation)

    assert page.control.clicked is True
    assert page.option.clicked is True
