"""Production URL generation: explore, plan, execute, present, and quality-check."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from productlens.artifacts.store import RunArtifacts
from productlens.browser.screencast import CdpScreencastRecorder
from productlens.contracts.models import (
    DemoPlan,
    DemoTrace,
    DiscoveryBudget,
    EditorialStoryboard,
    ExplorationReport,
    OperationKind,
    PresentationPlan,
    ProductContext,
    ViewportDecision,
)
from productlens.credentials.service import EnvironmentCredentialService
from productlens.discovery.live import LiveDiscovery
from productlens.evaluation.completion_audit import audit_run
from productlens.execution.engine import ExecutionEngine
from productlens.execution.playwright_adapter import PlaywrightAdapter
from productlens.narration.script import captions_from_duration, script_from_trace
from productlens.narration.service import NarrationService, SpeechProvider
from productlens.observability.logging import get_logger
from productlens.orchestration.lifecycle import RunStage
from productlens.planning.production import ProductionPlanningService
from productlens.presentation.director import build_presentation_plan
from productlens.presentation.editorial import (
    bind_storyboard_events,
    build_editorial_storyboard,
    editorial_script,
    enrich_editorial_brief,
    enrich_editorial_storyboard,
)
from productlens.presentation.journey import build_journey, inspect_journey
from productlens.presentation.scenes import build_scene_plan, inspect_scene_plan
from productlens.presentation.viewport import choose_viewport, probe_viewport_candidates
from productlens.providers.browserbase import BrowserbaseProvider
from productlens.providers.errors import ProviderError
from productlens.providers.stagehand import StagehandProvider
from productlens.quality.coverage import inspect_coverage
from productlens.quality.delivery import delivery_report
from productlens.quality.editorial import inspect_editorial
from productlens.quality.multimodal import build_review_packet, review_multimodal
from productlens.quality.presentation import (
    attach_presentation_qa,
    inspect_presentation,
    inspect_visual_state,
)
from productlens.quality.repair import classify_repair
from productlens.quality.story import inspect_story
from productlens.quality.synchronization import inspect_synchronization
from productlens.quality.video import inspect_video
from productlens.video.render import CaptureDurationError, render_remotion


class GenerationPreconditionError(RuntimeError):
    pass


logger = get_logger("productlens.generation")


def _canonical_url(value: str) -> str:
    """Compare browser states, not incidental redirect spelling."""
    parsed = urlsplit(value)
    scheme = "https" if parsed.scheme in {"http", "https"} else parsed.scheme
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((scheme, parsed.netloc.lower(), path, parsed.query, ""))


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
        toggle = page.get_by_role("button", name="Toggle Theme")
        if is_dark and await toggle.count():
            await toggle.first.click()
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
        return {"requested": "light", "applied": applied, "dark": not applied, "background": rendered["color"]}
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


class UrlGenerationService:
    def __init__(
        self,
        planner: ProductionPlanningService,
        speech_provider: SpeechProvider | None = None,
        browserbase_provider: BrowserbaseProvider | None = None,
        stagehand_provider: StagehandProvider | None = None,
        credential_service: EnvironmentCredentialService | None = None,
        cloud_capture_timeout_seconds: int = 840,
    ):
        self.planner = planner
        self.speech_provider = speech_provider
        self.browserbase_provider = browserbase_provider
        self.stagehand_provider = stagehand_provider
        self.credential_service = credential_service or EnvironmentCredentialService()
        self.cloud_capture_timeout_seconds = max(30, cloud_capture_timeout_seconds)
        self.discovery = LiveDiscovery()

    async def _choose_production_viewport(self, page, *, url: str, context: ProductContext, objective: str, artifacts: RunArtifacts) -> ViewportDecision:
        """Probe the clean opening layout; fall back only when a provider cannot resize."""
        try:
            # Discovery may finish on a supporting page. Viewport choice is a
            # presentation decision for the opening product context, so probe
            # the canonical requested entry route rather than the last crawl
            # location. This navigation belongs to exploration, never capture.
            if _canonical_url(page.url) != _canonical_url(url):
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_timeout(350)
            decision, probes = await probe_viewport_candidates(
                page, context.elements, objective, context.page_knowledge
            )
            artifacts.write_json("discovery/viewport-probe.json", {"selected": decision.model_dump(mode="json"), "candidates": probes})
            return decision
        except (PlaywrightError, PlaywrightTimeoutError, TypeError, ValueError) as error:
            decision = choose_viewport(context.elements, objective, context.page_knowledge)
            artifacts.write_json("discovery/viewport-probe.json", {
                "selected": decision.model_dump(mode="json"),
                "candidates": [],
                "fallback_reason": type(error).__name__,
            })
            return decision

    @staticmethod
    def _duration_floor(plan: DemoPlan) -> int | None:
        """Apply hard floors to thorough walkthroughs, not short feature flows."""
        return (
            plan.minimum_duration_seconds
            if re.search(r"\b(full|complete|entire|every|all)\b", plan.objective.lower())
            else None
        )

    async def discover_stage(
        self,
        *,
        run_id: str,
        url: str,
        objective: str,
        artifact_root: Path,
        budget: DiscoveryBudget | None = None,
        cloud_discovery: bool = False,
        known_routes: list[str] | None = None,
        known_actions: list[dict] | None = None,
        explore_visible_routes: bool = False,
        stagehand_assist: bool = False,
        credential_reference: str | None = None,
    ) -> ProductContext:
        """Discover product evidence and persist only non-secret, reloadable state."""
        artifacts = RunArtifacts(artifact_root, run_id)
        budget = budget or DiscoveryBudget()
        async with async_playwright() as pw:
            # Cloud discovery owns a Browserbase browser. Launching an extra
            # local Chromium here is wasteful and can hang before the cloud
            # timeout is even entered; only local discovery needs this browser.
            browser = None if cloud_discovery else await pw.chromium.launch()
            try:
                stagehand_evidence: dict | None = None
                if cloud_discovery:
                    if self.browserbase_provider is None:
                        raise GenerationPreconditionError(
                            "BROWSERBASE_REQUIRED: cloud discovery is not configured"
                        )
                    try:
                        session = await asyncio.wait_for(
                            self.browserbase_provider.create_session_info(), timeout=60
                        )
                    except ProviderError as error:
                        artifacts.write_json(
                            "qa/execution-report.json",
                            {
                                "outcome_verified": False,
                                "event_count": 0,
                                "hard_failures": [error.failure_code],
                                "provider": "browserbase",
                                "provider_status": error.status_code,
                            },
                        )
                        raise
                    artifacts.write_json(
                        "discovery/browserbase-session.json",
                        {"provider": "browserbase", "session_id": session.session_id},
                    )
                    remote = None
                    try:
                        remote = await asyncio.wait_for(
                            pw.chromium.connect_over_cdp(session.connect_url), timeout=60
                        )
                        context = remote.contexts[0]
                        page = context.pages[0] if context.pages else await context.new_page()
                        # Browserbase's provisioned viewport is not a stable
                        # editorial input. Lock discovery to the same desktop
                        # baseline used for local evidence collection so a
                        # responsive/mobile-only duplicate does not hide the
                        # primary navigation and force a direct-route probe.
                        await page.set_viewport_size({"width": 1440, "height": 900})
                        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                        await self.credential_service.authenticate_if_required(page, credential_reference)
                        async with asyncio.timeout(self.cloud_capture_timeout_seconds if cloud_discovery else 900):
                            product = await self.discovery.discover(
                                page, objective, budget, known_routes=known_routes, known_actions=known_actions,
                                explore_visible_routes=explore_visible_routes,
                                # Authenticated screens can contain customer data.
                                # Do not persist their pixels until redaction is a
                                # deliberate, provider-independent capability.
                                screenshot_directory=None if credential_reference else artifacts.root / "discovery" / "screenshots",
                            )
                        if stagehand_assist:
                            product, stagehand_evidence = await self._stagehand_enrich(
                                page, product, objective, environment="BROWSERBASE",
                                browserbase_session_id=session.session_id,
                            )
                        viewport = await self._choose_production_viewport(
                            page, url=url, context=product, objective=objective, artifacts=artifacts
                        )
                    finally:
                        if remote is not None:
                            await remote.close()
                        await self.browserbase_provider.close_session(session.session_id)
                else:
                    assert browser is not None
                    context = await browser.new_context(viewport={"width": 1440, "height": 900})
                    try:
                        page = await context.new_page()
                        await page.goto(url, wait_until="domcontentloaded")
                        await self.credential_service.authenticate_if_required(page, credential_reference)
                        product = await self.discovery.discover(
                            page, objective, budget, known_routes=known_routes, known_actions=known_actions,
                            explore_visible_routes=explore_visible_routes,
                            screenshot_directory=None if credential_reference else artifacts.root / "discovery" / "screenshots",
                        )
                        if stagehand_assist:
                            product, stagehand_evidence = await self._stagehand_enrich(
                                page, product, objective, environment="BROWSERBASE"
                            )
                        viewport = await self._choose_production_viewport(
                            page, url=url, context=product, objective=objective, artifacts=artifacts
                        )
                    finally:
                        await context.close()
            finally:
                if browser is not None:
                    await browser.close()
        if product.authentication_state == "login_required":
            raise GenerationPreconditionError(
                "AUTH_REQUIRED: credentials must be supplied through a secret reference"
            )
        artifacts.write_json("discovery/product-context.json", product.model_dump(mode="json"))
        artifacts.write_json("objective.json", product.objective.model_dump(mode="json") if product.objective else {"raw": objective})
        artifacts.write_json("exploration-report.json", ExplorationReport(
            pages_inspected=[page.url for page in product.page_knowledge],
            actions_probed=product.exploration_actions,
            blockers=product.blockers,
            rejected_routes=product.rejected_routes,
            candidate_flow_names=[flow.name for flow in product.candidate_demo_flows],
            stop_reason="bounded evidence sufficient",
        ).model_dump(mode="json"))
        artifacts.write_json("feature-graph.json", [feature.model_dump(mode="json") for feature in product.feature_knowledge])
        artifacts.write_json("candidate-flows.json", [flow.model_dump(mode="json") for flow in product.candidate_demo_flows])
        for index, page_knowledge in enumerate(product.page_knowledge):
            artifacts.write_json(f"page-knowledge/{index:02d}.json", page_knowledge.model_dump(mode="json"))
        artifacts.write_json("discovery/viewport-decision.json", viewport.model_dump(mode="json"))
        if stagehand_evidence is not None:
            artifacts.write_json("discovery/stagehand-observation.json", stagehand_evidence)
        return product

    async def plan_stage(
        self,
        *,
        run_id: str,
        objective: str,
        artifact_root: Path,
        allow_external_side_effects: bool,
        audience: str,
        target_duration_seconds: int,
    ) -> DemoPlan:
        """Plan from persisted discovery evidence; no browser or provider session is reused."""
        artifacts = RunArtifacts(artifact_root, run_id)
        context = ProductContext.model_validate(
            json.loads((artifacts.root / "discovery" / "product-context.json").read_text(encoding="utf-8"))
        )
        plan = await self.planner.plan(
            objective=objective,
            context=context,
            allow_external_side_effects=allow_external_side_effects,
            audience=audience,
            target_duration_seconds=target_duration_seconds,
        )
        artifacts.write_json("plan.json", plan.model_dump(mode="json"))
        storyboard = build_editorial_storyboard(context, plan)
        storyboard = await enrich_editorial_brief(context, storyboard, self.planner.provider)
        storyboard = await enrich_editorial_storyboard(context, storyboard, self.planner.provider)
        artifacts.write_json("presentation/editorial-brief.json", storyboard.brief.model_dump(mode="json"))
        artifacts.write_json("presentation/storyboard.json", storyboard.model_dump(mode="json"))
        return plan

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
        storyboard_path = artifacts.presentation / "storyboard.json"
        storyboard = EditorialStoryboard.model_validate(json.loads(storyboard_path.read_text(encoding="utf-8"))) if storyboard_path.exists() else None
        viewport = ViewportDecision.model_validate(
            json.loads((artifacts.root / "discovery" / "viewport-decision.json").read_text(encoding="utf-8"))
        )
        async with async_playwright() as pw:
            # Keep a local browser only for the local Playwright-video path.
            # Cloud production connects to a clean Browserbase CDP session and
            # ProductLens records that exact session through Page.screencast.
            browser = await pw.chromium.launch() if not cloud_production else None
            remote = None
            cloud_session_id: str | None = None
            try:
                screencast: CdpScreencastRecorder | None = None
                if cloud_production:
                    if self.browserbase_provider is None:
                        raise GenerationPreconditionError(
                            "BROWSERBASE_REQUIRED: cloud production is not configured"
                        )
                    try:
                        session = await asyncio.wait_for(
                            self.browserbase_provider.create_session_info(), timeout=60
                        )
                    except TimeoutError as error:
                        raise GenerationPreconditionError(
                            "BROWSERBASE_SESSION_CREATE_TIMEOUT: production session did not become ready"
                        ) from error
                    cloud_session_id = session.session_id
                    # This is deliberately separate from discovery's session:
                    # the production trace must be from a clean context.
                    artifacts.write_json(
                        "execution/browserbase-session.json",
                        {"provider": "browserbase", "session_id": cloud_session_id},
                    )
                    # Browserbase can accept session creation while the CDP
                    # endpoint is still provisioning. Bound the connect so a
                    # provider/network stall becomes a classified execution
                    # failure instead of an uncheckpointed worker hang.
                    try:
                        remote = await asyncio.wait_for(
                            pw.chromium.connect_over_cdp(session.connect_url), timeout=60
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
                        viewport={"width": viewport.viewport.width, "height": viewport.viewport.height},
                        color_scheme="light" if "light theme" in objective.lower() or "light themed" in objective.lower() else "no-preference",
                        record_video_dir=str(artifacts.execution),
                        record_video_size={"width": viewport.viewport.width, "height": viewport.viewport.height},
                    )
                    page = await production.new_page()
                    video = page.video
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

                recording_started_at = datetime.now(UTC)
                trace: DemoTrace | None = None
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
                    await self.credential_service.authenticate_if_required(page, credential_reference)
                    # A walkthrough always establishes its opening state before
                    # the first gesture. This footage is real product time, not
                    # a renderer-held screenshot.
                    opening_hold_ms = int((storyboard.scenes[0].required_dwell_seconds if storyboard else 5.0) * 1000)
                    await page.wait_for_timeout(opening_hold_ms)
                    trace = DemoTrace(
                        run_id=run_id,
                        objective=objective,
                        started_at=datetime.now(UTC),
                        recording_started_at=recording_started_at,
                    )
                    # The recorder already loaded the requested URL to capture
                    # its entrance state. Replaying an identical first Navigate
                    # immediately refreshes the page and makes a human demo look
                    # like it loaded twice.
                    execution_plan = plan
                    if (
                        plan.workflow_steps
                        and plan.workflow_steps[0].operation.kind is OperationKind.NAVIGATE
                        and _canonical_url(str(plan.workflow_steps[0].operation.value)) == _canonical_url(page.url)
                    ):
                        execution_plan = plan.model_copy(update={"workflow_steps": plan.workflow_steps[1:]})
                    scene_holds = {
                        scene.operation_id: int(scene.required_dwell_seconds * 1000)
                        for scene in (storyboard.scenes if storyboard else []) if scene.operation_id
                    }
                    try:
                        # Remote Browserbase commands have materially higher
                        # round-trip latency than local Playwright. Keep a
                        # deliberate readable hold, but bound it for cloud
                        # runs so a full evidence-backed story can remain in
                        # its approved 2–3 minute envelope without renderer
                        # speedups or frozen frames.
                        # Browserbase round trips already provide a long
                        # visible interaction interval (~10s per scene). Keep
                        # only a short post-action settle hold so complete
                        # walkthroughs fit the 2–3 minute delivery envelope
                        # without compressing or freezing source footage.
                        cloud_hold_ms = 700 if remote is not None else 3_000
                        if remote is not None:
                            scene_holds = {
                                operation_id: min(hold, cloud_hold_ms)
                                for operation_id, hold in scene_holds.items()
                            }
                        engine = ExecutionEngine(
                            PlaywrightAdapter(page, cloud_mode=remote is not None), trace, artifacts, beat_hold_ms=cloud_hold_ms, scene_hold_ms=scene_holds,
                            force_light_theme="light theme" in objective.lower() or "light themed" in objective.lower(),
                            capture_event_screenshots=remote is None,
                        )
                        # Cloud sessions have finite provider leases. Bound
                        # semantic execution below that lease so native-video
                        # download, screencast flush and session cleanup still
                        # run reliably. Local runs keep their normal duration.
                        if cloud_production:
                            async with asyncio.timeout(self.cloud_capture_timeout_seconds):
                                result = await engine.run_plan(execution_plan)
                        else:
                            result = await engine.run_plan(execution_plan)
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
                        await production.tracing.stop(path=str(artifacts.execution / "playwright-trace.zip"))
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
                        artifacts.preserve_browser_video(Path(await video.path()) if video else None)
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
                            logger.warning("cloud_cdp_close_timeout", run_id=run_id, session_id=cloud_session_id)
                        remote = None
                        if screencast is not None:
                            try:
                                assert self.browserbase_provider is not None
                                recording = await self.browserbase_provider.download_session_replay_video(
                                    session.session_id,
                                    artifacts.execution / "browser-recording.mp4",
                                )
                                artifacts.write_json("execution/browserbase-recording.json", recording)
                            except (ProviderError, RuntimeError) as recording_error:
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
                                    await screencast.encode(artifacts.execution / "browser-recording.webm")
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
                        logger.warning("cloud_cdp_cleanup_timeout", run_id=run_id, session_id=cloud_session_id)
                if cloud_session_id is not None:
                    assert self.browserbase_provider is not None
                    await self.browserbase_provider.close_session(cloud_session_id)
                if browser is not None:
                    await browser.close()
        artifacts.save_trace(result)
        coverage = inspect_coverage(plan, result)
        artifacts.write_json("qa/coverage-report.json", coverage)
        if coverage["hard_failures"]:
            raise RuntimeError(f"Coverage QA rejected execution: {coverage['missing_outcomes']}")
        if storyboard is not None:
            storyboard = bind_storyboard_events(storyboard, {event.operation_id for event in result.events if event.success})
            artifacts.write_json("presentation/storyboard.json", storyboard.model_dump(mode="json"))
            script = editorial_script(storyboard, {event.operation_id: event.id for event in result.events if event.success})
        else:
            script = script_from_trace(result)
        story = inspect_story(result, objective=objective, script=script)
        artifacts.write_json("qa/story-report.json", story)
        if story["hard_failures"]:
            raise RuntimeError(f"Story QA rejected execution: {story['hard_failures']}")
        scenes = build_scene_plan(result, storyboard=storyboard)
        presentation = build_presentation_plan(
            result, viewport_width=viewport.viewport.width, viewport_height=viewport.viewport.height,
            allow_camera_zoom=True, scene_plan=scenes,
        )
        artifacts.write_json("presentation/presentation-plan.json", presentation.model_dump(mode="json"))
        artifacts.write_json("presentation/scene-plan.json", scenes)
        journey = build_journey(result, scenes)
        artifacts.write_json("presentation/validated-scene-plan.json", journey)
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
            "qa/execution-report.json", {"outcome_verified": True, "event_count": len(result.events)}
        )
        return result

    async def narration_stage(
        self, *, run_id: str, artifact_root: Path, refresh_editorial: bool = False
    ) -> dict:
        artifacts = RunArtifacts(artifact_root, run_id)
        trace = self._load_trace(artifacts)
        # Regenerate deterministic editorial copy from the persisted evidence at
        # narration time. This lets a narration-only repair improve prose
        # without replaying browser actions or invalidating the DemoTrace.
        context = ProductContext.model_validate(
            json.loads((artifacts.root / "discovery" / "product-context.json").read_text(encoding="utf-8"))
        )
        plan = DemoPlan.model_validate(json.loads((artifacts.root / "plan.json").read_text(encoding="utf-8")))
        # Planning owns the approved editorial wording. Narration must never
        # rebuild a deterministic fallback over an already enriched storyboard:
        # that silently discards the evidence-reviewed script and changes scene
        # timing without a planning repair.
        storyboard_path = artifacts.presentation / "storyboard.json"
        storyboard = (
            build_editorial_storyboard(context, plan)
            if refresh_editorial or not storyboard_path.exists()
            else EditorialStoryboard.model_validate(json.loads(storyboard_path.read_text(encoding="utf-8")))
        )
        # A targeted narration repair intentionally starts from the
        # deterministic evidence-bound storyboard. The original planning pass
        # already had an opportunity to use OpenRouter; spending another pair
        # of model calls on a rejected script can reintroduce label dumps. A
        # caller may explicitly opt into a second editorial model pass through
        # the environment when investigating a provider/model regression.
        allow_repair_editorial_model = os.getenv(
            "PRODUCTLENS_REPAIR_EDITORIAL_WITH_LLM", "false"
        ).lower() in {"1", "true", "yes"}
        if refresh_editorial and allow_repair_editorial_model and self.planner is not None and self.planner.provider is not None:
            storyboard = await enrich_editorial_brief(context, storyboard, self.planner.provider)
            storyboard = await enrich_editorial_storyboard(context, storyboard, self.planner.provider)
        storyboard = bind_storyboard_events(storyboard, {event.operation_id for event in trace.events if event.success})
        artifacts.write_json("presentation/editorial-brief.json", storyboard.brief.model_dump(mode="json"))
        artifacts.write_json("presentation/storyboard.json", storyboard.model_dump(mode="json"))
        script = editorial_script(storyboard, {event.operation_id: event.id for event in trace.events if event.success}) if storyboard else script_from_trace(trace)
        editorial = inspect_editorial(
            context=context, plan=plan, trace=trace, storyboard=storyboard, script=script
        )
        artifacts.write_json("qa/editorial-report.json", editorial)
        if editorial["hard_failures"]:
            artifacts.write_json(
                "qa/repair-decision.json",
                classify_repair(editorial["hard_failures"]).model_dump(mode="json"),
            )
            raise GenerationPreconditionError(
                f"EDITORIAL_QA_REJECTED: {editorial['hard_failures']}"
            )
        captions = captions_from_duration(script, max(3.0, len(trace.events) * 1.35))
        narration = None
        if self.speech_provider:
            try:
                narration = await NarrationService().create(
                    trace, self.speech_provider, artifacts.root / "audio" / "narration.mp3", script=script
                )
                script, captions = narration["script"], narration["captions"]
            except ProviderError:
                narration = None
        payload = {"mode": "tts" if narration else "caption_only", "script": script}
        artifacts.write_json("presentation/captions.json", captions)
        artifacts.write_json("presentation/narration-script.json", payload)
        artifacts.write_json("narration/editorial-script.json", payload)
        return {**payload, "captions": captions, "audio_path": narration and narration["audio_path"]}

    def render_stage(self, *, run_id: str, artifact_root: Path) -> Path:
        artifacts = RunArtifacts(artifact_root, run_id)
        trace = self._load_trace(artifacts)
        plan = DemoPlan.model_validate(json.loads((artifacts.root / "plan.json").read_text(encoding="utf-8")))
        presentation = PresentationPlan.model_validate(
            json.loads((artifacts.presentation / "presentation-plan.json").read_text(encoding="utf-8"))
        )
        captions = json.loads((artifacts.presentation / "captions.json").read_text(encoding="utf-8"))
        scenes_path = artifacts.presentation / "validated-scene-plan.json"
        scenes = json.loads(scenes_path.read_text(encoding="utf-8")) if scenes_path.exists() else build_scene_plan(trace)
        storyboard_path = artifacts.presentation / "storyboard.json"
        storyboard = EditorialStoryboard.model_validate(json.loads(storyboard_path.read_text(encoding="utf-8"))) if storyboard_path.exists() else None
        audio = artifacts.root / "audio" / "narration.mp3"
        artifacts.write_json(
            "render/status.json",
            {
                "status": "RUNNING",
                "run_id": run_id,
                "target_duration_seconds": plan.target_duration_seconds,
                "started_at": datetime.now(UTC).isoformat(),
                "resume_from": "execution/trace.json",
                "candidate_location": str(artifacts.root / "render" / "demo.candidate.mp4"),
            },
        )
        started = perf_counter()
        try:
            output = render_remotion(
                trace, presentation, artifacts, narration_path=audio if audio.exists() else None,
                captions=captions, scenes=scenes, target_duration_seconds=plan.target_duration_seconds,
                maximum_duration_seconds=plan.maximum_duration_seconds, storyboard=storyboard,
            )
        except Exception as error:
            failure = (
                "CAPTURE_DURATION_EXCEEDS_OBJECTIVE_MAXIMUM"
                if isinstance(error, CaptureDurationError)
                else type(error).__name__
            )
            artifacts.write_json(
                "render/status.json",
                {"status": "FAILED", "run_id": run_id, "error_code": failure},
            )
            artifacts.write_json(
                "qa/repair-decision.json",
                classify_repair([failure]).model_dump(mode="json"),
            )
            raise
        artifacts.write_json(
            "render/status.json",
            {
                "status": "COMPLETE", "run_id": run_id, "location": str(output),
                "duration_ms": int((perf_counter() - started) * 1000),
                "completed_at": datetime.now(UTC).isoformat(),
            },
        )
        return output

    def qa_stage(self, *, run_id: str, artifact_root: Path) -> dict:
        artifacts = RunArtifacts(artifact_root, run_id)
        trace = self._load_trace(artifacts)
        script = json.loads((artifacts.presentation / "narration-script.json").read_text(encoding="utf-8"))["script"]
        captions_path = artifacts.presentation / "rendered-captions.json"
        captions = json.loads((captions_path if captions_path.exists() else artifacts.presentation / "captions.json").read_text(encoding="utf-8"))
        plan = DemoPlan.model_validate(json.loads((artifacts.root / "plan.json").read_text(encoding="utf-8")))
        video = inspect_video(
            artifacts.root / "final" / "demo.mp4",
            execution_verified=trace.outcome_verified,
            minimum_duration_seconds=self._duration_floor(plan),
            maximum_duration_seconds=plan.maximum_duration_seconds,
            source_video=(artifacts.root / "render" / "editorial-source.mp4")
            if (artifacts.root / "render" / "editorial-source.mp4").is_file()
            else artifacts.browser_video_path(),
        )
        # A CDP screencast is useful diagnostic evidence, but it is not an
        # acceptable production source for a Browserbase run.  If native
        # recording assembly failed, keep the diagnostic artifact for repair
        # while making the delivery decision explicitly blocking instead of
        # silently presenting the fallback as the product video.
        native_recording_meta = artifacts.execution / "browserbase-recording.json"
        if native_recording_meta.exists():
            try:
                native_meta = json.loads(native_recording_meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                native_meta = {}
            if native_meta.get("native_recording") == "unavailable":
                video["hard_failures"] = [
                    *video.get("hard_failures", []),
                    "BROWSERBASE_NATIVE_RECORDING_UNAVAILABLE",
                ]
                video["visual_score"] = 0.0
                video["overall_score"] = 0.0
        presentation = inspect_presentation(trace, json.loads((artifacts.presentation / "remotion-props.json").read_text(encoding="utf-8")))
        video = attach_presentation_qa(video, presentation)
        synchronization = inspect_synchronization(trace, script, captions, narration_requested=False, narration_created=(artifacts.root / "audio" / "narration.mp3").exists())
        story = json.loads((artifacts.qa / "story-report.json").read_text(encoding="utf-8"))
        context = ProductContext.model_validate(json.loads((artifacts.root / "discovery" / "product-context.json").read_text(encoding="utf-8")))
        storyboard_path = artifacts.presentation / "storyboard.json"
        storyboard = EditorialStoryboard.model_validate(json.loads(storyboard_path.read_text(encoding="utf-8"))) if storyboard_path.exists() else None
        actual_duration = float(video.get("probe", {}).get("format", {}).get("duration") or 0)
        # A target is an editorial planning aid, not a renderer stretch target.
        # The approved ObjectiveSpec still supplies hard lower/upper delivery
        # bounds: a presentation that exceeds its maximum needs a directed
        # dead-time/transition repair, never an arbitrary global speed-up.
        if storyboard is not None and actual_duration + 0.25 < storyboard.minimum_duration_seconds:
            video["hard_failures"] = [*video.get("hard_failures", []), "EDITORIAL_DURATION_BELOW_STORYBOARD_MINIMUM"]
            video["visual_score"] = 0.0
            video["overall_score"] = 0.0
        editorial = inspect_editorial(context=context, plan=plan, trace=trace, storyboard=storyboard, script=script)
        story = {**story, "story_score": min(float(story["story_score"]), float(editorial["editorial_score"])), "hard_failures": [*story.get("hard_failures", []), *editorial["hard_failures"]]}
        viewport = ViewportDecision.model_validate(json.loads((artifacts.root / "discovery" / "viewport-decision.json").read_text(encoding="utf-8")))
        # A targeted render/QA retry may intentionally skip execution and
        # presentation stages. Reconstruct their pure, trace-derived reports
        # instead of treating missing inherited files as a delivery failure.
        coverage_path = artifacts.qa / "coverage-report.json"
        if not coverage_path.exists():
            artifacts.write_json("qa/coverage-report.json", inspect_coverage(plan, trace))
        # Rebuild this pure report from the owned trace on every QA attempt.
        # A target-repair may inherit a stale report from a prior scene plan;
        # delivery must be gated by the current page-completion evidence.
        scene_path = artifacts.presentation / "scene-plan.json"
        scene_data = json.loads(scene_path.read_text(encoding="utf-8")) if scene_path.exists() else build_scene_plan(trace, storyboard=storyboard)
        journey_report = inspect_journey(build_journey(trace, scene_data))
        artifacts.write_json("quality/journey-report.json", journey_report)
        story = {
            **story,
            "story_score": min(float(story["story_score"]), float(journey_report["journey_score"])),
            "hard_failures": [*story.get("hard_failures", []), *journey_report["hard_failures"]],
        }
        # The delivery manifest is itself evidence-based. Persist individual QA
        # artifacts before evaluating required delivery artifacts so the first
        # successful QA attempt cannot self-reject due to write ordering.
        artifacts.write_json("qa/video-report.json", video)
        artifacts.write_json("qa/presentation-report.json", presentation)
        artifacts.write_json("qa/synchronization-report.json", synchronization)
        artifacts.write_json("qa/editorial-report.json", editorial)
        multimodal = review_multimodal(
            build_review_packet(
                video=artifacts.root / "final" / "demo.mp4",
                run_id=run_id,
                trace=trace.model_dump(mode="json"),
                storyboard=storyboard.model_dump(mode="json") if storyboard else None,
                sample_seconds=[
                    round(actual_duration * fraction, 3)
                    for fraction in (0.05, 0.2, 0.4, 0.6, 0.8, 0.95)
                    if actual_duration * fraction >= 0.1
                ],
            )
        )
        artifacts.write_json("qa/multimodal-report.json", multimodal)
        report = delivery_report(
            artifacts=artifacts.required_url_delivery_artifacts(),
            execution={"execution_score": 1.0}, story=story, video=video,
            synchronization=synchronization, visual_review=multimodal,
            viewport_score=viewport.score,
        )
        artifacts.write_json("qa/delivery-report.json", report)
        # The delivery report is part of the manifest. Hash it before running
        # the completion audit so the audit verifies the same immutable set of
        # artifacts that the worker will hand off.
        artifacts.write_manifest()
        artifacts.write_json("qa/completion-audit.json", audit_run(artifacts.root))
        # Refresh after the audit so the manifest covers every final artifact.
        artifacts.write_manifest()
        if not report["deliverable"]:
            artifacts.write_json("qa/repair-decision.json", classify_repair(report["hard_failures"]).model_dump(mode="json"))
            raise RuntimeError(f"Delivery QA rejected render: {report['hard_failures']}")
        return report

    @staticmethod
    def _load_trace(artifacts: RunArtifacts) -> DemoTrace:
        return DemoTrace.model_validate(json.loads((artifacts.execution / "trace.json").read_text(encoding="utf-8")))

    async def run(
        self,
        *,
        run_id: str,
        url: str,
        objective: str,
        artifact_root: Path,
        allow_external_side_effects: bool = False,
        render: bool = True,
        budget: DiscoveryBudget | None = None,
        cloud_discovery: bool = False,
        stage_hook: Callable[[RunStage], None] | None = None,
        known_routes: list[str] | None = None,
        known_actions: list[dict] | None = None,
        explore_visible_routes: bool = False,
        stagehand_assist: bool = False,
        credential_reference: str | None = None,
        audience: str = "product prospect",
        target_duration_seconds: int = 120,
    ) -> DemoTrace:
        """Compatibility coordinator for the durable URL-stage pipeline.

        API/worker runs already execute these stages independently. Keeping the
        direct service method on the same path prevents CLI/tests or future
        callers from bypassing persisted checkpoints and using an older
        monolithic capture/presentation implementation.
        """
        await self.discover_stage(
            run_id=run_id, url=url, objective=objective, artifact_root=artifact_root,
            budget=budget, cloud_discovery=cloud_discovery, known_routes=known_routes,
            known_actions=known_actions, explore_visible_routes=explore_visible_routes,
            stagehand_assist=stagehand_assist, credential_reference=credential_reference,
        )
        if stage_hook:
            stage_hook(RunStage.DISCOVERING)
        await self.plan_stage(
            run_id=run_id, objective=objective, artifact_root=artifact_root,
            allow_external_side_effects=allow_external_side_effects, audience=audience,
            target_duration_seconds=target_duration_seconds,
        )
        if stage_hook:
            stage_hook(RunStage.PLAN_VALIDATED)
        trace = await self.execute_stage(
            run_id=run_id, url=url, objective=objective, artifact_root=artifact_root,
            credential_reference=credential_reference, cloud_production=cloud_discovery,
        )
        if stage_hook:
            stage_hook(RunStage.TRACE_READY)
        await self.narration_stage(run_id=run_id, artifact_root=artifact_root)
        if stage_hook:
            stage_hook(RunStage.NARRATION_READY)
        if render:
            self.render_stage(run_id=run_id, artifact_root=artifact_root)
            self.qa_stage(run_id=run_id, artifact_root=artifact_root)
            if stage_hook:
                stage_hook(RunStage.QA_PASSED)
        return trace

        # Retained below temporarily as a source-level reference while staged
        # compatibility is exercised. It is unreachable and no URL caller may
        # use it; removal follows after the durable-path regression suite.
        artifacts = RunArtifacts(artifact_root, run_id)
        budget = budget or DiscoveryBudget()
        async with async_playwright() as pw:
            # The monolithic compatibility path must use the same cloud-only
            # discovery ownership as the staged path. Launching a local
            # browser before Browserbase wastes resources and creates a
            # distinct, non-durable execution path.
            browser = None if cloud_discovery else await pw.chromium.launch()
            try:
                discovery_session_id: str | None = None
                authenticated_storage_state: dict | None = None
                if cloud_discovery:
                    if self.browserbase_provider is None:
                        raise GenerationPreconditionError(
                            "BROWSERBASE_REQUIRED: cloud discovery is not configured"
                        )
                    session = await asyncio.wait_for(
                        self.browserbase_provider.create_session_info(), timeout=60
                    )
                    discovery_session_id = session.session_id
                    # Persist the external identity before connecting so a
                    # connection failure still leaves an auditable cleanup record.
                    artifacts.write_json(
                        "discovery/browserbase-session.json",
                        {"provider": "browserbase", "session_id": discovery_session_id},
                    )
                    remote = None
                    try:
                        remote = await asyncio.wait_for(
                            pw.chromium.connect_over_cdp(session.connect_url), timeout=60
                        )
                        exploration = remote.contexts[0]
                        page = (
                            exploration.pages[0]
                            if exploration.pages
                            else await exploration.new_page()
                        )
                        await page.goto(url, wait_until="domcontentloaded")
                        # Cloud discovery obeys the same secret-reference-only
                        # authentication contract as local discovery. No
                        # credential value is written to the Browserbase
                        # session artifact or persistent run payload.
                        await self.credential_service.authenticate_if_required(
                            page, credential_reference
                        )
                        context = await self.discovery.discover(
                            page, objective, budget, known_routes=known_routes, known_actions=known_actions,
                            explore_visible_routes=explore_visible_routes,
                            screenshot_directory=None if credential_reference else artifacts.root / "discovery" / "screenshots",
                        )
                        if stagehand_assist:
                            context, stagehand_evidence = await self._stagehand_enrich(
                                page, context, objective, environment="BROWSERBASE",
                                browserbase_session_id=discovery_session_id,
                            )
                    finally:
                        if remote is not None:
                            await remote.close()
                        # ProductLens owns the full session lifecycle.  A failed
                        # cleanup is surfaced as provider evidence, never hidden
                        # behind a successful CDP disconnect.
                        await self.browserbase_provider.close_session(discovery_session_id)
                else:
                    exploration = await browser.new_context(
                        viewport={"width": 1440, "height": 900},
                        color_scheme="light" if "light theme" in objective.lower() or "light themed" in objective.lower() else "no-preference",
                    )
                    page = await exploration.new_page()
                    await _prepare_requested_visual_state(page, objective)
                    await page.goto(url, wait_until="domcontentloaded")
                    artifacts.write_json(
                        "presentation/visual-state.json",
                        await _apply_requested_visual_state(page, objective),
                    )
                    await self.credential_service.authenticate_if_required(page, credential_reference)
                    authenticated_storage_state = await exploration.storage_state()
                    context = await self.discovery.discover(
                        page, objective, budget, known_routes=known_routes, known_actions=known_actions,
                        explore_visible_routes=explore_visible_routes,
                        screenshot_directory=None if credential_reference else artifacts.root / "discovery" / "screenshots",
                    )
                    if stagehand_assist:
                        context, stagehand_evidence = await self._stagehand_enrich(
                            page, context, objective, environment="BROWSERBASE"
                        )
                    viewport_decision = await self._choose_production_viewport(
                        page, url=url, context=context, objective=objective, artifacts=artifacts
                    )
                    await exploration.close()
                if stagehand_assist:
                    artifacts.write_json("discovery/stagehand-observation.json", stagehand_evidence)
                artifacts.write_json(
                    "discovery/product-context.json", context.model_dump(mode="json")
                )
                artifacts.write_json("objective.json", context.objective.model_dump(mode="json") if context.objective else {"raw": objective})
                artifacts.write_json("exploration-report.json", ExplorationReport(
                    pages_inspected=[page.url for page in context.page_knowledge],
                    actions_probed=context.exploration_actions,
                    blockers=context.blockers,
                    rejected_routes=context.rejected_routes,
                    candidate_flow_names=[flow.name for flow in context.candidate_demo_flows],
                    stop_reason="bounded evidence sufficient",
                ).model_dump(mode="json"))
                artifacts.write_json("feature-graph.json", [feature.model_dump(mode="json") for feature in context.feature_knowledge])
                artifacts.write_json("candidate-flows.json", [flow.model_dump(mode="json") for flow in context.candidate_demo_flows])
                for index, page_knowledge in enumerate(context.page_knowledge):
                    artifacts.write_json(f"page-knowledge/{index:02d}.json", page_knowledge.model_dump(mode="json"))
                artifacts.write_json(
                    "discovery/viewport-decision.json", viewport_decision.model_dump(mode="json")
                )
                if stage_hook:
                    stage_hook(RunStage.PLAN_READY)
                if context.authentication_state == "login_required":
                    raise GenerationPreconditionError(
                        "AUTH_REQUIRED: credentials must be supplied through a secret reference"
                    )
                plan = await self.planner.plan(
                    objective=objective,
                    context=context,
                    allow_external_side_effects=allow_external_side_effects,
                    audience=audience,
                    target_duration_seconds=target_duration_seconds,
                )
                artifacts.write_json("plan.json", plan.model_dump(mode="json"))
                storyboard = build_editorial_storyboard(context, plan)
                storyboard = await enrich_editorial_brief(context, storyboard, self.planner.provider)
                storyboard = await enrich_editorial_storyboard(context, storyboard, self.planner.provider)
                artifacts.write_json("presentation/editorial-brief.json", storyboard.brief.model_dump(mode="json"))
                artifacts.write_json("presentation/storyboard.json", storyboard.model_dump(mode="json"))
                if stage_hook:
                    stage_hook(RunStage.PLAN_VALIDATED)
                    stage_hook(RunStage.PRODUCTION_EXECUTION)
                production = await browser.new_context(
                    viewport={
                        "width": viewport_decision.viewport.width,
                        "height": viewport_decision.viewport.height,
                    },
                    color_scheme="light" if "light theme" in objective.lower() or "light themed" in objective.lower() else "no-preference",
                    record_video_dir=str(artifacts.execution),
                    record_video_size={
                        "width": viewport_decision.viewport.width,
                        "height": viewport_decision.viewport.height,
                    },
                    storage_state=authenticated_storage_state,
                )
                await production.tracing.start(screenshots=True, snapshots=True, sources=True)
                from datetime import UTC, datetime

                recording_started_at = datetime.now(UTC)
                page = await production.new_page()
                video = page.video
                result: DemoTrace | None = None
                try:
                    await _prepare_requested_visual_state(page, objective)
                    await page.goto(url, wait_until="domcontentloaded")
                    artifacts.write_json(
                        "presentation/visual-state.json",
                        await _apply_requested_visual_state(page, objective),
                    )
                    await page.wait_for_timeout(int(storyboard.scenes[0].required_dwell_seconds * 1000))
                    trace = DemoTrace(
                        run_id=run_id,
                        objective=objective,
                        started_at=datetime.now(UTC),
                        recording_started_at=recording_started_at,
                    )
                    execution_plan = plan
                    if (
                        plan.workflow_steps
                        and plan.workflow_steps[0].operation.kind is OperationKind.NAVIGATE
                        and _canonical_url(str(plan.workflow_steps[0].operation.value)) == _canonical_url(page.url)
                    ):
                        execution_plan = plan.model_copy(update={"workflow_steps": plan.workflow_steps[1:]})
                    scene_holds = {scene.operation_id: int(scene.required_dwell_seconds * 1000) for scene in storyboard.scenes if scene.operation_id}
                    result = await ExecutionEngine(
                        PlaywrightAdapter(page), trace, artifacts, beat_hold_ms=3_000, scene_hold_ms=scene_holds,
                        force_light_theme="light theme" in objective.lower() or "light themed" in objective.lower(),
                    ).run_plan(execution_plan)
                finally:
                    await production.tracing.stop(
                        path=str(artifacts.execution / "playwright-trace.zip")
                    )
                    await production.close()
                    artifacts.preserve_browser_video(Path(await video.path()) if video else None)
            finally:
                await browser.close()
        if result is None:
            raise RuntimeError("Production execution completed without a trace")
        artifacts.save_trace(result)
        coverage = inspect_coverage(plan, result)
        artifacts.write_json("qa/coverage-report.json", coverage)
        if coverage["hard_failures"]:
            raise RuntimeError(f"Coverage QA rejected execution: {coverage['missing_outcomes']}")
        if stage_hook:
            stage_hook(RunStage.TRACE_READY)
        storyboard = bind_storyboard_events(storyboard, {event.operation_id for event in result.events if event.success})
        artifacts.write_json("presentation/storyboard.json", storyboard.model_dump(mode="json"))
        script = editorial_script(storyboard, {event.operation_id: event.id for event in result.events if event.success})
        story = inspect_story(result, objective=objective, script=script)
        artifacts.write_json("qa/story-report.json", story)
        if story["hard_failures"]:
            raise RuntimeError(f"Story QA rejected execution: {story['hard_failures']}")
        scenes = build_scene_plan(result, storyboard=storyboard)
        presentation = build_presentation_plan(
            result,
            viewport_width=viewport_decision.viewport.width,
            viewport_height=viewport_decision.viewport.height,
            allow_camera_zoom=True,
            scene_plan=scenes,
        )
        artifacts.write_json(
            "presentation/presentation-plan.json", presentation.model_dump(mode="json")
        )
        scene_report = inspect_scene_plan(result, scenes)
        visual_state = json.loads((artifacts.presentation / "visual-state.json").read_text(encoding="utf-8"))
        visual_state_report = inspect_visual_state(visual_state)
        scene_report["visual_state"] = visual_state_report
        scene_report["hard_failures"].extend(visual_state_report["hard_failures"])
        journey = build_journey(result, scenes)
        journey_report = inspect_journey(journey)
        artifacts.write_json("presentation/scene-plan.json", scenes)
        artifacts.write_json("presentation/validated-scene-plan.json", journey)
        artifacts.write_json("quality/journey-report.json", journey_report)
        editorial_report = inspect_editorial(context=context, plan=plan, trace=result, storyboard=storyboard, script=script)
        editorial_report["hard_failures"].extend(journey_report["hard_failures"])
        scene_report["hard_failures"].extend(editorial_report["hard_failures"])
        artifacts.write_json("qa/editorial-report.json", {**editorial_report, "scene_plan": scene_report})
        if scene_report["hard_failures"]:
            raise RuntimeError(f"Editorial scene plan rejected: {scene_report['hard_failures']}")
        artifacts.write_json("presentation/cursor-plan.json", {"paths": presentation.cursor_paths})
        if stage_hook:
            stage_hook(RunStage.PRESENTATION_PLANNED)
        artifacts.write_json(
            "qa/execution-report.json",
            {"outcome_verified": True, "event_count": len(result.events)},
        )
        if render:
            narration = None
            captions = captions_from_duration(script, max(3.0, len(result.events) * 1.35))
            if self.speech_provider:
                try:
                    narration = await NarrationService().create(
                        result, self.speech_provider, artifacts.root / "audio" / "narration.mp3", script=script
                    )
                    script, captions = narration["script"], narration["captions"]
                    if stage_hook:
                        stage_hook(RunStage.NARRATION_READY)
                except ProviderError:
                    # Caption-only delivery remains valid when optional narration fails.
                    narration = None
            artifacts.write_json("presentation/captions.json", captions)
            artifacts.write_json(
                "presentation/narration-script.json",
                {"mode": "tts" if narration else "caption_only", "script": script},
            )
            artifacts.write_json(
                "narration/editorial-script.json",
                {"mode": "tts" if narration else "caption_only", "script": script},
            )
            if stage_hook:
                stage_hook(RunStage.RENDERING)
            output = render_remotion(
                result,
                presentation,
                artifacts,
                narration_path=Path(narration["audio_path"]) if narration else None,
                captions=captions,
                scenes=journey, storyboard=storyboard,
                target_duration_seconds=plan.target_duration_seconds,
                maximum_duration_seconds=plan.maximum_duration_seconds,
            )
            rendered_captions_path = artifacts.presentation / "rendered-captions.json"
            rendered_captions = (
                json.loads(rendered_captions_path.read_text(encoding="utf-8"))
                if rendered_captions_path.exists()
                else captions
            )
            video_report = inspect_video(
                output,
                execution_verified=result.outcome_verified,
                minimum_duration_seconds=self._duration_floor(plan),
                maximum_duration_seconds=plan.maximum_duration_seconds,
                source_video=(artifacts.root / "render" / "editorial-source.mp4")
                if (artifacts.root / "render" / "editorial-source.mp4").is_file()
                else artifacts.browser_video_path(),
            )
            presentation_report = inspect_presentation(result, json.loads((artifacts.presentation / "remotion-props.json").read_text(encoding="utf-8")))
            video_report = attach_presentation_qa(video_report, presentation_report)
            synchronization = inspect_synchronization(
                result,
                script,
                rendered_captions,
                narration_requested=False,
                narration_created=narration is not None,
            )
            artifacts.write_json("qa/video-report.json", video_report)
            artifacts.write_json("qa/presentation-report.json", presentation_report)
            artifacts.write_json("qa/synchronization-report.json", synchronization)
            artifacts.write_manifest()
            if stage_hook:
                stage_hook(RunStage.RENDERED)
                stage_hook(RunStage.VIDEO_QA)
            report = delivery_report(
                artifacts=artifacts.required_url_delivery_artifacts(),
                execution={"execution_score": 1.0},
                story=story,
                video=video_report,
                synchronization=synchronization,
                viewport_score=viewport_decision.score,
            )
            artifacts.write_json("qa/delivery-report.json", report)
            artifacts.write_json("qa/completion-audit.json", audit_run(artifacts.root))
            artifacts.write_manifest()
            if not report["deliverable"]:
                artifacts.write_json(
                    "qa/repair-decision.json", classify_repair(report["hard_failures"]).model_dump(mode="json")
                )
                raise RuntimeError(f"Delivery QA rejected render: {report['hard_failures']}")
            if stage_hook:
                stage_hook(RunStage.QA_PASSED)
        return result

    async def _stagehand_enrich(
        self,
        page,
        context,
        objective: str,
        *,
        environment: str = "LOCAL",
        browserbase_session_id: str | None = None,
    ):
        # Stagehand is an optional observation aid. Its output never becomes
        # workflow evidence until Playwright re-grounds it in this exact page.
        if self.stagehand_provider is None:
            return context, {
                "status": "UNAVAILABLE",
                "reason": "Browserbase-backed Stagehand is not configured",
                "candidate_count": 0,
                "candidates": [],
                "re_grounded_evidence": 0,
            }
        try:
            observation = await self.stagehand_provider.observe(
                url=page.url,
                instruction=(
                    "Observe only visible, safe, same-product navigation and primary controls "
                    f"that may help explain this objective: {objective}. Do not act, submit, or navigate."
                ),
                cache_dir=Path(".stagehand-cache"),
                environment=environment,
                browserbase_session_id=browserbase_session_id,
            )
        except ProviderError as error:
            return context, {
                "status": "UNAVAILABLE",
                "reason": str(error),
                "candidate_count": 0,
                "candidates": [],
                "re_grounded_evidence": 0,
            }
        enriched = await self.discovery.enrich_with_stagehand(page, context, observation)
        return enriched, {
            "status": "OBSERVED",
            "environment": observation.environment,
            "observed_url": observation.observed_url,
            # The Stagehand browser is intentionally independent of the
            # ProductLens discovery browser; this ID is correlation only.
            "correlated_browserbase_session_id": browserbase_session_id,
            "candidate_count": len(observation.candidates),
            "candidates": [
                {"selector": item.selector, "description": item.description, "method": item.method}
                for item in observation.candidates
            ],
            "metrics": observation.metrics,
            "re_grounded_evidence": len(enriched.elements) - len(context.elements),
        }
