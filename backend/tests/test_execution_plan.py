import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.artifacts.store import RunArtifacts
from app.contracts.models import (
    ActionIntent,
    DemoPlan,
    DemoTrace,
    OperationKind,
    Postcondition,
    ReplanDecision,
    SemanticOperation,
    Target,
    Viewport,
    WorkflowStep,
)
from app.execution.engine import ExecutionEngine, VerificationError, _browser_url_matches
from app.execution.playwright_adapter import GroundingError


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


class SemanticStateLocator:
    async def evaluate(self, _script, _state):
        return True


class SemanticStateAdapter(Adapter):
    def __init__(self):
        self.page = ReactivePage()

    async def grounded_locator(self, _target):
        return SemanticStateLocator(), "role"


def test_url_postcondition_accepts_spa_query_state_for_observed_route():
    assert _browser_url_matches(
        "https://example.test/leads?view=list&page=2",
        "https://example.test/leads",
    )
    assert not _browser_url_matches(
        "https://example.test/leads?view=list",
        "https://example.test/other",
    )
    assert _browser_url_matches(
        "https://example.test/leads/6aa999ac7115170648e7b0e4",
        "https://example.test/leads/6aa9906b7115170648e74af0",
    )
    assert not _browser_url_matches(
        "https://example.test/leads/maya-shah",
        "https://example.test/leads/john-doe",
    )


class PointerNoChangeAdapter(Adapter):
    async def execute(self, operation):
        return {"points": [{"x": 1, "y": 1}, {"x": 2, "y": 2}], "surface_changed": False}


class ChangedSnapshotAdapter(Adapter):
    def __init__(self):
        self.changed = False

    async def execute(self, operation):
        self.changed = True

    async def snapshot(self, target):
        return {"url": "https://example.test", "text": "after" if self.changed else "before"}


class ScrollTraceAdapter(Adapter):
    def __init__(self):
        self.page = ReactivePage()
        self.after_scroll = False

    async def view_state(self):
        return Viewport(width=1440, height=900), {
            "x": 0.0,
            "y": 600.0 if self.after_scroll else 0.0,
        }

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
async def test_engine_compiles_action_intents_into_the_same_execution_kernel():
    trace = DemoTrace(
        run_id="intent-boundary", objective="Inspect the result", started_at=datetime.now(UTC)
    )
    event = await ExecutionEngine(Adapter(), trace).run_intent(
        ActionIntent(goal="Inspect the current result", gesture="observe")
    )
    assert event.success is True
    assert event.kind is OperationKind.READ_VALUE


@pytest.mark.asyncio
async def test_semantic_test_state_is_verified_from_grounded_control_attributes():
    engine = ExecutionEngine(SemanticStateAdapter(), DemoTrace(
        run_id="semantic-state", objective="activate a tool", started_at=datetime.now(UTC)
    ))
    await engine.verify(
        Postcondition(
            kind="test_state",
            expected="active",
            target=Target(name="Observed tool", role="button"),
        )
    )


@pytest.mark.asyncio
async def test_semantic_boundary_observer_is_not_called_for_each_typed_value():
    trace = DemoTrace(
        run_id="boundary-observer", objective="Complete the observed flow", started_at=datetime.now(UTC)
    )
    calls: list[str] = []

    async def observe(operation, snapshot):
        calls.append(operation.id)
        assert snapshot is not None
        return {"status": "observed", "sections": ["Result"]}

    engine = ExecutionEngine(Adapter(), trace, semantic_boundary_observer=observe)
    await engine.run(
        SemanticOperation(
            id="typed-field",
            kind=OperationKind.FILL_TEXT,
            intent="Enter the observed value",
            target=Target(name="Name"),
            value="Demo",
        )
    )
    await engine.run(
        SemanticOperation(
            id="verify-result",
            kind=OperationKind.VERIFY_STATE,
            intent="Verify the visible result",
        )
    )
    assert calls == ["verify-result"]
    assert trace.events[-1].after["semantic_boundary"]["sections"] == ["Result"]


@pytest.mark.asyncio
async def test_engine_persists_kernel_attempt_and_verified_outcome(tmp_path: Path):
    trace = DemoTrace(
        run_id="kernel-trace", objective="Inspect the result", started_at=datetime.now(UTC)
    )
    artifacts = RunArtifacts(tmp_path, trace.run_id)
    engine = ExecutionEngine(Adapter(), trace, artifacts, capture_event_screenshots=False)
    event = await engine.run(
        SemanticOperation(
            kind=OperationKind.CLICK,
            intent="Inspect the visible result",
            target=Target(name="Result"),
        )
    )

    assert event.success
    payload = (artifacts.root / "execution" / "interaction-trace.json").read_text()
    assert '"complete": false' in payload
    assert '"status": "passed"' in payload
    assert len(engine.interaction_kernel.trace().attempts) == 1
    assert len(engine.interaction_kernel.trace().verifications) == 1


