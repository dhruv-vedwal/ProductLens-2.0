from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from app.artifacts.store import RunArtifacts, materialize_trace_lifecycle
from app.browser.screencast import CdpScreencastRecorder
from app.browser.theme import discover_theme_control
from app.contracts.models import (
    ActionCapability,
    AudienceProfile,
    DemoPlan,
    DemoTrace,
    DiscoveryBudget,
    EditorialStoryboard,
    ExplorationReport,
    FormField,
    InteractionEvent,
    InteractionTrace,
    NarrationScript,
    NarrationSegment,
    ObjectiveSpec,
    ObservedElement,
    OperationKind,
    Postcondition,
    ProductContext,
    Rect,
    ReplanDecision,
    SemanticOperation,
    Target,
    Viewport,
    ViewportDecision,
    WorkflowStep,
)
from app.execution.engine import ExecutionEngine
from app.execution.playwright_adapter import GroundingError, PlaywrightAdapter
from app.narration.script import (
    bind_opening_to_first_event,
    captions_from_duration,
    recommended_caption_duration,
    script_from_trace,
)
from app.observability.logging import get_logger, redact_prompt_text
from app.planning.state_machine import WorkflowStateMachine
from app.presentation.director import build_presentation_plan
from app.presentation.editorial import (
    bind_storyboard_events,
    build_editorial_storyboard,
    editorial_script,
    enrich_editorial_brief,
    enrich_editorial_storyboard,
)
from app.presentation.journey import build_journey, inspect_journey
from app.presentation.scenes import build_scene_plan
from app.providers.errors import ProviderError
from app.quality.coverage import inspect_coverage
from app.quality.repair import classify_repair
from app.quality.story import inspect_story
from app.services.generation_policy import (
    canonical_url as _canonical_url,
)
from app.services.generation_policy import (
    production_duration_envelope as _production_duration_envelope,
)
from app.services.generation_policy import (
    recording_frame_rate as _recording_frame_rate,
)
from .render import GenerationPreconditionError


logger = get_logger("app.generation")

async def _apply_requested_visual_state(page, objective: str) -> dict[str, object]:
    """Apply and record an explicit visual state before recording begins."""
    requested_light = "light theme" in objective.lower() or "light themed" in objective.lower()
    if not requested_light:
        return {"requested": "source-default", "applied": True, "dark": None}
    try:
        # Framework theme providers commonly hydrate after DOMContentLoaded.
        # Measure after that window, not against a transient server shell.
        await page.emulate_media(color_scheme="light")
        await page.wait_for_timeout(850)
        is_dark = await page.evaluate(
            """() => {
              const parts = (getComputedStyle(document.body).backgroundColor.match(/\\d+/g) || []).map(Number);
              return parts.length >= 3 && (parts[0] * .2126 + parts[1] * .7152 + parts[2] * .0722) <= 150;
            }"""
        )
        toggle = await discover_theme_control(page)
        if is_dark and toggle is not None:
            await toggle.click()
            await page.wait_for_timeout(900)
        rendered = await page.evaluate(
            """() => {
              const color = getComputedStyle(document.body).backgroundColor;
              const parts = (color.match(/\\d+/g) || []).map(Number);
              const luminance = parts.length >= 3 ? (parts[0] * .2126 + parts[1] * .7152 + parts[2] * .0722) : 0;
              return {darkClass: document.documentElement.classList.contains('dark'), color, luminance};
            }"""
        )
        applied = float(rendered["luminance"]) > 150
        return {
            "requested": "light",
            "applied": applied,
            "dark": not applied,
            "background": rendered["color"],
        }
    except (PlaywrightError, KeyError, TypeError, ValueError):
        # This is preserved as an explicit failed visual requirement for QA;
        # it must never silently become an accidental dark-theme delivery.
        return {"requested": "light", "applied": False, "dark": None}

async def _prepare_requested_visual_state(page, objective: str) -> None:
    """Seed an explicit theme before every document navigation starts."""
    if "light theme" not in objective.lower() and "light themed" not in objective.lower():
        return
    await page.add_init_script(
        """() => {
          localStorage.setItem('theme', 'light');
          document.documentElement.classList.remove('dark');
          document.documentElement.classList.add('light');
        }"""
    )

