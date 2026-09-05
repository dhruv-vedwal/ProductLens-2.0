import asyncio
from datetime import UTC, datetime

import pytest

from productlens.contracts.models import (
    DemoPlan,
    DemoTrace,
    OperationKind,
    SemanticOperation,
    Viewport,
    WorkflowStep,
)
from productlens.execution.engine import ExecutionEngine
from productlens.execution.playwright_adapter import GroundingError


class Adapter:
    page = None

    async def snapshot(self, target):
        return {"url": "https://example.test"}

    async def target_rect(self, target):
        return None

    async def view_state(self):
        return Viewport(width=1440, height=900), {"x": 0.0, "y": 0.0}

    async def execute(self, operation):
        return None


@pytest.mark.asyncio
async def test_engine_executes_a_validated_plan():
    plan = DemoPlan(
        objective="Open product",
        narrative_goal="Open product",
        audience="prospect",
        target_duration_seconds=10,
        selected_workflow="home",
        workflow_steps=[
            WorkflowStep(
                id="one",
                intent="Open home",
                operation=SemanticOperation(
                    kind=OperationKind.NAVIGATE, intent="Open home", value="https://example.test"
                ),
            )
        ],
        expected_outcomes=["Page opened"],
        viewport_strategy="default",
        stop_conditions=["done"],
    )
    trace = DemoTrace(run_id="run", objective="Open product", started_at=datetime.now(UTC))
    result = await ExecutionEngine(Adapter(), trace).run_plan(plan)
    assert result.outcome_verified and len(result.events) == 1
    assert result.events[0].viewport.width == 1440


class ReactivePage:
    url = "https://example.test"

    async def wait_for_timeout(self, _milliseconds: int):
        return None


class HoldingPage(ReactivePage):
    async def wait_for_timeout(self, milliseconds: int):
        await asyncio.sleep(milliseconds / 1000)


class RegroundingAdapter(Adapter):
    def __init__(self):
        self.page = ReactivePage()
        self.snapshot_calls = 0

    async def snapshot(self, target):
        self.snapshot_calls += 1
        if self.snapshot_calls == 1:
            raise GroundingError("role:0-matches")
        return {"url": self.page.url, "grounding_strategy": "role"}


@pytest.mark.asyncio
async def test_engine_records_one_bounded_semantic_reground_before_dispatch():
    trace = DemoTrace(run_id="run", objective="Open product", started_at=datetime.now(UTC))
    event = await ExecutionEngine(RegroundingAdapter(), trace).run(
        SemanticOperation(
            kind=OperationKind.CLICK,
            intent="Open the product overview",
            target={"name": "Overview", "role": "button"},
        )
    )
    assert event.success
    assert event.recovery == [
        {"strategy": "semantic_reground", "reason": "role:0-matches", "attempt": 1}
    ]


@pytest.mark.asyncio
async def test_engine_timestamps_visible_result_before_editorial_reading_hold():
    adapter = RegroundingAdapter()
    adapter.page = HoldingPage()
    trace = DemoTrace(run_id="hold", objective="Open product", started_at=datetime.now(UTC))
    event = await ExecutionEngine(adapter, trace, beat_hold_ms=50).run(
        SemanticOperation(kind=OperationKind.CLICK, intent="Open overview", target={"name": "Overview"})
    )
    finished_at = datetime.now(UTC)
    # The event means the state was verified, not that its required reading
    # dwell has elapsed. This is the anchor used by captions and audio later.
    assert (finished_at - event.occurred_at).total_seconds() >= 0.035
