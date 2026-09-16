import pytest

from app.browser.theme import discover_theme_control


class _Control:
    def __init__(self, metadata: str, visible: bool = True):
        self.metadata = metadata
        self.visible = visible
        self.clicked = False

    async def is_visible(self):
        return self.visible

    async def evaluate(self, _script):
        return self.metadata

    async def click(self):
        self.clicked = True


class _Controls:
    def __init__(self, controls):
        self.controls = controls

    async def count(self):
        return len(self.controls)

    def nth(self, index):
        return self.controls[index]


class _Page:
    def __init__(self, controls):
        self.controls = controls

    def locator(self, _selector):
        return _Controls(self.controls)


@pytest.mark.asyncio
async def test_theme_control_is_discovered_from_visible_metadata_not_fixed_label():
    unrelated = _Control("Open menu")
    theme = _Control("Switch to light appearance")
    found = await discover_theme_control(_Page([unrelated, theme]))
    assert found is theme


@pytest.mark.asyncio
async def test_theme_control_ignores_hidden_candidates():
    hidden = _Control("Theme", visible=False)
    assert await discover_theme_control(_Page([hidden])) is None
