import pytest

from app.contracts.models import OperationKind, SemanticOperation, Target
from app.execution.playwright_adapter import (
    PlaywrightAdapter,
    _canonical_date,
    _visible_date_keystrokes,
)


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
        ("press_sequentially", ("Maya", 70)),
        ("press_sequentially", (" Shah", 70)),
    ]


def test_date_keystrokes_keep_iso_and_locale_visible_forms():
    assert _canonical_date("2026-09-15") == "2026-09-15"
    assert _canonical_date("09/15/2026") == "2026-09-15"
    assert "09152026" in _visible_date_keystrokes("2026-09-15")
