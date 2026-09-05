import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from productlens.artifacts.store import RunArtifacts
from productlens.benchmark.fixtures import file_url
from productlens.contracts.models import (
    DemoTrace,
    ObservedElement,
    OperationKind,
    Postcondition,
    ProductContext,
    SemanticOperation,
    Target,
    WorkflowProposal,
)
from productlens.planning.production import PlanningValidationError, ProductionPlanningService
from productlens.providers.errors import ProviderError
from productlens.services.generation import UrlGenerationService


class EvidenceAwarePlanner:
    async def structured(self, prompt: str, schema):
        evidence = json.loads(prompt.split("Observed evidence: ", maxsplit=1)[1])
        users_route = next(route for route in evidence["routes"] if "section-users.html" in route)
        return WorkflowProposal(
            narrative_goal="Show how a teammate is invited",
            selected_workflow="Users",
            steps=[
                SemanticOperation(
                    kind=OperationKind.NAVIGATE,
                    intent="Open Users",
                    value=users_route,
                    postconditions=[Postcondition(kind="url", expected="**/section-users.html")],
                ),
                SemanticOperation(
                    kind=OperationKind.FILL_EMAIL,
                    intent="Enter teammate email",
                    target=Target(name="Invite by email", selector='[data-testid="invite-email"]'),
                    value="new.teammate@northwind.test",
                    postconditions=[
                        Postcondition(
                            kind="value",
                            expected="new.teammate@northwind.test",
                            target=Target(
                                name="Invite by email", selector='[data-testid="invite-email"]'
                            ),
                        )
                    ],
                ),
                SemanticOperation(
                    kind=OperationKind.SELECT_OPTION,
                    intent="Choose teammate role",
                    target=Target(name="Role", selector='[data-testid="invite-role"]'),
                    value="Member",
                    postconditions=[
                        Postcondition(
                            kind="value",
                            expected="Member",
                            target=Target(name="Role", selector='[data-testid="invite-role"]'),
                        )
                    ],
                ),
                SemanticOperation(
                    kind=OperationKind.SUBMIT,
                    intent="Send invitation",
                    target=Target(name="Send invite", selector='[data-testid="invite-btn"]'),
                    postconditions=[
                        Postcondition(
                            kind="test_state", expected="window.__testState.invited !== undefined"
                        )
                    ],
                ),
            ],
            expected_outcomes=["Invitation is sent"],
        )


class RejectingPlanner:
    async def structured(self, prompt: str, schema):
        raise ProviderError("test", 503, "fallback")


@pytest.mark.asyncio
async def test_direct_run_delegates_to_the_durable_stages(tmp_path: Path):
    """The compatibility API must not silently revive the old monolithic flow."""
    calls: list[str] = []

    class StagedOnlyService(UrlGenerationService):
        async def discover_stage(self, **kwargs):
            calls.append("discovery")

        async def plan_stage(self, **kwargs):
            calls.append("planning")

        async def execute_stage(self, **kwargs):
            calls.append("execution")
            return DemoTrace(run_id="delegated", objective="test", started_at=datetime.now(UTC), outcome_verified=True)

        async def narration_stage(self, **kwargs):
            calls.append("narration")
            return {"mode": "caption_only", "script": [], "audio_path": None}

        def render_stage(self, **kwargs):
            calls.append("render")

        def qa_stage(self, **kwargs):
            calls.append("qa")
            return {"deliverable": True}

    service = StagedOnlyService(ProductionPlanningService(EvidenceAwarePlanner()))
    trace = await service.run(
        run_id="delegated", url="https://example.test", objective="Show settings",
        artifact_root=tmp_path, render=True,
    )
    assert trace.outcome_verified
    assert calls == ["discovery", "planning", "execution", "narration", "render", "qa"]


@pytest.mark.asyncio
async def test_provider_fallback_rejects_context_without_page_local_evidence():
    context = ProductContext(
        url="https://example.test/",
        title="Study planner",
        application_type="dashboard",
        elements=[
            ObservedElement(tag="a", role="link", name="Today", selector="a", href="/"),
            ObservedElement(tag="a", role="link", name="Open Week 1", selector="a", href="/weeks/01"),
            ObservedElement(tag="a", role="link", name="Progress", selector="a", href="/progress"),
        ],
        relevant_routes=["https://example.test/weeks/01", "https://example.test/progress"],
    )
    with pytest.raises(PlanningValidationError, match="page lacks readable local evidence"):
        await ProductionPlanningService(RejectingPlanner()).plan(
            objective="Demonstrate the Today view, open one weekly study plan, and show how progress is reviewed.",
            context=context,
        )


