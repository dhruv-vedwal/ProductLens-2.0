from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

from app.artifacts.store import RunArtifacts
from app.contracts.models import DemoPlan, DemoTrace, DiscoveryBudget
from app.credentials.service import EnvironmentCredentialService
from app.discovery.live import LiveDiscovery
from app.narration.service import SpeechProvider
from app.observability.logging import get_logger
from app.orchestration.lifecycle import RunStage
from app.planning.production import ProductionPlanningService
from app.providers.browserbase import BrowserbaseProvider
from app.providers.stagehand import StagehandProvider
from app.quality.multimodal import VisualReviewer

from .discover import DiscoverMixin
from .execute import ExecuteMixin
from .narrate import NarrateMixin
from .plan import PlanMixin
from .qa import QaMixin
from .rehearse import RehearseMixin
from .render import GenerationPreconditionError, RenderMixin

logger = get_logger("app.generation")


class UrlGenerationService(
    DiscoverMixin,
    PlanMixin,
    RehearseMixin,
    ExecuteMixin,
    NarrateMixin,
    RenderMixin,
    QaMixin,
):
    def __init__(
        self,
        planner: ProductionPlanningService,
        speech_provider: SpeechProvider | None = None,
        browserbase_provider: BrowserbaseProvider | None = None,
        stagehand_provider: StagehandProvider | None = None,
        credential_service: EnvironmentCredentialService | None = None,
        visual_reviewer: VisualReviewer | None = None,
        cloud_capture_timeout_seconds: int = 840,
        stagehand_observe_timeout_seconds: float = 105.0,
    ):
        self.planner = planner
        self.speech_provider = speech_provider
        self.browserbase_provider = browserbase_provider
        self.stagehand_provider = stagehand_provider
        self.credential_service = credential_service or EnvironmentCredentialService()
        self.visual_reviewer = visual_reviewer
        self.cloud_capture_timeout_seconds = max(30, cloud_capture_timeout_seconds)
        self.stagehand_observe_timeout_seconds = max(
            30.0, min(180.0, float(stagehand_observe_timeout_seconds))
        )
        self.discovery = LiveDiscovery()

    async def _release_cloud_session(self, session_id: str, *, reason: str, run_id: str) -> None:
        """Release a Browserbase lease without letting cleanup hide its cause."""
        if self.browserbase_provider is None:
            return
        try:
            await asyncio.wait_for(
                self.browserbase_provider.close_session(session_id),
                timeout=15,
            )
        except Exception as error:  # noqa: BLE001 - cleanup is best-effort and idempotent
            logger.warning(
                "browserbase_session_release_failed",
                run_id=run_id,
                session_id=session_id,
                reason=reason,
                error_type=type(error).__name__,
            )

    async def _release_cloud_session_after(
        self, session_id: str, *, seconds: int, reason: str, run_id: str
    ) -> None:
        """Out-of-band lease guard for a CDP operation that ignores cancellation."""
        try:
            await asyncio.sleep(seconds)
            logger.warning(
                "browserbase_session_lease_guard_fired",
                run_id=run_id,
                session_id=session_id,
                reason=reason,
            )
            await self._release_cloud_session(session_id, reason=reason, run_id=run_id)
        except asyncio.CancelledError:
            # Successful/disposed stage lifecycle: normal path cancels guard.
            return

    @staticmethod
    def _load_trace(artifacts: RunArtifacts) -> DemoTrace:
        return DemoTrace.model_validate(
            json.loads((artifacts.execution / "trace.json").read_text(encoding="utf-8"))
        )

    @staticmethod
    def _load_plan(artifacts: RunArtifacts) -> DemoPlan:
        """Load the effective plan after any evidence-grounded suffix replan."""
        path = artifacts.root / "plan-effective.json"
        if not path.is_file():
            path = artifacts.root / "plan.json"
        return DemoPlan.model_validate(json.loads(path.read_text(encoding="utf-8")))

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
            run_id=run_id,
            url=url,
            objective=objective,
            artifact_root=artifact_root,
            budget=budget,
            cloud_discovery=cloud_discovery,
            known_routes=known_routes,
            known_actions=known_actions,
            explore_visible_routes=explore_visible_routes,
            stagehand_assist=stagehand_assist,
            credential_reference=credential_reference,
        )
        if stage_hook:
            stage_hook(RunStage.DISCOVERING)
        await self.plan_stage(
            run_id=run_id,
            objective=objective,
            artifact_root=artifact_root,
            allow_external_side_effects=allow_external_side_effects,
            audience=audience,
            target_duration_seconds=target_duration_seconds,
        )
        if stage_hook:
            stage_hook(RunStage.PLAN_VALIDATED)
        trace = await self.execute_stage(
            run_id=run_id,
            url=url,
            objective=objective,
            artifact_root=artifact_root,
            credential_reference=credential_reference,
            cloud_production=cloud_discovery,
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

