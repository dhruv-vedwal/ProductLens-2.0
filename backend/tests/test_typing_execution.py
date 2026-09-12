import pytest

from productlens.contracts.models import OperationKind, SemanticOperation, Target
from productlens.execution.playwright_adapter import PlaywrightAdapter


class _TypingLocator:
    def __init__(self) -> None:
        self.actions: list[tuple[str, object]] = []

    async def click(self):
        self.actions.append(("click", None))

    async def press(self, value: str):
        self.actions.append(("press", value))

    async def press_sequentially(self, value: str, *, delay: int):
        self.actions.append(("press_sequentially", (value, delay)))

    async def fill(self, _value: str):
        raise AssertionError("demo value entry must remain visible sequential typing")


class _TypingPage:
    def __init__(self) -> None:
        self.locator = _TypingLocator()

    def get_by_test_id(self, _value: str):
        return self.locator


@pytest.mark.asyncio
async def test_all_text_controls_use_visible_typing_even_when_a_test_id_is_present():
    page = _TypingPage()
    operation = SemanticOperation(
        kind=OperationKind.FILL_TEXT,
        intent="Enter the name",
        target=Target(name="Name", test_id="combobox-input"),
        value="Maya Shah",
    )

    await PlaywrightAdapter(page).execute(operation)

    assert page.locator.actions == [
        ("click", None),
        ("press", "ControlOrMeta+A"),
        ("press", "Backspace"),
        ("press_sequentially", ("Maya Shah", 70)),
    ]
