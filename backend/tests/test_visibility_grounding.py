import pytest

from productlens.contracts.models import Target
from productlens.execution.playwright_adapter import PlaywrightAdapter


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