@pytest.mark.asyncio
async def test_plan_stage_rejects_incomplete_persisted_discovery_without_a_browser(tmp_path: Path):
    artifacts = RunArtifacts(tmp_path, "plan-stage")
    artifacts.write_json(
        "discovery/product-context.json",
        ProductContext(
            url=file_url("gate5-discovery", "hub.html"),
            title="Demo hub",
            application_type="dashboard",
            authentication_state="authenticated",
            relevant_routes=[file_url("gate5-discovery", "section-users.html")],
        ).model_dump(mode="json"),
    )
    service = UrlGenerationService(ProductionPlanningService(EvidenceAwarePlanner()))
    with pytest.raises(PlanningValidationError, match="no evidence-grounded candidate flow"):
        await service.plan_stage(
            run_id="plan-stage",
            objective="Invite a new teammate",
            artifact_root=tmp_path,
            allow_external_side_effects=True,
            audience="admins",
            target_duration_seconds=90,
        )
    assert not (artifacts.root / "plan.json").exists()


@pytest.mark.integration
async def test_url_stages_execute_from_persisted_evidence_without_rendering(tmp_path: Path):
    service = UrlGenerationService(ProductionPlanningService(EvidenceAwarePlanner()))
    url = file_url("gate5-discovery", "hub.html")
    context = await service.discover_stage(
        run_id="staged-url", url=url, objective="Invite a new teammate", artifact_root=tmp_path,
        explore_visible_routes=True,
    )
    assert context.relevant_routes
    plan = await service.plan_stage(
        run_id="staged-url", objective="Invite a new teammate", artifact_root=tmp_path,
        allow_external_side_effects=True, audience="admins", target_duration_seconds=90,
    )
    assert plan.workflow_steps
    assert plan.selected_workflow == "Users", plan.risk_flags
    trace = await service.execute_stage(
        run_id="staged-url", url=url, objective="Invite a new teammate", artifact_root=tmp_path,
    )
    narration = await service.narration_stage(run_id="staged-url", artifact_root=tmp_path)
    assert trace.outcome_verified
    # Visible navigation replaces the prior duplicate direct route load.
    assert len(trace.events) == 4
    assert narration["mode"] == "caption_only"


@pytest.mark.integration
async def test_url_generation_rejects_a_short_render_that_cannot_satisfy_its_storyboard(tmp_path: Path):
    service = UrlGenerationService(ProductionPlanningService(EvidenceAwarePlanner()))

    # This integration case owns the delivery boundary, not Chromium's full
    # compositor.  A real Remotion encode of the 650-frame fixture makes the
    # suite depend on a local GPU/browser worker and has previously left CI
    # waiting after the product behaviour under test was already known.  The
    # focused render tests exercise the actual Remotion command.  Here we
    # deliberately supply a valid, short MP4 so QA must reject it against the
    # storyboard duration rather than accepting "an MP4 exists" as delivery.
    def render_intentionally_short_candidate(command, *, renderer, timeout_seconds):
        output = Path(next(argument for argument in command if str(argument).endswith(".mp4")))
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30",
                "-t", "1", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(output),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    from productlens.video import render as render_module
    original_segment_renderer = render_module._run_remotion_segment
    render_module._run_remotion_segment = render_intentionally_short_candidate
    with pytest.raises(RuntimeError, match="Delivery QA rejected render"):
        try:
            await service.run(
                run_id="url-run",
                url=file_url("gate5-discovery", "hub.html"),
                objective="Invite a new teammate",
                artifact_root=tmp_path,
                allow_external_side_effects=True,
                render=True,
            )
        finally:
            render_module._run_remotion_segment = original_segment_renderer
    # The verified visible navigation plus the three form interactions still
    # produce a trace, but a short fixture must never masquerade as a polished
    # final demo merely because it rendered an MP4.
    trace = DemoTrace.model_validate(
        json.loads((tmp_path / "runs" / "url-run" / "execution" / "trace.json").read_text())
    )
    assert trace.outcome_verified and len(trace.events) == 4
    assert (tmp_path / "runs" / "url-run" / "discovery" / "product-context.json").exists()
    assert (tmp_path / "runs" / "url-run" / "execution" / "trace.json").exists()
    final_video = tmp_path / "runs" / "url-run" / "final" / "demo.mp4"
    assert final_video.is_file() and final_video.stat().st_size >= 10_000
    delivery = json.loads((tmp_path / "runs" / "url-run" / "qa" / "delivery-report.json").read_text())
    assert not delivery["deliverable"]
    assert {"RENDER_TOO_SHORT", "EDITORIAL_DURATION_BELOW_STORYBOARD_MINIMUM"}.issubset(delivery["hard_failures"])
    narration = json.loads(
        (tmp_path / "runs" / "url-run" / "presentation" / "narration-script.json").read_text()
    )
    assert narration["mode"] == "caption_only"
    assert json.loads(
        (tmp_path / "runs" / "url-run" / "presentation" / "captions.json").read_text()
    )
