import pytest

from app.contracts.models import (
    FormField,
    ObservedElement,
    OperationKind,
    SemanticOperation,
    Target,
)
from app.execution.playwright_adapter import GroundingError, PlaywrightAdapter
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


def test_combobox_without_observed_value_preserves_observed_product_order():
    operation = _operation_for(
        FormField(
            name="Source",
            selector="label:Source",
            control_type="combobox",
            options=["Website", "Referral", "Campaign"],
        ),
        "https://example.test/form",
    )
    assert operation.kind is OperationKind.SELECT_OPTION
    # Without an objective-specific value, the planner must not invent a
    # semantic preference from alphabetic order.  Preserve the live option
    # order captured from the product instead.
    assert operation.value == "Website"


class _Option:
    def __init__(self) -> None:
        self.clicked = False

    async def is_visible(self):
        return True

    async def inner_text(self):
        return "North"

    async def get_attribute(self, _name):
        return "North"

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
        self.selected = ""

    async def select_option(self, _value: str):
        raise AttributeError("custom combobox is not a native select")

    async def click(self):
        self.clicked = True
        self.selected = "North"

    async def evaluate(self, script, *_args):
        # The production adapter requires a visible semantic witness after a
        # custom option click.  Model the selected chip/value that a real
        # combobox would render instead of allowing an unverified click.
        if "tagName === 'SELECT'" in script:
            return {"native": False}
        return self.selected


class _Page:
    def __init__(self) -> None:
        self.control = _Combobox()
        self.option = _Option()

    def get_by_test_id(self, _value: str):
        return self.control

    def get_by_role(self, role: str, **_kwargs):
        assert role == "option"
        return _Options(self.option)


class _StaleCombobox(_Combobox):
    async def click(self):
        self.clicked = True
        # The menu opens, but the selected value never appears in the control.
        # This is the production failure mode that must trigger re-observation.
        self.selected = ""


class _StalePage(_Page):
    def __init__(self) -> None:
        super().__init__()
        self.control = _StaleCombobox()


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


@pytest.mark.asyncio
async def test_custom_combobox_rejects_click_without_a_visible_selection_witness():
    page = _StalePage()
    operation = SemanticOperation(
        kind=OperationKind.SELECT_OPTION,
        intent="Choose branch",
        target=Target(name="Branch", test_id="branch"),
        value="North",
    )

    with pytest.raises(GroundingError, match="selected label"):
        await PlaywrightAdapter(page).execute(operation)
