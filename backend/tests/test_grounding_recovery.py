import pytest

from productlens.contracts.models import Target
from productlens.execution.playwright_adapter import GroundingError, PlaywrightAdapter


class Locator:
    def __init__(self, count: int):
        self._count = count

    async def count(self) -> int:
        return self._count


class Page:
    def __init__(self, counts: dict[str, int]):
        self.counts = counts

    def get_by_test_id(self, value: str) -> Locator:
        return Locator(self.counts.get(f"test:{value}", 0))

    def get_by_role(self, role: str, *, name: str, exact: bool) -> Locator:
        return Locator(self.counts.get(f"role:{role}:{name}", 0))

    def get_by_label(self, value: str, *, exact: bool) -> Locator:
        return Locator(self.counts.get(f"label:{value}", 0))

    def get_by_text(self, value: str, *, exact: bool = False) -> Locator:
        return Locator(self.counts.get(f"text:{value}", 0))

    def locator(self, value: str) -> Locator:
        return Locator(self.counts.get(f"selector:{value}", 0))


@pytest.mark.asyncio
async def test_grounding_recovers_from_stale_test_id_using_semantic_role():
    adapter = PlaywrightAdapter(Page({"test:stale": 0, "role:button:Create lead": 1}))
    locator, strategy = await adapter.grounded_locator(
        Target(name="Create lead", test_id="stale", role="button")
    )
    assert strategy == "role"
    assert await locator.count() == 1


@pytest.mark.asyncio
async def test_grounding_rejects_ambiguous_or_missing_evidence():
    adapter = PlaywrightAdapter(Page({"role:button:Create lead": 2, "selector:#lead": 0}))
    with pytest.raises(GroundingError, match="role:2-matches"):
        await adapter.grounded_locator(Target(name="Create lead", role="button", selector="#lead"))


@pytest.mark.asyncio
async def test_grounding_prefers_observed_control_selector_over_adjacent_label_text():
    adapter = PlaywrightAdapter(
        Page({"text:Phone": 1, 'selector:[name="phone"]': 1})
    )
    _locator, strategy = await adapter.grounded_locator(
        Target(name="Phone", text="Phone", selector='[name="phone"]')
    )
    assert strategy == "selector"
