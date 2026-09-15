"""The ProductLens-owned semantic execute → verify → trace loop."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import perf_counter
from urllib.parse import unquote, urlsplit, urlunsplit

from playwright.async_api import Error as PlaywrightError

from productlens.artifacts.store import RunArtifacts
from productlens.browser.recovery import RecoveryBudget
from productlens.browser.theme import discover_theme_control
from productlens.contracts.models import (
    ActionIntent,
    DemoPlan,
    DemoTrace,
    FailureCode,
    InteractionEvent,
    OperationKind,
    Postcondition,
    ReplanDecision,
    SemanticOperation,
    WorkflowState,
    WorkflowStep,
)
from productlens.execution.playwright_adapter import GroundingError, PlaywrightAdapter
from productlens.execution.state_diff import state_delta


class VerificationError(RuntimeError):
    pass


def _is_non_replayable_operation(operation: SemanticOperation) -> bool:
    """Return whether dispatching the operation can create an external effect.

    The operation id is not a sufficient safety boundary: a replanner can
    accidentally issue a semantically equivalent submit with a new id.  Treat
    explicitly authorised mutations and terminal create/submit operations as
    non-replayable, while allowing read-only observation and verification to
    repair a failed postcondition.
    """
    return operation.side_effect_policy == "authorized_mutation" or operation.kind in {
        OperationKind.SUBMIT,
        OperationKind.CREATE_RECORD,
    }


def _canonical_browser_url(value: str) -> str:
    """Normalize encoded paths and query-free browser state for URL QA."""
    parsed = urlsplit(value)
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            unquote(parsed.path).rstrip("/") or "/",
            parsed.query,
            "",
        )
    )


def _browser_url_matches(actual: str, expected: str) -> bool:
    """Match exact canonical states and the plan's same-origin suffix form."""
    if expected.startswith("**/"):
        suffix = unquote(expected[3:]).rstrip("/")
        return _canonical_browser_url(actual).rstrip("/").endswith("/" + suffix)
    return _canonical_browser_url(actual) == _canonical_browser_url(expected)


def _value_matches(target_name: str, expected: object, actual: str) -> bool:
    """Compare a rendered form value using the target's observed data shape.

    Telephone controls commonly format or strip punctuation while a person is
    typing. Exact-string comparison incorrectly treats that visible, accepted
    value as a failed action. Keep strict equality for every other field and
    normalise only a target explicitly identified as phone/mobile/tel.
    """
    expected_text = str(expected)
    if actual == expected_text:
        return True
    target_words = set(re.findall(r"[a-z0-9]+", target_name.casefold()))
    if {"phone", "mobile", "telephone", "tel"} & target_words:
        expected_digits = "".join(re.findall(r"\d", expected_text))
        actual_digits = "".join(re.findall(r"\d", actual))
        return bool(expected_digits) and expected_digits == actual_digits
    return False


