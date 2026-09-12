import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from productlens.artifacts.store import RunArtifacts
from productlens.contracts.models import (
    DemoPlan,
    DemoTrace,
    OperationKind,
    Postcondition,
    SemanticOperation,
    Target,
    Viewport,
    WorkflowStep,
)
from productlens.execution.engine import ExecutionEngine, VerificationError
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


class ScrollTraceAdapter(Adapter):
    def __init__(self):
        self.page = ReactivePage()
        self.after_scroll = False

    async def view_state(self):
        return Viewport(width=1440, height=900), {"x": 0.0, "y": 600.0 if self.after_scroll else 0.0}

    async def execute(self, operation):
        self.after_scroll = True
        return {
            "start_y": 0.0,
            "target_y": 600.0,
            "duration_ms": 1200.0,
            "steps": 4.0,
            "path": [{"x": 0, "y": 0}, {"x": 0, "y": 180}, {"x": 0, "y": 420}, {"x": 0, "y": 600}],
        }


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


@pytest.mark.asyncio
async def test_engine_persists_intermediate_scroll_positions_in_trace():
    trace = DemoTrace(run_id="scroll", objective="Reveal details", started_at=datetime.now(UTC))
    event = await ExecutionEngine(ScrollTraceAdapter(), trace).run(
        SemanticOperation(kind=OperationKind.SCROLL_TO, intent="Reveal details", target={"name": "Details"})
    )
    assert len(event.scroll_path) == 4
    assert event.scroll_path[1]["y"] == 180
    assert event.after["scroll_motion"]["steps"] == 4


class ReactivePage:
    url = "https://example.test"

    async def wait_for_timeout(self, _milliseconds: int):
        return None


class HoldingPage(ReactivePage):
    async def wait_for_timeout(self, milliseconds: int):
        await asyncio.sleep(milliseconds / 1000)


class ScreenshotPage(ReactivePage):
    async def screenshot(self, *, path: str, full_page: bool):
        assert full_page is False
        Path(path).write_bytes(b"verified visible outcome")


class VisibleLocator:
    async def wait_for(self, **_kwargs):
        return None


class SubmitOutcomeAdapter(Adapter):
    def __init__(self):
        self.page = ScreenshotPage()

    async def visible_locator(self, _target):
        return VisibleLocator(), "role"


class UrlOutcomeAdapter(Adapter):
    def __init__(self):
        self.page = ReactivePage()

    async def execute(self, _operation):
        self.page.url = "https://example.test/records/created"

    async def snapshot(self, target):
        assert target is None or target.name != "verified created record"
        return {"url": self.page.url}


class RegroundingAdapter(Adapter):
    def __init__(self):
        self.page = ReactivePage()
        self.snapshot_calls = 0

    async def snapshot(self, target):
        self.snapshot_calls += 1
        if self.snapshot_calls == 1:
            raise GroundingError("role:0-matches")
        return {"url": self.page.url, "grounding_strategy": "role"}


class OverlayAdapter(Adapter):
    def __init__(self):
        self.page = ReactivePage()
        self.dismissed = False

    async def blocking_overlay(self, _target):
        return None if self.dismissed else "Choose workspace"

    async def dismiss_safe_overlay(self):
        self.dismissed = True
        return True


class ValueLocator:
    def __init__(self, value: str):
        self.value = value

    async def wait_for(self, **_kwargs):
        return None

    async def input_value(self):
        return self.value


class ValueAdapter(Adapter):
    def __init__(self, value: str):
        self.page = ReactivePage()
        self.locator = ValueLocator(value)

    async def grounded_locator(self, _target):
        return self.locator, "selector"


@pytest.mark.asyncio
async def test_engine_accepts_phone_control_normalization_after_visible_typing():
    engine = ExecutionEngine(ValueAdapter("9198223949"), DemoTrace(
        run_id="phone", objective="Create a safe record", started_at=datetime.now(UTC),
    ))
    await engine.verify(Postcondition(
        kind="value", expected="+91 98223949", target=Target(name="Enter Phone Number"),
    ))


@pytest.mark.asyncio
async def test_engine_keeps_exact_value_matching_for_non_phone_fields():
    engine = ExecutionEngine(ValueAdapter("maya"), DemoTrace(
        run_id="name", objective="Create a safe record", started_at=datetime.now(UTC),
    ))
    with pytest.raises(VerificationError, match="Expected Name"):
        await engine.verify(Postcondition(kind="value", expected="Maya", target=Target(name="Name")))


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
async def test_engine_clears_a_dismissible_blocking_overlay_before_scene_capture():
    trace = DemoTrace(run_id="overlay", objective="Open product", started_at=datetime.now(UTC))
    event = await ExecutionEngine(OverlayAdapter(), trace).run(
        SemanticOperation(kind=OperationKind.CLICK, intent="Open overview", target={"name": "Overview"})
    )
    assert event.success
    assert event.recovery == [{"strategy": "dismiss_safe_overlay_before_scene", "reason": "Choose workspace"}]


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


@pytest.mark.asyncio
async def test_authorized_visible_submit_captures_outcome_witness_even_in_compact_mode(tmp_path):
    trace = DemoTrace(run_id="outcome", objective="Create isolated record", started_at=datetime.now(UTC))
    artifacts = RunArtifacts(tmp_path, "outcome")
    event = await ExecutionEngine(
        SubmitOutcomeAdapter(), trace, artifacts,
        capture_event_screenshots=False,
    ).run(SemanticOperation(
        kind=OperationKind.SUBMIT,
        intent="Create the isolated record",
        target=Target(name="Create record"),
        postconditions=[Postcondition(
            kind="visible", expected="Record created", target=Target(name="Record created"),
        )],
    ))

    assert Path(event.screenshot_path).as_posix() == "execution/screenshots/001.png"
    assert (artifacts.root / event.screenshot_path).read_bytes() == b"verified visible outcome"


@pytest.mark.asyncio
async def test_submit_url_postcondition_uses_url_witness_not_abstract_dom_target():
    trace = DemoTrace(run_id="url-outcome", objective="Create isolated record", started_at=datetime.now(UTC))
    event = await ExecutionEngine(UrlOutcomeAdapter(), trace).run(SemanticOperation(
        kind=OperationKind.SUBMIT,
        intent="Create the isolated record",
        target=Target(name="Create record"),
        postconditions=[Postcondition(
            kind="url",
            expected="https://example.test/records/created",
            target=Target(name="verified created record"),
        )],
    ))

    assert event.success
    assert event.after["verified_outcome"]["expected_url"] == "https://example.test/records/created"
