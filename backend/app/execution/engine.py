"""The ProductLens-owned semantic execute → verify → trace loop."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import perf_counter
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from playwright.async_api import Error as PlaywrightError

from app.artifacts.store import RunArtifacts
from app.browser.recovery import RecoveryBudget
from app.browser.theme import discover_theme_control
from app.contracts.models import (
    ActionAttempt,
    ActionIntent,
    Affordance,
    DemoPlan,
    DemoTrace,
    FailureCode,
    InteractionEvent,
    InteractionRecoveryDecision,
    InteractionSnapshot,
    OperationKind,
    OutcomeVerification,
    Postcondition,
    ReplanDecision,
    SemanticOperation,
    Target,
    VerificationResult,
    WorkflowState,
    WorkflowStep,
)
from app.execution.playwright_adapter import GroundingError, PlaywrightAdapter
from app.execution.state_diff import state_delta
from app.interaction import InteractionDirector, InteractionKernel


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


def _safe_evidence_url(value: str) -> str:
    """Keep route identity while removing credential/token query values."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[redacted-url]"
    sensitive = re.compile(
        r"(?:token|secret|password|passwd|api[_-]?key|access[_-]?key|session|code|otp|state)",
        re.IGNORECASE,
    )
    query = [
        (key, "[redacted]") if sensitive.search(key) else (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _redact_runtime_text(value: str) -> str:
    """Remove common personal/credential-shaped values from evidence text."""
    value = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[redacted-email]", value)
    value = re.sub(r"\b(?:\+?\d[\d ()-]{7,}\d)\b", "[redacted-phone]", value)
    return re.sub(
        r"(?im)\b(password|passcode|token|secret|api[ -]?key)\b\s*[:=]\s*\S+",
        r"\1: [redacted]",
        value,
    )


def _browser_url_matches(actual: str, expected: str) -> bool:
    """Match exact canonical states and the plan's same-origin suffix form."""
    if expected.startswith("**/"):
        suffix = unquote(expected[3:]).rstrip("/")
        return _canonical_browser_url(actual).rstrip("/").endswith("/" + suffix)
    actual_canonical = _canonical_browser_url(actual)
    expected_canonical = _canonical_browser_url(expected)
    if actual_canonical == expected_canonical:
        return True
    # SPAs commonly add filters, view state, cache keys, or pagination to a
    # route after a semantic navigation click.  A plan that observed the
    # destination without query state is asserting the page identity, not an
    # exact transient query string.  Preserve strict matching whenever the
    # plan supplied query parameters, but accept same-origin path identity for
    # a query-less expected URL.
    expected_parts = urlsplit(expected_canonical)
    actual_parts = urlsplit(actual_canonical)
    same_path = (
        not expected_parts.query
        and expected_parts.scheme == actual_parts.scheme
        and expected_parts.netloc == actual_parts.netloc
        and expected_parts.path.rstrip("/") == actual_parts.path.rstrip("/")
    )
    if same_path:
        return True
    # Creation/detail routes often contain a server-assigned opaque id.  A
    # rehearsal observes one id, while production intentionally creates a new
    # isolated record and receives another.  Treat only the final segment as
    # dynamic when both values are high-entropy identifiers and the parent
    # route is identical; human-readable slugs remain strict.
    expected_path = expected_parts.path.rstrip("/").split("/")
    actual_path = actual_parts.path.rstrip("/").split("/")
    return (
        not expected_parts.query
        and expected_parts.scheme == actual_parts.scheme
        and expected_parts.netloc == actual_parts.netloc
        and len(expected_path) == len(actual_path)
        and expected_path[:-1] == actual_path[:-1]
        and len(expected_path) > 1
        and re.fullmatch(r"[a-z0-9_-]{16,}", expected_path[-1], re.IGNORECASE)
        and re.fullmatch(r"[a-z0-9_-]{16,}", actual_path[-1], re.IGNORECASE)
        and any(character.isdigit() for character in expected_path[-1])
        and any(character.isdigit() for character in actual_path[-1])
    )


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
        semantic_boundary_observer: Callable[
            [SemanticOperation, InteractionSnapshot | None], Awaitable[dict[str, object] | None]
        ]
        | None = None,
    ):
        self.adapter = adapter
        self.trace = trace
        self.artifacts = artifacts
        self.state = WorkflowState.NEW
        self.beat_hold_ms = beat_hold_ms
        self.scene_hold_ms = scene_hold_ms or {}
        self.force_light_theme = force_light_theme
        self.capture_event_screenshots = capture_event_screenshots
        self.semantic_boundary_observer = semantic_boundary_observer
        self.recovery_budget = RecoveryBudget()
        # Keep only non-sensitive values entered into the current workflow so
        # a terminal create/submit can prove that the resulting page actually
        # represents the record just demonstrated. Credentials and secret-like
        # fields are never retained here or in the persisted trace.
        self._non_sensitive_form_values: dict[str, str] = {}
        self.interaction_kernel = InteractionKernel(
            run_id=trace.run_id,
            objective=trace.objective,
        )
        self.interaction_director = InteractionDirector(self.interaction_kernel)

    def _persist_interaction_trace(self, *, complete: bool = False) -> None:
        """Write the canonical provider-neutral interaction trace when possible."""
        if not self.artifacts:
            return
        try:
            trace = self.interaction_kernel.trace(complete=complete)
            self.artifacts.write_json(
                "execution/interaction-trace.json", trace.model_dump(mode="json")
            )
        except Exception:  # noqa: BLE001 - diagnostics must not mask execution
            return

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

    async def _record_interaction_observation(self) -> InteractionSnapshot | None:
        """Capture a redacted multimodal boundary for the interaction kernel."""
        try:
            page = self._active_page()
            viewport, scroll = await self.adapter.view_state()
            evidence_reader = getattr(self.adapter, "page_evidence", None)
            evidence = await evidence_reader() if callable(evidence_reader) else {}
            if not isinstance(evidence, dict):
                evidence = {}
            text = str(evidence.get("text") or "")[:12_000]
            # Inputs are not included by page_evidence, but redact any values
            # that a custom adapter may have returned before this snapshot is
            # persisted. Credentials must never become interaction evidence.
            text = _redact_runtime_text(text)
            title = str(evidence.get("title") or "")[:240]
            controls = (
                evidence.get("controls") if isinstance(evidence.get("controls"), list) else []
            )
            safe_controls: list[dict[str, object]] = []
            for item in controls:
                if not isinstance(item, dict):
                    continue
                safe_item = dict(item)
                if "name" in safe_item:
                    safe_item["name"] = _redact_runtime_text(str(safe_item["name"]))[:240]
                safe_controls.append(safe_item)
            affordances: list[Affordance] = []
            for item in controls[:160]:
                if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                    continue
                tag = str(item.get("tag") or "").casefold()
                input_type = str(item.get("type") or "").casefold()
                method = (
                    "select"
                    if tag == "select"
                    else "type"
                    if tag in {"input", "textarea"} and input_type not in {"checkbox", "radio"}
                    else "keypress"
                    if tag == "input" and input_type in {"search", "text"}
                    else "click"
                )
                affordances.append(
                    Affordance(
                        label=_redact_runtime_text(str(item.get("name")))[:240],
                        role=str(item.get("role") or item.get("tag") or "")[:80] or None,
                        method=method,
                        target=Target(
                            name=_redact_runtime_text(str(item.get("name")))[:240],
                            role=str(item.get("role") or item.get("tag") or "")[:80] or None,
                            label=_redact_runtime_text(str(item.get("name")))[:240],
                            selector=str(item.get("selector") or "") or None,
                            source_url=_safe_evidence_url(str(getattr(page, "url", ""))),
                        ),
                        geometry=(
                            item.get("geometry") if isinstance(item.get("geometry"), dict) else {}
                        ),
                        evidence_refs=["runtime:page-evidence"],
                        confidence=0.86 if not bool(item.get("disabled")) else 0.0,
                        visible=True,
                        enabled=not bool(item.get("disabled")),
                    )
                )
            visual_terms = " ".join(
                str(item.get("name") or "") for item in controls if isinstance(item, dict)
            ).casefold()
            focused_target = None
            focused = evidence.get("focusedControl")
            if isinstance(focused, dict) and str(focused.get("name") or "").strip():
                focused_name = _redact_runtime_text(str(focused.get("name")))[:240]
                focused_target = Target(
                    name=focused_name,
                    role=str(focused.get("role") or focused.get("tag") or "")[:80] or None,
                    label=focused_name,
                    source_url=_safe_evidence_url(str(getattr(page, "url", ""))),
                )
            visual_surface = (
                "canvas"
                if "canvas" in visual_terms or "drawing" in text.casefold()
                else "dom"
                if controls or text
                else "unknown"
            )
            safe_url = _safe_evidence_url(str(getattr(page, "url", "")))
            observation_index = len(self.interaction_kernel.trace().observations) + 1
            screenshot_ref = None
            dom_ref = None
            if self.artifacts:
                observation_dir = self.artifacts.root / "execution" / "observations"
                observation_dir.mkdir(parents=True, exist_ok=True)
                evidence_path = observation_dir / f"{observation_index:03d}.json"
                self.artifacts.write_json(
                    str(evidence_path.relative_to(self.artifacts.root)),
                    {
                        "url": safe_url,
                        "title": title,
                        "text": text,
                        "controls": safe_controls,
                        "focusedControl": focused if isinstance(focused, dict) else None,
                        "overlays": evidence.get("overlays")
                        if isinstance(evidence.get("overlays"), list)
                        else [],
                        "dom": str(evidence.get("domSnapshot") or "")[:50_000],
                        "accessibility": str(evidence.get("accessibilitySnapshot") or "")[:20_000],
                    },
                )
                dom_ref = str(evidence_path.relative_to(self.artifacts.root))
                accessibility_ref = dom_ref
                try:
                    screenshot_path = observation_dir / f"{observation_index:03d}.png"
                    await page.screenshot(path=str(screenshot_path), full_page=False)
                    screenshot_ref = str(screenshot_path.relative_to(self.artifacts.root))
                except Exception:  # noqa: BLE001 - screenshot is optional evidence
                    screenshot_ref = None
            else:
                accessibility_ref = None
            observation = InteractionSnapshot(
                url=safe_url,
                title=title,
                visible_text=text,
                visible_affordances=affordances,
                screenshot_ref=screenshot_ref,
                dom_snapshot_ref=dom_ref,
                accessibility_snapshot_ref=accessibility_ref,
                viewport=viewport,
                scroll=scroll,
                focused_target=focused_target,
                overlays=[
                    str(item.get("label") or item.get("role") or "overlay")[:240]
                    for item in (evidence.get("overlays") or [])
                    if isinstance(item, dict)
                ][:24],
                visual_surface=(
                    "mixed"
                    if isinstance(evidence.get("visualSurface"), dict)
                    and evidence["visualSurface"].get("canvas")
                    and evidence["visualSurface"].get("svg")
                    else "canvas"
                    if isinstance(evidence.get("visualSurface"), dict)
                    and evidence["visualSurface"].get("canvas")
                    else visual_surface
                ),
                iframe_count=max(0, len(getattr(page, "frames", [])) - 1),
                evidence_refs=["runtime:page-evidence", "runtime:view-state"],
            )
            self.interaction_director.observe(observation)
            return observation
        except Exception:  # noqa: BLE001 - evidence must never mask browser truth
            return None

    @staticmethod
    def _action_intent_for_operation(operation: SemanticOperation) -> ActionIntent | None:
        """Convert compatible planned gestures into the kernel contract."""
        gesture_by_kind = {
            OperationKind.NAVIGATE: "wait",
            OperationKind.CLICK: "click",
            OperationKind.OPEN_NAVIGATION_ITEM: "click",
            OperationKind.OPEN_MODAL: "click",
            OperationKind.CLOSE_MODAL: "click",
            OperationKind.FILL_TEXT: "type",
            OperationKind.FILL_EMAIL: "type",
            OperationKind.FILL_PHONE: "type",
            OperationKind.SELECT_OPTION: "select",
            OperationKind.SELECT_DATE: "select",
            OperationKind.SELECT_DATE_RANGE: "select",
            OperationKind.CHECK: "click",
            OperationKind.UNCHECK: "click",
            OperationKind.CHOOSE_RADIO: "click",
            OperationKind.SEARCH: "type",
            OperationKind.APPLY_FILTER: "click",
            OperationKind.SCROLL_TO: "scroll",
            OperationKind.SUBMIT: "submit",
            OperationKind.WAIT_FOR_STATE: "wait",
            OperationKind.READ_VALUE: "observe",
            OperationKind.VERIFY_STATE: "verify",
            OperationKind.HOVER: "hover",
            OperationKind.KEY_PRESS: "key",
            OperationKind.UPLOAD: "upload",
            OperationKind.CREATE_RECORD: "submit",
            OperationKind.POINTER_SEQUENCE: "pointer_sequence",
            OperationKind.DRAG: "drag",
        }
        gesture = gesture_by_kind.get(operation.kind)
        if gesture is None:
            # Drag/pointer operations carry richer geometry than this compact
            # adapter can safely reconstruct. They remain in DemoTrace and
            # are promoted once their observed path is available.
            return None
        policy = "authorized_mutation" if _is_non_replayable_operation(operation) else "read_only"
        try:
            parameters = operation.value if isinstance(operation.value, dict) else {}
            destination = None
            if gesture == "drag" and isinstance(parameters.get("destination"), dict):
                destination = Target.model_validate(parameters["destination"])
            if gesture == "pointer_sequence" and not (
                parameters.get("points") or parameters.get("relative_points")
            ):
                return None
            return ActionIntent(
                id=operation.id,
                goal=operation.intent,
                gesture=gesture,
                target=operation.target,
                value=operation.value,
                parameters=parameters,
                destination=destination,
                expected_state=list(operation.postconditions),
                evidence_refs=list(operation.evidence_refs),
                side_effect_policy=policy,
            )
        except ValueError:
            return None

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
        before_observation: InteractionSnapshot | None = None
        after_observation: InteractionSnapshot | None = None
        semantic_boundary: dict[str, object] | None = None
        kernel_intent = self._action_intent_for_operation(operation)
        kernel_attempt: ActionAttempt | None = None
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
                    before_observation = await self._record_interaction_observation()
                    if kernel_intent is not None and before_observation is not None:
                        kernel_attempt = ActionAttempt(
                            intent=kernel_intent,
                            before_state_id=before_observation.id,
                        )
                    before = await self.adapter.snapshot(operation.target)
                    rect = await self.adapter.target_rect(operation.target)
                    if operation.kind is OperationKind.SCROLL_TO:
                        _, scroll_before = await self.adapter.view_state()
                    action_at = datetime.now(UTC)
                    action_result = await self.adapter.execute(operation)
                    if operation.kind in {
                        OperationKind.FILL_TEXT,
                        OperationKind.FILL_PHONE,
                        OperationKind.SELECT_OPTION,
                    }:
                        target_name = str(operation.target.name if operation.target else "")
                        value = str(operation.value or "").strip()
                        sensitive = re.search(
                            r"(?:password|passcode|secret|token|api[ _-]?key|otp|email)",
                            target_name,
                            re.IGNORECASE,
                        )
                        if value and not sensitive and len(value) >= 3:
                            self._non_sensitive_form_values[target_name] = value
                    if (
                        operation.kind is OperationKind.POINTER_SEQUENCE
                        and isinstance(operation.value, dict)
                        and operation.value.get("pattern")
                        in {
                            "short_reversible_stroke",
                            "connector_segment",
                        }
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
                        if condition.kind == "surface_changed" and isinstance(action_result, dict):
                            observed_surface_change = action_result.get("surface_changed")
                            if observed_surface_change is None:
                                raise VerificationError(
                                    "Pointer interaction did not provide a surface change witness"
                                )
                            if bool(observed_surface_change) is not bool(condition.expected):
                                raise VerificationError(
                                    "Expected observable editor surface change="
                                    f"{bool(condition.expected)}, got {bool(observed_surface_change)}"
                                )
                        elif (
                            condition.kind == "changed"
                            and operation.kind is OperationKind.POINTER_SEQUENCE
                            and isinstance(action_result, dict)
                            and action_result.get("surface_changed") is not None
                        ):
                            # Pointer editors frequently expose no meaningful
                            # DOM value. Prefer the clipped surface witness
                            # produced by the adapter over a broad page hash,
                            # which can be unchanged for a transparent canvas.
                            if bool(action_result["surface_changed"]) is not bool(
                                condition.expected
                            ):
                                raise VerificationError(
                                    "Expected observable editor surface change="
                                    f"{bool(condition.expected)}, got {bool(action_result['surface_changed'])}"
                                )
                        else:
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
                                visible_values: list[str] = []
                                # A dynamic detail URL is not, by itself, proof
                                # that the demonstrated form values reached the
                                # resulting record. Require at least one
                                # non-sensitive value to be visible after the
                                # route settles; this catches blank/N-A detail
                                # pages and prevents a publishable video from
                                # claiming an unverified create.
                                if self._non_sensitive_form_values:
                                    readiness = getattr(self.adapter, "wait_for_page_readiness", None)
                                    if callable(readiness):
                                        await readiness(None)
                                    body_text = await self._active_page().locator("body").inner_text()
                                    visible_values = [
                                        value
                                        for value in self._non_sensitive_form_values.values()
                                        if value.casefold() in body_text.casefold()
                                    ]
                                    if not visible_values:
                                        raise VerificationError(
                                            "Created record URL changed, but no demonstrated form value "
                                            "is visible in the resulting state"
                                        )
                                verified_outcome = await self.adapter.snapshot(None)
                                verified_outcome["expected_url"] = str(condition.expected)
                                verified_outcome["matched_form_values"] = visible_values
                            else:
                                snapshot_visible = getattr(self.adapter, "snapshot_visible", None)
                                verified_outcome = (
                                    await snapshot_visible(condition.target)
                                    if callable(snapshot_visible)
                                    else await self.adapter.snapshot(condition.target)
                                )
                    # Stagehand is consulted only at meaningful semantic
                    # boundaries (navigation/reveal/mutation/verification),
                    # never for each keystroke. Its response is advisory; the
                    # Playwright postcondition above remains authoritative.
                    if (
                        self.semantic_boundary_observer is not None
                        and operation.kind
                        in {
                            OperationKind.NAVIGATE,
                            OperationKind.OPEN_NAVIGATION_ITEM,
                            OperationKind.OPEN_MODAL,
                            OperationKind.CLOSE_MODAL,
                            OperationKind.SUBMIT,
                            OperationKind.CREATE_RECORD,
                            OperationKind.DRAG,
                            OperationKind.POINTER_SEQUENCE,
                            OperationKind.VERIFY_STATE,
                        }
                    ):
                        try:
                            boundary_observation = await self._record_interaction_observation()
                            boundary = await self.semantic_boundary_observer(
                                operation, boundary_observation
                            )
                            if isinstance(boundary, dict) and boundary:
                                semantic_boundary = boundary
                        except Exception:  # noqa: BLE001 - advisory provider cannot alter truth
                            semantic_boundary = {"status": "unavailable"}
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
                except Exception as error:
                    failure = error
                    raise
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
            if semantic_boundary is not None:
                after["semantic_boundary"] = semantic_boundary
            after_observation = await self._record_interaction_observation()
            if kernel_attempt is not None:
                kernel_attempt.dispatched = action_at is not None
                kernel_attempt.completed_at = datetime.now(UTC)
                kernel_attempt.after_state_id = (
                    after_observation.id if after_observation is not None else None
                )
                if not success:
                    kernel_attempt.error = str(failure or "interaction operation failed")[:500]
                verification = OutcomeVerification(
                    intent_id=kernel_attempt.intent.id,
                    status="passed" if success else "failed",
                    expected=list(operation.postconditions),
                    observed_state_id=(
                        after_observation.id if after_observation is not None else None
                    ),
                    evidence_refs=list(operation.evidence_refs)
                    or [f"trace:operation:{operation.id}"],
                    state_delta=state_delta(before, after),
                    confidence=1.0 if success else 0.0,
                    reason=(
                        "operation completed and postconditions verified"
                        if success
                        else str(failure or "operation failed")[:500]
                    ),
                )
                kernel_attempt.verification = VerificationResult(
                    intent_id=verification.intent_id,
                    status=verification.status,
                    expected=verification.expected,
                    observed_state_id=verification.observed_state_id,
                    evidence_refs=verification.evidence_refs,
                    confidence=verification.confidence,
                    reason=verification.reason,
                )
                try:
                    self.interaction_kernel.record_attempt(kernel_attempt)
                    self.interaction_kernel.record_verification(verification)
                    for item in recovery:
                        strategy = str(item.get("strategy") or "")
                        decision = (
                            "replace_suffix"
                            if "fallback" in strategy
                            else "stop"
                            if action_at is not None and not success
                            else "retry_before_dispatch"
                        )
                        self.interaction_kernel.record_recovery(
                            InteractionRecoveryDecision(
                                failed_attempt_id=kernel_attempt.id,
                                source_snapshot_id=kernel_attempt.before_state_id
                                or (after_observation.id if after_observation else "unknown"),
                                decision=decision,
                                reason=str(item.get("reason") or strategy or "bounded recovery")[
                                    :500
                                ],
                                side_effect_dispatched=action_at is not None,
                                evidence_refs=[f"trace:operation:{operation.id}"],
                            )
                        )
                except ValueError:
                    # Preserve browser truth even if a third-party adapter
                    # supplied a malformed optional kernel boundary.
                    pass
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
            self._persist_interaction_trace()
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
        elif condition.kind == "focused":
            if condition.target is None:
                raise VerificationError("Focused postcondition requires a semantic target")
            locator, _ = await self.adapter.grounded_locator(condition.target)
            focused = await locator.evaluate(
                "element => element === document.activeElement || element.contains(document.activeElement)"
            )
            if not focused:
                raise VerificationError(
                    f"Expected {condition.target.name!r} to retain keyboard focus"
                )
        elif condition.kind == "options_visible":
            # Native selects expose options in the element; custom comboboxes
            # expose a visible role=listbox/option surface.  This witness is
            # read-only and never opens a menu as a side effect.
            expected = str(condition.expected) if condition.expected not in (True, False) else None
            page = self.adapter.page
            if condition.target is not None:
                locator, _ = await self.adapter.grounded_locator(condition.target)
                visible = await locator.evaluate(
                    "element => Array.from(element.options || []).some(option => !option.disabled)"
                )
            else:
                visible = False
            if not visible:
                options = page.get_by_role("option")
                count = await options.count()
                for index in range(count):
                    option = options.nth(index)
                    if not await option.is_visible():
                        continue
                    if (
                        expected is None
                        or expected.casefold() in (await option.inner_text()).casefold()
                    ):
                        visible = True
                        break
            if (not visible or condition.expected is False) and bool(visible) is not bool(
                condition.expected
            ):
                raise VerificationError("Expected the select options witness to match")
        elif condition.kind == "overlay_clear":
            blocker = getattr(self.adapter, "blocking_overlay", None)
            if callable(blocker) and await blocker(condition.target) is not None:
                raise VerificationError("A visible overlay still occludes the requested state")
        else:
            raise VerificationError(f"Unknown postcondition {condition.kind}")

    def complete(self) -> DemoTrace:
        self.trace.completed_at = datetime.now(UTC)
        self.trace.final_state = WorkflowState.COMPLETE
        self.trace.outcome_verified = True
        interaction_verdicts: dict[str, str] = {}
        for item in self.interaction_kernel.trace().verifications:
            interaction_verdicts[item.intent_id] = item.status
        self._persist_interaction_trace(
            complete=all(item == "passed" for item in interaction_verdicts.values())
        )
        if self.artifacts:
            self.artifacts.save_trace(self.trace)
        return self.trace
