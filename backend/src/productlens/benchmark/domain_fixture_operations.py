"""Deterministic CRM-like operations used only by the benchmark fixture.

These helpers intentionally model the acceptance fixture's lead/booking flow.
They are not product-planning capabilities and must never be imported by the
generic discovery, planning, or URL-generation pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from productlens.contracts.models import OperationKind, Postcondition, SemanticOperation, Target


@dataclass(frozen=True)
class DomainTargetMap:
    """Fixture controls, overridable by a benchmark test's observed targets."""

    open_lead: Target = field(default_factory=lambda: Target(name="New lead", test_id="new-lead-btn"))
    lead_modal: Target = field(default_factory=lambda: Target(name="Lead form", test_id="lead-modal"))
    lead_name: Target = field(default_factory=lambda: Target(name="Lead name", test_id="lead-name"))
    lead_company: Target = field(default_factory=lambda: Target(name="Lead company", test_id="lead-company"))
    lead_source: Target = field(default_factory=lambda: Target(name="Lead source", test_id="lead-source"))
    save_lead: Target = field(default_factory=lambda: Target(name="Create lead", test_id="lead-save"))
    open_booking: Target | None = None
    booking_modal: Target = field(default_factory=lambda: Target(name="Booking form", test_id="booking-modal"))
    service: Target | None = None
    booking_date: Target = field(default_factory=lambda: Target(name="Booking date", test_id="booking-date"))
    booking_time: Target = field(default_factory=lambda: Target(name="Booking time", test_id="booking-time"))
    save_booking: Target = field(default_factory=lambda: Target(name="Create booking", test_id="booking-save"))


def _targets(targets: DomainTargetMap | None) -> DomainTargetMap:
    return targets or DomainTargetMap(
        open_booking=Target(name="Create booking", test_id="create-booking-0"),
        service=Target(name="Service", selector='input[name="service"]'),
    )


def create_lead(name: str, company: str, source: str, targets: DomainTargetMap | None = None) -> list[SemanticOperation]:
    t = _targets(targets)
    return [
        SemanticOperation(kind=OperationKind.OPEN_MODAL, intent="Open Create Lead", target=t.open_lead,
            postconditions=[Postcondition(kind="visible", expected=True, target=t.lead_modal)]),
        SemanticOperation(kind=OperationKind.FILL_TEXT, intent="Enter lead name", target=t.lead_name, value=name,
            postconditions=[Postcondition(kind="value", expected=name, target=t.lead_name)]),
        SemanticOperation(kind=OperationKind.FILL_TEXT, intent="Enter company", target=t.lead_company, value=company,
            postconditions=[Postcondition(kind="value", expected=company, target=t.lead_company)]),
        SemanticOperation(kind=OperationKind.SELECT_OPTION, intent="Select source", target=t.lead_source, value=source),
        SemanticOperation(kind=OperationKind.SUBMIT, intent="Create lead", target=t.save_lead,
            postconditions=[Postcondition(kind="test_state", expected="window.__testState.leads.length > 0")]),
    ]


def create_booking(lead_index: int, service: str, date: str, time: str, targets: DomainTargetMap | None = None) -> list[SemanticOperation]:
    t = _targets(targets)
    open_booking = t.open_booking or Target(name="Create booking", test_id=f"create-booking-{lead_index}")
    service_target = t.service or Target(name="Service", selector='input[name="service"]')
    return [
        SemanticOperation(kind=OperationKind.OPEN_MODAL, intent="Open booking for created lead", target=open_booking,
            postconditions=[Postcondition(kind="visible", expected=True, target=t.booking_modal)]),
        SemanticOperation(kind=OperationKind.CHOOSE_RADIO, intent="Select service",
            target=Target(name=service_target.name, selector=(service_target.selector or "") + f'[value="{service}"]')),
        SemanticOperation(kind=OperationKind.SELECT_DATE, intent="Select booking date", target=t.booking_date, value=date),
        SemanticOperation(kind=OperationKind.SELECT_OPTION, intent="Select booking time", target=t.booking_time, value=time),
        SemanticOperation(kind=OperationKind.SUBMIT, intent="Create booking", target=t.save_booking,
            postconditions=[Postcondition(kind="test_state", expected="window.__testState.bookings.length > 0 && window.__testState.leads[0].hasBooking")]),
    ]
