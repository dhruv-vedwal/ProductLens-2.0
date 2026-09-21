import pytest

from app.contracts.models import OperationKind, SemanticOperation, Target
from app.execution.playwright_adapter import (
    PlaywrightAdapter,
    _canonical_date,
    _canonical_date_from_visible_label,
    _canonical_time,
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


def test_calendar_labels_are_normalized_without_widget_specific_selectors():
    assert _canonical_date_from_visible_label("September 30, 2026") == "2026-09-30"
    assert _canonical_date_from_visible_label("Wed 30 Sep 2026") == "2026-09-30"
    assert _canonical_date_from_visible_label("2026-09-30") == "2026-09-30"


def test_native_time_values_are_normalized_for_segmented_control_verification():
    assert _canonical_time("10:30") == "10:30"
    assert _canonical_time("10:30 AM") == "10:30"
    assert _canonical_time("10:30 PM") == "22:30"
    assert _canonical_time("1030") == "10:30"