@pytest.mark.asyncio
async def test_engine_persists_intermediate_scroll_positions_in_trace():
    trace = DemoTrace(run_id="scroll", objective="Reveal details", started_at=datetime.now(UTC))
    event = await ExecutionEngine(ScrollTraceAdapter(), trace).run(
        SemanticOperation(
            kind=OperationKind.SCROLL_TO, intent="Reveal details", target={"name": "Details"}
        )
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


class PostconditionFailureAdapter(Adapter):
    """The first click dispatches, but its expected state is no longer present."""

    def __init__(self):
        self.page = ReactivePage()
        self.calls = 0

    async def visible_locator(self, _target):
        self.calls += 1
        if self.calls == 1:
            raise GroundingError("result overlay replaced the original target")
        return VisibleLocator(), "role"


@pytest.mark.asyncio
async def test_engine_accepts_phone_control_normalization_after_visible_typing():
    engine = ExecutionEngine(
        ValueAdapter("9198223949"),
        DemoTrace(
            run_id="phone",
            objective="Create a safe record",
            started_at=datetime.now(UTC),
        ),
    )
    await engine.verify(
        Postcondition(
            kind="value",
            expected="+91 98223949",
            target=Target(name="Enter Phone Number"),
        )
    )


@pytest.mark.asyncio
async def test_engine_rejects_reversible_stroke_without_observable_surface_change():
    engine = ExecutionEngine(
        PointerNoChangeAdapter(),
        DemoTrace(
            run_id="pointer-no-change",
            objective="Draw",
            started_at=datetime.now(UTC),
        ),
    )
    operation = SemanticOperation(
        kind=OperationKind.POINTER_SEQUENCE,
        intent="Draw a reversible stroke",
        target=Target(name="drawing surface", selector="canvas"),
        value={"pattern": "short_reversible_stroke"},
        postconditions=[
            Postcondition(
                kind="visible",
                expected=True,
                target=Target(name="drawing surface", selector="canvas"),
            )
        ],
    )
    with pytest.raises(VerificationError, match="no observable change"):
        await engine.run(operation)


@pytest.mark.asyncio
async def test_engine_verifies_provider_neutral_changed_postcondition():
    adapter = ChangedSnapshotAdapter()
    engine = ExecutionEngine(
        adapter, DemoTrace(run_id="changed", objective="Change state", started_at=datetime.now(UTC))
    )
    event = await engine.run(
        SemanticOperation(
            kind=OperationKind.CLICK,
            intent="Change the observed state",
            target=Target(name="Change", selector="#change"),
            postconditions=[Postcondition(kind="changed", expected=True)],
        )
    )
    assert event.state_delta["content_changed"] is True


@pytest.mark.asyncio
async def test_engine_rejects_changed_postcondition_when_snapshot_is_identical():
    engine = ExecutionEngine(
        Adapter(),
        DemoTrace(run_id="unchanged", objective="Change state", started_at=datetime.now(UTC)),
    )
    with pytest.raises(VerificationError, match="observable state change"):
        await engine.run(
            SemanticOperation(
                kind=OperationKind.CLICK,
                intent="Change the observed state",
                target=Target(name="Change", selector="#change"),
                postconditions=[Postcondition(kind="changed", expected=True)],
            )
        )


@pytest.mark.asyncio
async def test_engine_keeps_exact_value_matching_for_non_phone_fields():
    engine = ExecutionEngine(
        ValueAdapter("maya"),
        DemoTrace(
            run_id="name",
            objective="Create a safe record",
            started_at=datetime.now(UTC),
        ),
    )
    with pytest.raises(VerificationError, match="Expected Name"):
        await engine.verify(
            Postcondition(kind="value", expected="Maya", target=Target(name="Name"))
        )


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
async def test_adaptive_execution_replaces_only_failed_suffix_and_records_replan():
    trace = DemoTrace(run_id="adaptive", objective="Show the result", started_at=datetime.now(UTC))
    plan = DemoPlan(
        objective="Show the result",
        narrative_goal="Demonstrate the observed outcome",
        audience="prospect",
        target_duration_seconds=20,
        selected_workflow="observed-flow",
        workflow_steps=[
            WorkflowStep(
                id="dispatch",
                intent="Open the result",
                operation=SemanticOperation(
                    kind=OperationKind.CLICK,
                    intent="Open the result",
                    target=Target(name="Open result"),
                    postconditions=[
                        Postcondition(
                            kind="visible",
                            expected=True,
                            target=Target(name="Result panel"),
                        )
                    ],
                ),
            ),
        ],
        expected_outcomes=["Result is visible"],
        viewport_strategy="native",
        stop_conditions=["result visible"],
    )

    async def replan(_plan, failed_step, _trace, reason, dispatched):
        assert failed_step.id == "dispatch"
        assert dispatched is True
        assert "result" in reason.lower()
        # This continuation observes the current page; it does not replay the
        # click that already dispatched in the browser.
        return ReplanDecision(
            reason="The result panel replaced the clicked control; verify the current state.",
            replacement_steps=[
                WorkflowStep(
                    id="verify-current",
                    intent="Verify the current result state",
                    operation=SemanticOperation(
                        kind=OperationKind.READ_VALUE, intent="Read current result state"
                    ),
                )
            ],
            evidence_refs=["trace:current-page"],
        )

    result = await ExecutionEngine(PostconditionFailureAdapter(), trace).run_adaptive(
        plan,
        replanner=replan,
    )
    assert result.outcome_verified
    assert len(result.events) == 2
    assert len(result.replan_decisions) == 1
    assert result.replan_decisions[0]["dispatched"] is True
    assert result.replan_decisions[0]["failed_operation_id"] == plan.workflow_steps[0].operation.id


@pytest.mark.asyncio
async def test_adaptive_execution_never_replays_dispatched_mutation_with_new_id():
    trace = DemoTrace(
        run_id="mutation-replay", objective="Create a record", started_at=datetime.now(UTC)
    )
    submit = SemanticOperation(
        kind=OperationKind.SUBMIT,
        intent="Create the authorised record",
        target=Target(name="Create"),
        side_effect_policy="authorized_mutation",
        postconditions=[
            Postcondition(kind="visible", expected=True, target=Target(name="Created record"))
        ],
    )
    plan = DemoPlan(
        objective="Create a record",
        narrative_goal="Prove the record was created",
        audience="operator",
        target_duration_seconds=20,
        selected_workflow="record",
        workflow_steps=[WorkflowStep(id="submit", intent="Create", operation=submit)],
        expected_outcomes=["Created record"],
        viewport_strategy="native",
        stop_conditions=["created"],
    )

    async def replan(_plan, _failed_step, _trace, _reason, dispatched):
        assert dispatched is True
        return ReplanDecision(
            reason="Retry the create action",
            replacement_steps=[
                WorkflowStep(
                    id="submit-again",
                    intent="Create again",
                    operation=submit.model_copy(update={"id": "new-submit"}),
                )
            ],
        )

    with pytest.raises(VerificationError, match="replay a dispatched side effect"):
        await ExecutionEngine(PostconditionFailureAdapter(), trace).run_adaptive(
            plan, replanner=replan
        )


@pytest.mark.asyncio
async def test_engine_clears_a_dismissible_blocking_overlay_before_scene_capture():
    trace = DemoTrace(run_id="overlay", objective="Open product", started_at=datetime.now(UTC))
    event = await ExecutionEngine(OverlayAdapter(), trace).run(
        SemanticOperation(
            kind=OperationKind.CLICK, intent="Open overview", target={"name": "Overview"}
        )
    )
    assert event.success
    assert event.recovery == [
        {"strategy": "dismiss_safe_overlay_before_scene", "reason": "Choose workspace"}
    ]


@pytest.mark.asyncio
async def test_engine_timestamps_visible_result_before_editorial_reading_hold():
    adapter = RegroundingAdapter()
    adapter.page = HoldingPage()
    trace = DemoTrace(run_id="hold", objective="Open product", started_at=datetime.now(UTC))
    event = await ExecutionEngine(adapter, trace, beat_hold_ms=50).run(
        SemanticOperation(
            kind=OperationKind.CLICK, intent="Open overview", target={"name": "Overview"}
        )
    )
    finished_at = datetime.now(UTC)
    # The event means the state was verified, not that its required reading
    # dwell has elapsed. This is the anchor used by captions and audio later.
    assert (finished_at - event.occurred_at).total_seconds() >= 0.035


@pytest.mark.asyncio
async def test_authorized_visible_submit_captures_outcome_witness_even_in_compact_mode(tmp_path):
    trace = DemoTrace(
        run_id="outcome", objective="Create isolated record", started_at=datetime.now(UTC)
    )
    artifacts = RunArtifacts(tmp_path, "outcome")
    event = await ExecutionEngine(
        SubmitOutcomeAdapter(),
        trace,
        artifacts,
        capture_event_screenshots=False,
    ).run(
        SemanticOperation(
            kind=OperationKind.SUBMIT,
            intent="Create the isolated record",
            target=Target(name="Create record"),
            postconditions=[
                Postcondition(
                    kind="visible",
                    expected="Record created",
                    target=Target(name="Record created"),
                )
            ],
        )
    )

    assert Path(event.screenshot_path).as_posix() == "execution/screenshots/001.png"
    assert (artifacts.root / event.screenshot_path).read_bytes() == b"verified visible outcome"


@pytest.mark.asyncio
async def test_submit_url_postcondition_uses_url_witness_not_abstract_dom_target():
    trace = DemoTrace(
        run_id="url-outcome", objective="Create isolated record", started_at=datetime.now(UTC)
    )
    event = await ExecutionEngine(UrlOutcomeAdapter(), trace).run(
        SemanticOperation(
            kind=OperationKind.SUBMIT,
            intent="Create the isolated record",
            target=Target(name="Create record"),
            postconditions=[
                Postcondition(
                    kind="url",
                    expected="https://example.test/records/created",
                    target=Target(name="verified created record"),
                )
            ],
        )
    )

    assert event.success
    assert event.after["verified_outcome"]["expected_url"] == "https://example.test/records/created"
