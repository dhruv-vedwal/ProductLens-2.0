import pytest

from productlens.benchmark.domain_fixture_operations import DomainTargetMap, create_lead
from productlens.contracts.models import OperationKind, SemanticOperation, Target
from productlens.execution.playwright_adapter import PlaywrightAdapter


class Locator:
    async def input_value(self):
        return "Sarah"

    async def wait_for(self, **kwargs):
        return "visible"


class Page:
    def get_by_test_id(self, value):
        return Locator()


@pytest.mark.asyncio
async def test_read_and_verify_state_operations_are_supported():
    adapter = PlaywrightAdapter(Page())
    target = Target(name="name", test_id="name")
    assert (
        await adapter.execute(
            SemanticOperation(kind=OperationKind.READ_VALUE, intent="Read", target=target)
        )
        == "Sarah"
    )
    assert (
        await adapter.execute(
            SemanticOperation(kind=OperationKind.VERIFY_STATE, intent="Verify", target=target)
        )
        == "visible"
    )


def test_domain_operations_use_observed_targets_instead_of_site_constants():
    operations = create_lead(
        "A",
        "B",
        "C",
        DomainTargetMap(
            open_lead=Target(name="Add contact", selector="button.add-contact"),
            lead_modal=Target(name="Contact form", selector="[role=dialog]"),
            lead_name=Target(name="Full name", selector="#full-name"),
            lead_company=Target(name="Organization", selector="#organization"),
            lead_source=Target(name="Origin", selector="#origin"),
            save_lead=Target(name="Save contact", selector="button.save"),
        ),
    )
    assert operations[0].target.selector == "button.add-contact"
    assert operations[1].target.selector == "#full-name"
    assert operations[-1].target.selector == "button.save"
