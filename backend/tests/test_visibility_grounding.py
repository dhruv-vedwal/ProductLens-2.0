import pytest

from app.contracts.models import Target
from app.execution.playwright_adapter import PlaywrightAdapter


class Locator:
    def __init__(self, count: int):
        self._count = count
        self.first = self

    async def count(self) -> int:
        return self._count


class Page:
    def get_by_test_id(self, _: str) -> Locator:
        return Locator(0)

    def get_by_role(self, *_: object, **__: object) -> Locator:
        return Locator(3)


class FormattedNumberPage:
    def get_by_text(self, value, **_kwargs: object) -> Locator:
        return Locator(2 if "\\D*" in getattr(value, "pattern", "") else 0)


class VisibilityLocator:
    def __init__(self, visibility):
        self._visibility = list(visibility)
        self.first = VisibilityItem(self._visibility[0]) if self._visibility else self

    async def count(self):
        return len(self._visibility)

    def nth(self, index):
        return VisibilityItem(self._visibility[index])

    async def is_visible(self):
        return bool(self._visibility and self._visibility[0])


class VisibilityItem:
    def __init__(self, visible):
        self.visible = visible

    async def is_visible(self):
        return self.visible


class HiddenFirstPage:
    def get_by_test_id(self, _: str):
        return VisibilityLocator([])

    def get_by_text(self, *_args, **_kwargs):
        return VisibilityLocator([False, True])


@pytest.mark.asyncio
async def test_visibility_evidence_can_use_repeated_responsive_markup():
    locator, strategy = await PlaywrightAdapter(Page()).visible_locator(
        Target(name="Today", role="link")
    )
    assert strategy == "role"
    assert await locator.count() == 3


@pytest.mark.asyncio
async def test_actions_remain_strictly_unique():
    with pytest.raises(Exception, match="3-matches"):
        await PlaywrightAdapter(Page()).grounded_locator(Target(name="Today", role="link"))


@pytest.mark.asyncio
async def test_visible_outcome_can_match_a_formatted_generated_phone_value():
    locator, strategy = await PlaywrightAdapter(FormattedNumberPage()).visible_locator(
        Target(name="verified created record", text="9834811227")
    )
    assert strategy == "normalized_number_text"
    assert await locator.count() == 2


@pytest.mark.asyncio
async def test_visible_outcome_skips_hidden_duplicate_dom_witness():
    locator, strategy = await PlaywrightAdapter(HiddenFirstPage()).visible_locator(
        Target(name="language", text="en")
    )
    assert strategy == "text:visible"
    assert await locator.is_visible()


@pytest.mark.asyncio
async def test_target_geometry_falls_back_to_the_grounded_dom_client_box(monkeypatch):
    class GeometryLocator:
        async def bounding_box(self):
            return None

        async def evaluate(self, _script):
            return {"x": 240.5, "y": 120.0, "width": 320.0, "height": 48.0}

    adapter = PlaywrightAdapter(object())

    async def grounded(_target):
        return GeometryLocator(), "label"

    monkeypatch.setattr(adapter, "grounded_locator", grounded)
    rect = await adapter.target_rect(Target(name="Email"))
    assert rect is not None
    assert rect.model_dump() == {"x": 240.5, "y": 120.0, "width": 320.0, "height": 48.0}