class ExecutionEngine:
    def __init__(
        self,
        adapter: PlaywrightAdapter,
        trace: DemoTrace,
        artifacts: RunArtifacts | None = None,
        *,
        beat_hold_ms: int = 0,
        scene_hold_ms: dict[str, int] | None = None,
        force_light_theme: bool = False,
        capture_event_screenshots: bool = True,
    ):
        self.adapter = adapter
        self.trace = trace
        self.artifacts = artifacts
        self.state = WorkflowState.NEW
        self.beat_hold_ms = beat_hold_ms
        self.scene_hold_ms = scene_hold_ms or {}
        self.force_light_theme = force_light_theme
        self.capture_event_screenshots = capture_event_screenshots
        self.recovery_budget = RecoveryBudget()

    def _active_page(self):
        # Keep lightweight adapter fakes and third-party compatibility callers
        # working while real Playwright adapters can refresh a replaced CDP
        # target after remote navigation.
        ensure = getattr(self.adapter, "ensure_page", None)
        return ensure() if callable(ensure) else self.adapter.page

    async def _stabilize_light_theme(self) -> None:
        """Wait for hydration, then use the site's real theme control once."""
        page = self.adapter.page
        await page.emulate_media(color_scheme="light")
        await page.wait_for_timeout(850)
        is_dark = await page.evaluate(
            """() => { const p = (getComputedStyle(document.body).backgroundColor.match(/\\d+/g) || []).map(Number);
            return p.length >= 3 && (p[0] * .2126 + p[1] * .7152 + p[2] * .0722) <= 150; }"""
        )
        if is_dark:
            toggle = await discover_theme_control(page)
            if toggle is not None:
                await toggle.click()
                await page.wait_for_timeout(900)
        # Do not mutate product CSS. The next verifier measures the rendered
        # result and rejects the capture if the site's own control did not win.

    async def run(
        self, operation: SemanticOperation, *, next_state: WorkflowState | None = None
    ) -> InteractionEvent:
        recovery: list[dict[str, object]] = []
        started = perf_counter()
        success = False
        page_url = getattr(self.adapter.page, "url", "")
        before: dict = {"url": page_url}
        after: dict = {"url": page_url}
        rect = None
        action_at = None
        scroll_before: dict[str, float] | None = None
        scroll_motion: dict[str, object] | None = None
        verified_outcome: dict | None = None
        action_result: object | None = None
        # This is deliberately captured *before* the editorial reading hold.
        # ``occurred_at`` is the moment the verified state became visible; a
        # hold preserves that state for the viewer, it is not part of the
        # browser operation itself.  Recording it afterwards caused the next
        # scene's screen to be paired with the outgoing scene's caption.
        occurred_at = None
        failure: Exception | None = None
        try:
            while True:
                action_dispatched = False
                try:
                    readiness = getattr(self.adapter, "wait_for_page_readiness", None)
                    if callable(readiness):
                        await readiness(operation.target)
                    for condition in operation.preconditions:
                        await self.verify(condition)
                    # Do this before the before-snapshot and scene clock. A
                    # viewer must never receive narration for content hidden
                    # behind a branch/announcement/chooser dialog. Planned
                    # form controls are allowed because the adapter proves
                    # their target belongs to the active dialog.
                    blocking_overlay = getattr(self.adapter, "blocking_overlay", None)
                    if callable(blocking_overlay):
                        blocker = await blocking_overlay(operation.target)
                        if blocker:
                            dismiss = getattr(self.adapter, "dismiss_safe_overlay", None)
                            if callable(dismiss) and await dismiss():
                                recovery.append(
                                    {
                                        "strategy": "dismiss_safe_overlay_before_scene",
                                        "reason": blocker,
                                    }
                                )
                            else:
                                raise GroundingError(f"BLOCKING_OVERLAY_UNRESOLVED: {blocker}")
                    # Both calls resolve a fresh locator from the live DOM. A
                    # retry therefore re-grounds semantically, not by reusing an
                    # old coordinate or a cached element handle.
                    before = await self.adapter.snapshot(operation.target)
                    rect = await self.adapter.target_rect(operation.target)
                    if operation.kind is OperationKind.SCROLL_TO:
                        _, scroll_before = await self.adapter.view_state()
                    action_at = datetime.now(UTC)
                    action_result = await self.adapter.execute(operation)
                    if (
                        operation.kind is OperationKind.POINTER_SEQUENCE
                        and isinstance(operation.value, dict)
                        and operation.value.get("pattern") == "short_reversible_stroke"
                        and isinstance(action_result, dict)
                        and action_result.get("surface_changed") is False
                    ):
                        raise VerificationError(
                            "Drawing gesture produced no observable change on the grounded surface"
                        )
                    if (
                        isinstance(action_result, dict)
                        and action_result.get("navigation_fallback") == "direct_after_visible_noop"
                    ):
                        recovery.append(
                            {
                                "strategy": "direct_same_origin_after_visible_noop",
                                "fallback_url": str(action_result.get("fallback_url") or ""),
                            }
                        )
                    if operation.kind is OperationKind.SCROLL_TO and isinstance(
                        action_result, dict
                    ):
                        scroll_motion = {
                            key: (
                                [
                                    {"x": float(point.get("x", 0)), "y": float(point.get("y", 0))}
                                    for point in value
                                ]
                                if key == "path" and isinstance(value, list)
                                else float(value)
                            )
                            for key, value in action_result.items()
                            if key in {"start_y", "target_y", "duration_ms", "steps", "path"}
                        }
                    if self.force_light_theme and operation.kind is OperationKind.NAVIGATE:
                        await self._stabilize_light_theme()
                    action_dispatched = True
                    if operation.kind is OperationKind.SCROLL_TO:
                        # Store the geometry after the gradual reveal, not the
                        # stale off-screen rectangle from before it began.
                        rect = await self.adapter.target_rect(operation.target)
                    # The semantic retry window ends as soon as the action has
                    # been dispatched. Retrying after a postcondition failure
                    # could duplicate a submit or another side effect.
                    for condition in operation.postconditions:
                        verification_condition = condition
                        if condition.kind == "changed" and condition.target is None:
                            verification_condition = condition.model_copy(
                                update={"target": operation.target}
                            )
                        await self.verify(verification_condition, before=before)
                        if (
                            operation.kind is OperationKind.SUBMIT
                            and condition.target is not None
                            and (
                                operation.target is None
                                or condition.target.name.casefold()
                                != operation.target.name.casefold()
                            )
                        ):
                            # Persist the independent witness, not merely the
                            # save button snapshot. Completion audit uses this
                            # exact postcondition evidence to prove a replayed
                            # create flow reached its promised result.
                            # A URL postcondition is already its own witness.
                            # Its optional target is descriptive metadata (for
                            # example ``verified created record``), not
                            # necessarily a DOM element.  Never try to ground
                            # that abstract label after navigation: doing so
                            # turns a successfully verified outcome into a
                            # ``No deterministic grounding evidence`` failure.
                            if condition.kind == "url":
                                verified_outcome = await self.adapter.snapshot(None)
                                verified_outcome["expected_url"] = str(condition.expected)
                            else:
                                snapshot_visible = getattr(self.adapter, "snapshot_visible", None)
                                verified_outcome = (
                                    await snapshot_visible(condition.target)
                                    if callable(snapshot_visible)
                                    else await self.adapter.snapshot(condition.target)
                                )
                    occurred_at = datetime.now(UTC)
                    if self.adapter.page is not None:
                        await self._active_page().wait_for_timeout(
                            max(self.beat_hold_ms, self.scene_hold_ms.get(operation.id, 0))
                        )
                    success = True
                    break
                except GroundingError as error:
                    if action_dispatched:
                        # A dispatched operation is never replayed merely
                        # because verifying it needs another locator.
                        raise
                    # A fresh production context can have a safe, dismissible
                    # dialog layered over the discovered page (for example a
                    # branch chooser).  Clear only that semantic overlay, then
                    # re-ground the same target; the recovery is attached to
                    # the scene event so it remains auditable.
                    dismiss = getattr(self.adapter, "dismiss_safe_overlay", None)
                    if callable(dismiss) and await dismiss():
                        recovery.append(
                            {"strategy": "dismiss_safe_overlay", "reason": str(error)[:500]}
                        )
                        continue
                    if not self.recovery_budget.allow_reground(
                        FailureCode.TARGET_RESOLUTION_FAILURE
                    ):
                        failure = error
                        break
                    recovery.append(
                        {
                            "strategy": "semantic_reground",
                            "reason": str(error)[:500],
                            "attempt": self.recovery_budget.used_regrounds,
                        }
                    )
                    # Let a just-rendered reactive control settle, then rebuild
                    # the deterministic locator candidates from the current DOM.
                    await self.adapter.page.wait_for_timeout(250)
        finally:
            try:
                after = await self.adapter.snapshot(operation.target)
            except GroundingError as error:
                after = {
                    "url": getattr(self.adapter.page, "url", ""),
                    "target_available": False,
                    "snapshot_error": str(error)[:500],
                }
            if verified_outcome is not None:
                after["verified_outcome"] = verified_outcome
            viewport, scroll = await self.adapter.view_state()
            event = InteractionEvent(
                operation_id=operation.id,
                kind=operation.kind,
                intent=operation.intent,
                action_at=action_at,
                occurred_at=occurred_at or datetime.now(UTC),
                target=operation.target,
                postconditions=list(operation.postconditions),
                target_rect=rect,
                viewport=viewport,
                scroll=scroll,
                page_url=str(after.get("url", "")) or None,
                before=before,
                after=after,
                state_delta=state_delta(before, after),
                success=success,
                recovery=recovery,
                duration_ms=int((perf_counter() - started) * 1000),
                page_contract_phases=list(
                    operation.page_contract_phases
                    or ([operation.story_phase] if operation.story_phase else [])
                ),
                required_content_groups=list(operation.required_content_groups),
                covered_content_groups=list(operation.covered_content_groups),
                scroll_path=(
                    list(scroll_motion.get("path", []))
                    if operation.kind is OperationKind.SCROLL_TO
                    and scroll_motion
                    and scroll_motion.get("path")
                    else [
                        {
                            "x": float((scroll_before or {}).get("x", 0)),
                            "y": float((scroll_before or {}).get("y", 0)),
                        },
                        {"x": float(scroll.get("x", 0)), "y": float(scroll.get("y", 0))},
                    ]
                    if operation.kind is OperationKind.SCROLL_TO and scroll_before is not None
                    else []
                ),
            )
            if scroll_motion:
                event.after["scroll_motion"] = scroll_motion
            if isinstance(action_result, dict) and operation.kind in {
                OperationKind.POINTER_SEQUENCE,
                OperationKind.DRAG,
                OperationKind.HOVER,
                OperationKind.KEY_PRESS,
            }:
                # These values are observed browser geometry/state, not model
                # instructions. Keeping them in the trace lets presentation
                # reproduce a real drag/path rather than drawing a decorative
                # cursor jump from the source screenshot.
                event.after["gesture"] = action_result
            # Production may turn off routine per-event screenshots for a
            # lightweight rehearsal, but an authorised mutation with a
            # viewer-facing visible postcondition is never optional evidence.
            # Keep a screenshot of that verified state even in the compact
            # mode; otherwise a later read-only recovery can prove the record
            # exists while the actual demo recording ends on the submit form.
            requires_visible_outcome_witness = operation.kind is OperationKind.SUBMIT and any(
                condition.kind == "visible" and condition.target is not None
                for condition in operation.postconditions
            )
            if self.artifacts and (
                self.capture_event_screenshots or requires_visible_outcome_witness
            ):
                screenshot = self.artifacts.screenshot_path(len(self.trace.events) + 1)
                await self._active_page().screenshot(path=str(screenshot), full_page=False)
                event.screenshot_path = str(screenshot.relative_to(self.artifacts.root))
            self.trace.events.append(event)
            # Keep a compact page-state index on the trace in addition to the
            # full Playwright trace.  Presentation and QA can therefore reason
            # about route/scroll continuity without reopening a browser session.
            self.trace.page_states.append(
                {
                    "event_id": event.id,
                    "url": event.page_url,
                    "viewport": event.viewport.model_dump(mode="json") if event.viewport else None,
                    "scroll": event.scroll,
                    "before": event.before,
                    "after": event.after,
                    "screenshot": event.screenshot_path,
                }
            )
            if operation.kind is OperationKind.NAVIGATE and action_at is not None:
                # The browser trace contains the detailed network timeline; this
                # compact interval lets presentation QA account for route-load
                # time without reopening that trace archive.
                self.trace.loading_periods.append(
                    {
                        "event_id": event.id,
                        "from_url": before.get("url"),
                        "to_url": after.get("url"),
                        "duration_ms": event.duration_ms,
                        "settled": event.success,
                    }
                )
        if next_state and success:
            self.state = next_state
            self.trace.final_state = next_state
        if not success:
            raise VerificationError(
                f"Operation failed after semantic re-ground: {operation.intent}; {failure}"
            )
        return event

    async def run_intent(
        self, intent: ActionIntent, *, next_state: WorkflowState | None = None
    ) -> InteractionEvent:
        """Execute a provider-neutral intent through the same safety kernel.

        Stagehand/model adapters produce :class:`ActionIntent` values while
        validated plans historically contain :class:`SemanticOperation`.
        Converting at this boundary keeps both paths on one observe/act/verify
        implementation and prevents a second executor from drifting in
        support for unfamiliar controls.
        """
        return await self.run(intent.to_operation(), next_state=next_state)

    async def run_plan(self, plan: DemoPlan) -> DemoTrace:
        """Execute exactly the validated semantic plan; no browser improvisation is allowed."""
        if not plan.workflow_steps:
            raise VerificationError("A production plan must contain at least one workflow step")
        for step in plan.workflow_steps:
            if step.from_state and step.from_state is not self.state:
                raise VerificationError(
                    f"Invalid plan state before {step.id}: expected {step.from_state}, got {self.state}"
                )
            await self.run(step.operation, next_state=step.to_state)
        return self.complete()

    async def run_adaptive(
        self,
        plan: DemoPlan,
        *,
        replanner: Callable[
            [DemoPlan, WorkflowStep, DemoTrace, str, bool],
            Awaitable[ReplanDecision | None],
        ]
        | None = None,
        max_replans: int = 3,
    ) -> DemoTrace:
        """Execute a plan while allowing evidence-grounded local replanning.

        The normal path is identical to :meth:`run_plan`.  When a step fails,
        an injected planner may return replacement steps for *that suffix* of
        the workflow.  The already completed prefix is never replayed.  If the
        failed operation was dispatched, its replacement is additionally
        forbidden from containing that operation ID; this prevents duplicate
        submits or other side effects while still allowing a read-only outcome
        check or a different continuation from the observed state.

        The callback is intentionally a dependency boundary: the engine owns
        browser safety and trace truth, while exploration/model code owns
        interpretation of the newly observed page.
        """
        if not plan.workflow_steps:
            raise VerificationError("An adaptive production plan needs workflow steps")
        if max_replans < 0:
            raise ValueError("max_replans must be non-negative")
        steps = list(plan.workflow_steps)
        effective_plan = plan
        index = 0
        replans = 0
        while index < len(steps):
            step = steps[index]
            try:
                if step.from_state and step.from_state is not self.state:
                    raise VerificationError(
                        f"Invalid plan state before {step.id}: expected {step.from_state}, got {self.state}"
                    )
                await self.run(step.operation, next_state=step.to_state)
                index += 1
            except (VerificationError, GroundingError) as error:
                event = self.trace.events[-1] if self.trace.events else None
                same_step = bool(event and event.operation_id == step.operation.id)
                dispatched = bool(same_step and event.action_at is not None)
                if replanner is None or replans >= max_replans:
                    raise
                decision = await replanner(plan, step, self.trace, str(error), dispatched)
                if decision is None:
                    raise
                if (
                    decision.failed_operation_id
                    and decision.failed_operation_id != step.operation.id
                ):
                    raise VerificationError(
                        "adaptive replanner returned a decision for a different failed operation"
                    )
                replacement_ids = {item.operation.id for item in decision.replacement_steps}
                if dispatched and step.operation.id in replacement_ids:
                    raise VerificationError(
                        "adaptive replanner attempted to replay a dispatched operation"
                    )
                if dispatched and _is_non_replayable_operation(step.operation):
                    replaying_mutation = next(
                        (
                            item.operation
                            for item in decision.replacement_steps
                            if _is_non_replayable_operation(item.operation)
                        ),
                        None,
                    )
                    if replaying_mutation is not None:
                        raise VerificationError(
                            "adaptive replanner attempted to replay a dispatched side effect"
                        )
                # The replacement starts at the current browser state. Keep the
                # completed prefix intact and replace only the failed suffix.
                steps[index:] = list(decision.replacement_steps)
                effective_plan = effective_plan.model_copy(
                    update={
                        "workflow_steps": [*steps[:index], *steps[index:]],
                    }
                )
                # The next replan must see the effective suffix, not the
                # original stale workflow. This matters when a changed UI
                # requires two consecutive local repairs.
                plan = effective_plan
                decision_record = decision.model_dump(mode="json")
                decision_record.update(
                    {
                        "failed_operation_id": step.operation.id,
                        "dispatched": dispatched,
                        "trace_event_id": event.id if event else None,
                    }
                )
                self.trace.replan_decisions.append(decision_record)
                if self.artifacts:
                    self.artifacts.write_json(
                        "execution/replan-decisions.json",
                        self.trace.replan_decisions,
                    )
                    self.artifacts.write_json(
                        "execution/adapted-plan.json",
                        effective_plan.model_dump(mode="json"),
                    )
                replans += 1
        return self.complete()

    async def verify(self, condition: Postcondition, *, before: dict | None = None) -> None:
        page = self.adapter.page
        if condition.kind == "url":
            expected = str(condition.expected)
            deadline = perf_counter() + condition.timeout_ms / 1000
            while not _browser_url_matches(page.url, expected):
                if perf_counter() >= deadline:
                    raise VerificationError(
                        f"Expected URL {condition.expected!r}, got {page.url!r}"
                    )
                await asyncio.sleep(0.05)
        elif condition.kind == "visible":
            if condition.target is None:
                raise VerificationError("Visible postcondition requires a semantic target")
            locator, _ = await self.adapter.visible_locator(condition.target)
            # ``visible_locator`` already performs the bounded semantic
            # visibility search.  Calling Playwright's ``wait_for`` on the
            # returned ``.first`` can race responsive canvas layers (one
            # compositing surface may be hidden immediately after a gesture)
            # and turn a valid visible witness into a misleading timeout.
            # Re-check the selected witness directly and report a truthful
            # verification failure if it disappeared.
            if hasattr(locator, "is_visible") and not await locator.is_visible():
                raise VerificationError(
                    f"Visible evidence disappeared for {condition.target.name!r}"
                )
        elif condition.kind == "value":
            locator, _ = await self.adapter.grounded_locator(condition.target)
            await locator.wait_for(timeout=condition.timeout_ms)
            try:
                # Native form controls expose input_value(). Rich text editors,
                # custom textboxes, and contenteditable surfaces do not; they
                # still have a directly observable text value that is valid
                # evidence for the same postcondition.
                actual = await locator.input_value()
            except PlaywrightError:
                if hasattr(locator, "text_content"):
                    actual = (await locator.text_content()) or ""
                elif hasattr(locator, "inner_text"):
                    actual = await locator.inner_text()
                else:
                    raise
            if not _value_matches(condition.target.name, condition.expected, actual):
                raise VerificationError(
                    f"Expected {condition.target.name}={condition.expected!r}, got {actual!r}"
                )
        elif condition.kind == "text":
            locator, _ = await self.adapter.grounded_locator(condition.target)
            await locator.get_by_text(str(condition.expected), exact=False).wait_for(
                timeout=condition.timeout_ms
            )
        elif condition.kind == "test_state":
            expression = str(condition.expected)
            await page.wait_for_function(
                f"() => Boolean({expression})", timeout=condition.timeout_ms
            )
        elif condition.kind == "changed":
            if before is None:
                raise VerificationError(
                    "Changed postconditions are valid only after a dispatched action"
                )
            target = condition.target
            observed = await self.adapter.snapshot(target)
            delta = state_delta(before, observed)
            expected = bool(condition.expected)
            if bool(delta["meaningful"]) is not expected:
                raise VerificationError(
                    f"Expected observable state change={expected}, changed fields={delta['changed_fields']}"
                )
        else:
            raise VerificationError(f"Unknown postcondition {condition.kind}")

    def complete(self) -> DemoTrace:
        self.trace.completed_at = datetime.now(UTC)
        self.trace.final_state = WorkflowState.COMPLETE
        self.trace.outcome_verified = True
        if self.artifacts:
            self.artifacts.save_trace(self.trace)
        return self.trace