class ExecuteMixin:
    async def _runtime_replan(
        self,
        *,
        adapter: PlaywrightAdapter,
        plan: DemoPlan,
        failed_step: WorkflowStep,
        trace: DemoTrace,
        error: str,
        dispatched: bool,
        objective: str,
        artifacts: RunArtifacts,
        cloud_session_id: str | None = None,
        browserbase_connect_url: str | None = None,
        stagehand_extension_id: str | None = None,
    ) -> ReplanDecision | None:
        """Ask the configured intelligence layer for a grounded suffix repair.

        The model receives only bounded, redacted current-page evidence.  It
        proposes semantic steps; the execution engine still owns target
        grounding, safety, postconditions, and the no-replay rule.
        """
        structured = getattr(getattr(self.planner, "provider", None), "structured", None)
        if structured is None:
            return None
        try:
            evidence = await adapter.page_evidence(max_text=6_000)
        except Exception:  # noqa: BLE001 - advisory evidence must never abort production
            return None
        # A discovered content link can disappear after a prior SPA transition
        # (for example, a card observed on the landing page is not present on a
        # component detail page).  Do not ask the model to hallucinate a new
        # target in that state.  If the validated visible-control target is no
        # longer available, use the already observed same-origin destination as
        # an explicit, auditable fallback.  This preserves the story and the
        # no-replay guarantee while keeping direct navigation a last resort.
        if (
            not dispatched
            and failed_step.operation.kind is OperationKind.OPEN_NAVIGATION_ITEM
            and failed_step.operation.target is not None
            and failed_step.operation.target.source_url
        ):
            expected_url = next(
                (
                    str(condition.expected)
                    for condition in failed_step.operation.postconditions
                    if condition.kind == "url" and condition.expected
                ),
                None,
            )
            current_url = str(evidence.get("url") or adapter.page.url)
            if expected_url:
                destination = urljoin(current_url, expected_url)
                if urlsplit(destination).netloc == urlsplit(current_url).netloc and _canonical_url(
                    destination
                ) != _canonical_url(current_url):
                    fallback = WorkflowStep(
                        id=f"{failed_step.id}-visible-control-fallback",
                        intent=(
                            f"Use the observed same-origin destination for {failed_step.operation.target.name} "
                            "because its previously observed visible control is unavailable in the current state"
                        ),
                        operation=SemanticOperation(
                            kind=OperationKind.NAVIGATE,
                            intent=(
                                f"Intentional direct-navigation fallback to the observed destination for "
                                f"{failed_step.operation.target.name}; visible control unavailable"
                            ),
                            value=destination,
                            postconditions=failed_step.operation.postconditions,
                            critical=failed_step.operation.critical,
                            story_phase=failed_step.operation.story_phase,
                            evidence_refs=[
                                *failed_step.operation.evidence_refs,
                                "fallback:visible-control-unavailable",
                            ],
                        ),
                        page_requirement=current_url,
                        importance=failed_step.importance,
                        narration_intent=failed_step.narration_intent,
                        visual_intent=failed_step.visual_intent,
                        fallback_strategy="observed same-origin destination after visible-control loss",
                        allowed_retries=0,
                        completion_criteria=failed_step.completion_criteria,
                        evidence_refs=[
                            *failed_step.evidence_refs,
                            "fallback:visible-control-unavailable",
                        ],
                        page_phase=failed_step.page_phase,
                    )
                    # ``run_adaptive`` replaces the entire remaining suffix,
                    # not only the failing step. Preserve the already
                    # validated continuation after this navigation so a
                    # recovery does not silently truncate later pages/scenes.
                    try:
                        failed_index = next(
                            index
                            for index, candidate in enumerate(plan.workflow_steps)
                            if candidate.operation.id == failed_step.operation.id
                        )
                    except StopIteration:
                        failed_index = len(plan.workflow_steps)
                    continuation = plan.workflow_steps[failed_index + 1 :]
                    return ReplanDecision(
                        reason="visible semantic control disappeared after a verified page transition",
                        failed_operation_id=failed_step.operation.id,
                        replacement_steps=[fallback, *continuation],
                    )
        text = str(evidence.get("text") or "")
        # Do not place personal/contact fields or credential-like values in a
        # model prompt or a durable artifact. Labels and visible structure are
        # sufficient to re-ground a changed UI.
        text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[redacted-email]", text)
        text = re.sub(r"\b(?:\+?\d[\d ()-]{7,}\d)\b", "[redacted-phone]", text)
        text = re.sub(
            r"(?im)\b(password|passcode|token|secret|api[ -]?key)\b\s*[:=]\s*\S+",
            r"\1: [redacted]",
            text,
        )
        stagehand_context = ""
        if self.stagehand_provider:
            try:
                stagehand_environment = (
                    "BROWSERBASE" if cloud_session_id and browserbase_connect_url else "LOCAL"
                )
                observation = await self.stagehand_provider.observe(
                    url=str(evidence.get("url") or adapter.page.url),
                    instruction=(
                        "Observe the current visible state only. Identify the relevant "
                        "read-only continuation after the failed step; do not click, type, "
                        "submit, navigate, or mutate anything."
                    ),
                    analysis_instruction="Return visible sections, meaningful controls, and safe next actions only.",
                    environment=stagehand_environment,
                    browserbase_session_id=cloud_session_id,
                    browserbase_connect_url=browserbase_connect_url,
                    browserbase_extension_id=stagehand_extension_id,
                    cache_dir=artifacts.root / "discovery" / "stagehand-cache",
                )
                if observation.analysis:
                    stagehand_context = json.dumps(
                        {
                            "visible_sections": observation.analysis.visible_sections[:12],
                            "meaningful_controls": observation.analysis.meaningful_controls[:20],
                            "safe_next_actions": observation.analysis.safe_next_actions[:12],
                        },
                        ensure_ascii=False,
                    )
            except Exception:  # noqa: BLE001 - Stagehand is an optional advisory provider
                # Stagehand is advisory. Playwright evidence remains the source
                # of truth, so a provider observation failure does not erase a
                # valid local replan opportunity.
                stagehand_context = "unavailable"
        prompt = (
            "You are the runtime recovery planner for a browser demo. Return only a "
            "ReplanDecision JSON object. Replace the failed workflow suffix from the "
            "CURRENT browser state; do not replay completed steps. Use only visible "
            "semantic targets and evidence. Every step needs a postcondition when it "
            "changes state. Prefer read-only observation/verification. Never invent "
            "URLs, selectors, claims, credentials, or hidden state. Do not emit a "
            "Navigate step. If the failed action was dispatched, the replacement must "
            "not repeat it or submit/mutate anything; verify the resulting state or take "
            "a safe read-only continuation.\n\n"
            f"Objective: {redact_prompt_text(objective)}\n"
            f"Failed step: {failed_step.intent}\n"
            f"Dispatched: {dispatched}\n"
            f"Failure: {error[:800]}\n"
            f"Current evidence: {json.dumps({'url': evidence.get('url'), 'title': evidence.get('title'), 'text': text, 'controls': evidence.get('controls', [])}, ensure_ascii=False)[:9_000]}\n"
            f"Stagehand advisory (untrusted): {stagehand_context or 'not used'}\n"
            "Return a short replacement sequence that preserves the story and can be "
            "grounded against this live page."
        )
        try:
            decision = await structured(prompt, ReplanDecision)
        except Exception:  # noqa: BLE001 - malformed provider output triggers local fallback
            return None
        if not isinstance(decision, ReplanDecision):
            return None
        allowed_read_only = {
            OperationKind.READ_VALUE,
            OperationKind.VERIFY_STATE,
            OperationKind.WAIT_FOR_STATE,
            OperationKind.SCROLL_TO,
            OperationKind.HOVER,
            OperationKind.KEY_PRESS,
            OperationKind.CLICK,
            OperationKind.OPEN_NAVIGATION_ITEM,
            OperationKind.OPEN_MODAL,
            OperationKind.CLOSE_MODAL,
            OperationKind.APPLY_FILTER,
        }
        safe_steps: list[WorkflowStep] = []
        for step in decision.replacement_steps:
            operation = step.operation
            if operation.kind is OperationKind.NAVIGATE:
                return None
            if dispatched and operation.kind not in allowed_read_only:
                return None
            if operation.kind in {
                OperationKind.SUBMIT,
                OperationKind.CHECK,
                OperationKind.UNCHECK,
                OperationKind.CREATE_RECORD,
            }:
                return None
            safe_steps.append(step)
        if not safe_steps:
            return None
        return decision.model_copy(update={"replacement_steps": safe_steps})

    async def execute_stage(
        self,
        *,
        run_id: str,
        url: str,
        objective: str,
        artifact_root: Path,
        credential_reference: str | None = None,
        cloud_production: bool = False,
    ) -> DemoTrace:
        """Execute the persisted semantic plan in a new, independently owned browser."""
        artifacts = RunArtifacts(artifact_root, run_id)
        plan = DemoPlan.model_validate(
            json.loads((artifacts.root / "plan.json").read_text(encoding="utf-8"))
        )
        context_path = artifacts.root / "discovery" / "product-context.json"
        try:
            context = ProductContext.model_validate(
                json.loads(context_path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise GenerationPreconditionError(
                "CAPABILITY_CONTEXT_MISSING: production execution requires the evidence-backed product context"
            ) from error
        state_graph_path = artifacts.root / "planning" / "validated-state-graph.json"
        if not state_graph_path.is_file():
            raise GenerationPreconditionError(
                "STATE_GRAPH_MISSING: production execution requires the validated planning state graph"
            )
        try:
            state_graph = json.loads(state_graph_path.read_text(encoding="utf-8"))
            transitions = state_graph.get("transitions", [])
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise GenerationPreconditionError(
                "STATE_GRAPH_INVALID: validated state graph is unreadable"
            ) from error
        expected_graph = WorkflowStateMachine.from_operations(
            [step.operation for step in plan.workflow_steps]
        ).artifact()
        if (
            transitions != expected_graph["transitions"]
            or state_graph.get("initial_state") != expected_graph["initial_state"]
        ):
            raise GenerationPreconditionError(
                "STATE_GRAPH_MISMATCH: persisted execution contract does not match the validated DemoPlan"
            )
        storyboard_path = artifacts.presentation / "storyboard.json"
        storyboard = (
            EditorialStoryboard.model_validate(
                json.loads(storyboard_path.read_text(encoding="utf-8"))
            )
            if storyboard_path.exists()
            else None
        )
        _effective_maximum, duration_accounting = _production_duration_envelope(plan)
        if duration_accounting:
            artifacts.write_json("presentation/duration-accounting.json", duration_accounting)
        viewport = ViewportDecision.model_validate(
            json.loads(
                (artifacts.root / "discovery" / "viewport-decision.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        async with async_playwright() as pw:
            # Keep a local browser only for the local Playwright-video path.
            # Cloud production connects to a clean Browserbase CDP session and
            # ProductLens records that exact session through Page.screencast.
            browser = await pw.chromium.launch() if not cloud_production else None
            remote = None
            session = None
            cloud_session_id: str | None = None
            production_context_id: str | None = None
            try:
                screencast: CdpScreencastRecorder | None = None
                if cloud_production:
                    if self.browserbase_provider is None:
                        raise GenerationPreconditionError(
                            "BROWSERBASE_REQUIRED: cloud production is not configured"
                        )
                    try:
                        session = await asyncio.wait_for(
                            self.browserbase_provider.create_session_info(
                                viewport={
                                    "width": viewport.viewport.width,
                                    "height": viewport.viewport.height,
                                },
                                user_metadata={"productlens_run_id": run_id, "stage": "production"},
                            ),
                            timeout=60,
                        )
                    except TimeoutError as error:
                        raise GenerationPreconditionError(
                            "BROWSERBASE_SESSION_CREATE_TIMEOUT: production session did not become ready"
                        ) from error
                    cloud_session_id = session.session_id
                    production_context_id = cloud_session_id
                    # This is deliberately separate from discovery's session:
                    # the production trace must be from a clean context.
                    artifacts.write_json(
                        "execution/browserbase-session.json",
                        {
                            "provider": "browserbase",
                            "session_id": cloud_session_id,
                            "stagehand_extension_id": session.stagehand_extension_id,
                        },
                    )
                    # Browserbase can accept session creation while the CDP
                    # endpoint is still provisioning. Bound the connect so a
                    # provider/network stall becomes a classified execution
                    # failure instead of an uncheckpointed worker hang.
                    try:
                        remote = await asyncio.wait_for(
                            pw.chromium.connect_over_cdp(session.connect_url, timeout=60_000),
                            timeout=65,
                        )
                    except TimeoutError as error:
                        raise GenerationPreconditionError(
                            "BROWSERBASE_CDP_CONNECT_TIMEOUT: production endpoint did not become ready"
                        ) from error
                    production = remote.contexts[0]
                    # Browserbase contexts are provisioned remotely, so video
                    # recording must use our CDP screencast rather than
                    # Playwright's local record_video option.
                    page = production.pages[0] if production.pages else await production.new_page()
                    await page.set_viewport_size(
                        {"width": viewport.viewport.width, "height": viewport.viewport.height}
                    )
                    video = None
                    screencast = CdpScreencastRecorder(
                        page,
                        artifacts.execution / "cloud-screencast-frames",
                        frame_rate=30,
                    )
                    await screencast.start()
                else:
                    assert browser is not None
                    production = await browser.new_context(
                        viewport={
                            "width": viewport.viewport.width,
                            "height": viewport.viewport.height,
                        },
                        color_scheme="light"
                        if "light theme" in objective.lower() or "light themed" in objective.lower()
                        else "no-preference",
                        record_video_dir=str(artifacts.execution),
                        record_video_size={
                            "width": viewport.viewport.width,
                            "height": viewport.viewport.height,
                        },
                    )
                    page = await production.new_page()
                    video = page.video
                    production_context_id = f"local:{run_id}"
                # The ProductLens DemoTrace plus Browserbase Session Replay
                # are the durable cloud evidence. Playwright tracing opens a
                # second CDP recording channel which can stall a remote
                # context before the opening page is loaded; retain it only
                # for local capture, where it is a useful diagnostic artifact.
                playwright_trace_started = remote is None
                if playwright_trace_started:
                    await production.tracing.start(
                        screenshots=True,
                        snapshots=True,
                        sources=True,
                    )
                from datetime import UTC, datetime

                trace: DemoTrace | None = DemoTrace(
                    run_id=run_id,
                    objective=objective,
                    started_at=datetime.now(UTC),
                    capability_resolutions=list(context.capability_resolutions),
                )

                async def observe_auth_action(
                    operation_id: str,
                    kind: str,
                    label: str,
                    selector: str,
                ) -> None:
                    """Append login evidence without copying secret values."""
                    assert trace is not None
                    now = datetime.now(UTC)
                    # The credential service deliberately passes a stable
                    # semantic selector to this callback, but an application
                    # is free to implement its email/user field as a text
                    # input with only a name or autocomplete attribute.  Do
                    # not let that implementation detail force a full-frame
                    # redaction (which made the login look pasted/opaque).
                    # Re-ground the geometry against the currently visible
                    # login control using generic input semantics only.
                    selector_candidates = [selector]
                    if "password" in label.casefold():
                        selector_candidates.extend(
                            [
                                'input[type="password"]',
                                'input[autocomplete="current-password"]',
                            ]
                        )
                    else:
                        selector_candidates.extend(
                            [
                                'input[type="email"]',
                                'input[name*="user" i]',
                                'input[name*="email" i]',
                                'input[autocomplete="username"]',
                            ]
                        )
                    box = None
                    for candidate_selector in dict.fromkeys(selector_candidates):
                        try:
                            candidates = page.locator(candidate_selector)
                            for candidate_index in range(await candidates.count()):
                                locator = candidates.nth(candidate_index)
                                if not await locator.is_visible():
                                    continue
                                candidate_box = await locator.bounding_box()
                                if (
                                    candidate_box
                                    and candidate_box.get("width", 0) > 1
                                    and candidate_box.get("height", 0) > 1
                                ):
                                    box = candidate_box
                                    break
                            if box is not None:
                                break
                        except PlaywrightError:
                            continue
                    target_rect = Rect(**box) if box else None
                    viewport_size = page.viewport_size or {}
                    viewport = (
                        Viewport(
                            width=int(viewport_size.get("width", 0)),
                            height=int(viewport_size.get("height", 0)),
                        )
                        if viewport_size.get("width") and viewport_size.get("height")
                        else None
                    )
                    trace.events.append(
                        InteractionEvent(
                            operation_id=operation_id,
                            kind=OperationKind(kind),
                            intent=f"Complete the {label.lower()} step of authentication",
                            occurred_at=now,
                            action_at=now,
                            target=Target(name=label, selector=selector, source_url=page.url),
                            target_rect=target_rect,
                            viewport=viewport,
                            page_url=page.url,
                            before={"url": page.url, "authentication": "redacted"},
                            after={"url": page.url, "authentication": "redacted", "action": label},
                            success=True,
                            # The credential observer is called at the
                            # beginning of a deliberate presentation hold in
                            # ``authenticate_if_required``.  Recording that
                            # interval in the trace keeps storyboard dwell,
                            # captions, and the native browser footage on the
                            # same clock without retaining any secret value.
                            duration_ms={
                                "auth:username": 5_000,
                                "auth:password": 5_000,
                                "auth:submit": 5_000,
                            }.get(operation_id, 800),
                            page_contract_phases=["establish"],
                        )
                    )

                capture_interrupted = False
                try:
                    await _prepare_requested_visual_state(page, objective)
                    # A remote CDP navigation can otherwise wait forever when
                    # Browserbase has reclaimed a page or the origin stalls.
                    # Turn that provider failure into a resumable execution
                    # outcome instead of burning the entire session lease.
                    try:
                        await asyncio.wait_for(
                            page.goto(url, wait_until="domcontentloaded"),
                            timeout=60 if cloud_production else 120,
                        )
                    except TimeoutError as error:
                        raise GenerationPreconditionError(
                            "PRODUCTION_NAVIGATION_TIMEOUT: opening page did not become ready"
                        ) from error
                    artifacts.write_json(
                        "presentation/visual-state.json",
                        await _apply_requested_visual_state(page, objective),
                    )
                    # SPAs frequently register their actionable controls just after
                    # DOMContentLoaded.  Start the verified trace only once the
                    # interface has had a bounded chance to hydrate; a strict
                    # network-idle requirement would hang on analytics/websocket
                    # traffic, so retain a short deterministic fallback.
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5_000)
                    except PlaywrightTimeoutError:
                        await page.wait_for_timeout(700)
                    # The provider recording begins when Browserbase creates a
                    # session, but the editorial demo begins only after the
                    # requested product state is stable. Anchor the trace
                    # before authentication so an objective that explicitly
                    # asks for login retains the real sequential typing and
                    # submit interaction. This still excludes the blank
                    # provision/navigation prelude because the page has
                    # already reached its settled DOM state.
                    trace.recording_started_at = datetime.now(UTC)
                    await self.credential_service.authenticate_if_required(
                        page,
                        credential_reference,
                        action_observer=observe_auth_action,
                    )
                    # A walkthrough always establishes its opening state before
                    # the first gesture. This footage is real product time, not
                    # a renderer-held screenshot.
                    opening_hold_ms = int(
                        (storyboard.scenes[0].required_dwell_seconds if storyboard else 5.0) * 1000
                    )
                    await page.wait_for_timeout(opening_hold_ms)
                    # The recorder already loaded the requested URL to capture
                    # its entrance state. Replaying an identical first Navigate
                    # immediately refreshes the page and makes a human demo look
                    # like it loaded twice.
                    execution_plan = plan
                    if (
                        plan.workflow_steps
                        and plan.workflow_steps[0].operation.kind is OperationKind.NAVIGATE
                        and _canonical_url(str(plan.workflow_steps[0].operation.value))
                        == _canonical_url(page.url)
                    ):
                        execution_plan = plan.model_copy(
                            update={"workflow_steps": plan.workflow_steps[1:]}
                        )
                    scene_holds = {
                        scene.operation_id: int(scene.required_dwell_seconds * 1000)
                        for scene in (storyboard.scenes if storyboard else [])
                        if scene.operation_id
                    }
                    try:
                        execution_adapter = PlaywrightAdapter(page, cloud_mode=remote is not None)
                        engine = ExecutionEngine(
                            # Scene-level dwell is the only viewer-facing hold.
                            # Provider round-trip latency is captured as real
                            # footage and may be removed later only when the
                            # evidence-backed editor proves it is dead time;
                            # it must not be multiplied into every operation.
                            execution_adapter,
                            trace,
                            artifacts,
                            beat_hold_ms=0,
                            scene_hold_ms=scene_holds,
                            force_light_theme="light theme" in objective.lower()
                            or "light themed" in objective.lower(),
                            capture_event_screenshots=remote is None,
                        )
                        # Cloud sessions have finite provider leases. Bound
                        # semantic execution below that lease so native-video
                        # download, screencast flush and session cleanup still
                        # run reliably. Local runs keep their normal duration.
                        if cloud_production:
                            async with asyncio.timeout(self.cloud_capture_timeout_seconds):
                                result = await engine.run_adaptive(
                                    execution_plan,
                                    replanner=lambda current_plan, failed_step, current_trace, reason, dispatched: (
                                        self._runtime_replan(
                                            adapter=execution_adapter,
                                            plan=current_plan,
                                            failed_step=failed_step,
                                            trace=current_trace,
                                            error=reason,
                                            dispatched=dispatched,
                                            objective=objective,
                                            artifacts=artifacts,
                                            cloud_session_id=cloud_session_id,
                                            browserbase_connect_url=(
                                                session.connect_url if session is not None else None
                                            ),
                                            stagehand_extension_id=(
                                                session.stagehand_extension_id
                                                if session is not None
                                                else None
                                            ),
                                        )
                                    ),
                                )
                        else:
                            result = await engine.run_adaptive(
                                execution_plan,
                                replanner=lambda current_plan, failed_step, current_trace, reason, dispatched: (
                                    self._runtime_replan(
                                        adapter=execution_adapter,
                                        plan=current_plan,
                                        failed_step=failed_step,
                                        trace=current_trace,
                                        error=reason,
                                        dispatched=dispatched,
                                        objective=objective,
                                        artifacts=artifacts,
                                    )
                                ),
                            )
                    except Exception as error:
                        # A cloud browser can be reclaimed while a plan is in
                        # progress. Preserve the evidence collected so far and
                        # make the interruption a durable execution outcome;
                        # otherwise an operator sees only a session artifact
                        # and cannot classify or repair the failing boundary.
                        trace.completed_at = datetime.now(UTC)
                        trace.outcome_verified = False
                        trace.errors.append(
                            {
                                "code": (
                                    "PRODUCTION_CAPTURE_TIMEOUT"
                                    if isinstance(error, TimeoutError)
                                    else "PRODUCTION_CAPTURE_INTERRUPTED"
                                ),
                                "exception_type": type(error).__name__,
                                "message": str(error)[:1_000],
                                "stage": "production_execution",
                                "events_captured": len(trace.events),
                                "cloud_production": cloud_production,
                            }
                        )
                        artifacts.save_trace(trace)
                        artifacts.write_json(
                            "qa/execution-report.json",
                            {
                                "outcome_verified": False,
                                "event_count": len(trace.events),
                                "hard_failures": ["PRODUCTION_CAPTURE_INTERRUPTED"],
                                "error_type": type(error).__name__,
                            },
                        )
                        capture_interrupted = True
                        raise
                finally:
                    if playwright_trace_started:
                        await production.tracing.stop(
                            path=str(artifacts.execution / "playwright-trace.zip")
                        )
                    screencast_ready = False
                    screencast_error: RuntimeError | None = None
                    if screencast is not None:
                        try:
                            await screencast.stop_capture()
                            screencast_ready = True
                        except RuntimeError as error:
                            # The native Browserbase MP4 remains the primary
                            # asset even when a sparse diagnostic screencast
                            # produced too few frames to encode.
                            screencast_error = error
                    if remote is None:
                        await production.close()
                        artifacts.preserve_browser_video(
                            Path(await video.path()) if video else None
                        )
                    else:
                        # Disconnect first so Browserbase finalizes its
                        # source-faithful Session Replay. Fetching while the
                        # CDP browser is still active can return no playlist
                        # or a truncated one. Session cleanup follows fetch.
                        try:
                            # A provider session may already be closed while
                            # the CDP websocket is still draining. Never let
                            # that disconnect block replay retrieval and the
                            # durable stage checkpoint indefinitely.
                            await asyncio.wait_for(remote.close(), timeout=15)
                        except PlaywrightError:
                            pass
                        except TimeoutError:
                            logger.warning(
                                "cloud_cdp_close_timeout",
                                run_id=run_id,
                                session_id=cloud_session_id,
                            )
                        remote = None
                        if screencast is not None:
                            try:
                                assert self.browserbase_provider is not None
                                replay_timeout_seconds = (
                                    min(120, self.cloud_capture_timeout_seconds)
                                    if capture_interrupted
                                    else self.cloud_capture_timeout_seconds
                                )
                                recording = await asyncio.wait_for(
                                    self.browserbase_provider.download_session_replay_video(
                                        session.session_id,
                                        artifacts.execution / "browser-recording.mp4",
                                        # Use the configured stage deadline for
                                        # playlist publication and local assembly.
                                        # The provider's old 180s default made a
                                        # valid long capture fail before the
                                        # Browserbase session policy elapsed.
                                        # An interrupted capture is already a
                                        # failed execution boundary.  Do not
                                        # hold the worker for another full
                                        # production lease while trying to
                                        # retrieve a replay that cannot be
                                        # delivered; successful captures retain
                                        # the configured timeout for complete
                                        # source-faithful video finalization.
                                        timeout_seconds=replay_timeout_seconds,
                                    ),
                                    timeout=replay_timeout_seconds,
                                )
                                # Bind provider metadata to this run and the
                                # bytes that were actually downloaded.  A
                                # retry must never be able to reuse a replay
                                # belonging to its parent or another run.
                                recording = {
                                    **recording,
                                    "run_id": run_id,
                                    "sha256": hashlib.sha256(
                                        (artifacts.execution / "browser-recording.mp4").read_bytes()
                                    ).hexdigest(),
                                }
                                artifacts.write_json(
                                    "execution/browserbase-recording.json", recording
                                )
                            except (ProviderError, RuntimeError, TimeoutError) as recording_error:
                                artifacts.write_json(
                                    "execution/browserbase-recording.json",
                                    {
                                        "provider": "browserbase",
                                        "native_recording": "unavailable",
                                        "error_type": type(recording_error).__name__,
                                        # Keep a bounded, non-secret diagnostic
                                        # so a delivery failure can be repaired
                                        # without guessing whether Browserbase
                                        # rejected the endpoint, timed out
                                        # assembly, or returned an empty asset.
                                        "error": str(recording_error)[:500],
                                        "capture_interrupted": capture_interrupted,
                                    },
                                )
                                # Preserve a sparse CDP stream for failure
                                # diagnostics, but never label it native footage.
                                if screencast_ready:
                                    await screencast.encode(
                                        artifacts.execution / "browser-recording.webm"
                                    )
                                elif screencast_error is not None:
                                    raise screencast_error
                        if cloud_session_id is not None:
                            assert self.browserbase_provider is not None
                            await self.browserbase_provider.close_session(cloud_session_id)
                            cloud_session_id = None
            finally:
                # Browserbase cleanup belongs to the session lifecycle, not
                # the happy path. A failed CDP connection or screencast start
                # must not leak a billable remote session.
                if remote is not None:
                    try:
                        await asyncio.wait_for(remote.close(), timeout=15)
                    except PlaywrightError:
                        pass
                    except TimeoutError:
                        logger.warning(
                            "cloud_cdp_cleanup_timeout", run_id=run_id, session_id=cloud_session_id
                        )
                if cloud_session_id is not None:
                    assert self.browserbase_provider is not None
                    await self.browserbase_provider.close_session(cloud_session_id)
                if browser is not None:
                    await browser.close()
        # Complete the trace with measured capture metadata only after the
        # browser/session has been closed and the native recording (if any) is
        # available. These fields let downstream QA distinguish source
        # evidence from compositor defaults.
        result.viewport_decision = viewport
        result.browser_zoom_percent = viewport.browser_zoom_percent
        result.browser_context_id = production_context_id
        native_recording = artifacts.execution / "browser-recording.mp4"
        result.source_frame_rate = _recording_frame_rate(native_recording)
        if result.source_frame_rate is None and remote is None:
            # Local Playwright's recorded video metadata may not be available
            # until its path is finalized; keep the field absent rather than
            # claiming a synthetic rate.
            result.source_frame_rate = _recording_frame_rate(
                artifacts.execution / "browser-recording.webm"
            )
        adapted_plan_path = artifacts.execution / "adapted-plan.json"
        if adapted_plan_path.is_file():
            # Runtime replanning is part of the owned workflow contract.  QA,
            # presentation and later narration stages must evaluate the
            # effective suffix, not the stale pre-capture plan.
            plan = DemoPlan.model_validate(
                json.loads(adapted_plan_path.read_text(encoding="utf-8"))
            )
            artifacts.write_json("plan-effective.json", plan.model_dump(mode="json"))
            artifacts.write_json(
                "planning/adapted-state-graph.json",
                WorkflowStateMachine.from_operations(
                    [step.operation for step in plan.workflow_steps]
                ).artifact(),
            )
        if (artifacts.execution / "playwright-trace.zip").is_file():
            result.dom_snapshot_refs = ["execution/playwright-trace.zip"]
            result.accessibility_snapshot_refs = ["execution/playwright-trace.zip"]
        result = materialize_trace_lifecycle(result)
        artifacts.save_trace(result)
        # Keep lifecycle records independently queryable for recovery and QA;
        # the complete trace remains the source of truth for replay.
        artifacts.write_json(
            "execution/state-snapshots.json",
            [item.model_dump(mode="json") for item in result.state_snapshots],
        )
        artifacts.write_json(
            "execution/action-attempts.json",
            [item.model_dump(mode="json") for item in result.action_attempts],
        )
        artifacts.write_json(
            "execution/verification-results.json",
            [item.model_dump(mode="json") for item in result.verification_results],
        )
        coverage = inspect_coverage(plan, result)
        artifacts.write_json("qa/coverage-report.json", coverage)
        if coverage["hard_failures"]:
            raise RuntimeError(f"Coverage QA rejected execution: {coverage['missing_outcomes']}")
        if storyboard is not None:
            storyboard = bind_storyboard_events(
                storyboard, {event.operation_id for event in result.events if event.success}
            )
            artifacts.write_json("presentation/storyboard.json", storyboard.model_dump(mode="json"))
            script = editorial_script(
                storyboard,
                {event.operation_id: event.id for event in result.events if event.success},
            )
        else:
            script = script_from_trace(result)
        story = inspect_story(result, objective=objective, script=script)
        artifacts.write_json("qa/story-report.json", story)
        if story["hard_failures"]:
            raise RuntimeError(f"Story QA rejected execution: {story['hard_failures']}")
        scenes = build_scene_plan(result, storyboard=storyboard)
        presentation = build_presentation_plan(
            result,
            viewport_width=viewport.viewport.width,
            viewport_height=viewport.viewport.height,
            allow_camera_zoom=True,
            scene_plan=scenes,
        )
        artifacts.write_json(
            "presentation/presentation-plan.json", presentation.model_dump(mode="json")
        )
        artifacts.write_json("presentation/scene-plan.json", scenes)
        journey = build_journey(result, scenes)
        artifacts.write_json("presentation/validated-scene-plan.json", journey)
        # This is intentionally separate from the pre-capture storyboard:
        # it describes the actual browser states that survived execution,
        # including real scroll/cursor evidence and page-completion proof.
        # Rendering and narration can therefore be repaired from evidence
        # without pretending the original plan occurred exactly as written.
        artifacts.write_json(
            "presentation/actual-flow-storyboard.json",
            {
                "run_id": result.run_id,
                "objective": result.objective,
                "scenes": journey,
                "trace_event_ids": [event.id for event in result.events if event.success],
                "source": "verified_demo_trace",
            },
        )
        journey_report = inspect_journey(journey)
        artifacts.write_json("quality/journey-report.json", journey_report)
        # A staged URL run must enforce the same page-completion contract as
        # the monolithic path. Persisting a failed report and continuing to
        # rendering would turn a known route sweep into an apparently valid
        # delivery after a later render/QA retry.
        if journey_report["hard_failures"]:
            artifacts.write_json(
                "qa/repair-decision.json",
                classify_repair(journey_report["hard_failures"]).model_dump(mode="json"),
            )
            raise GenerationPreconditionError(
                f"JOURNEY_QA_REJECTED: {journey_report['hard_failures']}"
            )
        artifacts.write_json("presentation/cursor-plan.json", {"paths": presentation.cursor_paths})
        artifacts.write_json(
            "qa/execution-report.json",
            {
                "outcome_verified": True,
                "event_count": len(result.events),
                "replan_count": len(result.replan_decisions),
                "adaptive_execution": bool(result.replan_decisions),
            },
        )
        return result

