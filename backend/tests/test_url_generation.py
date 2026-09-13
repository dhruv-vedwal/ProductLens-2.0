import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from productlens.artifacts.store import RunArtifacts
from productlens.benchmark.fixtures import file_url
from productlens.contracts.models import (
    ActionCapability,
    DemoTrace,
    FormField,
    FormSchema,
    ObjectiveSpec,
    ObservedElement,
    OperationKind,
    PageKnowledge,
    Postcondition,
    ProductContext,
    SemanticOperation,
    Target,
    WorkflowProposal,
)
from productlens.planning.capabilities import compile_rehearsal_operations
from productlens.planning.production import PlanningValidationError, ProductionPlanningService
from productlens.planning.synthetic import hydrate_operations
from productlens.providers.errors import ProviderError
from productlens.services.generation import UrlGenerationService


class EvidenceAwarePlanner:
    async def structured(self, prompt: str, schema):
        # The staged URL pipeline first asks the provider to normalize the
        # request into an ObjectiveSpec, then asks for a workflow.  Keep this
        # fixture provider schema-aware so integration coverage exercises the
        # real two-stage contract instead of depending on prompt ordering.
        if schema is ObjectiveSpec:
            return ObjectiveSpec(
                raw=prompt.rsplit("Request:", 1)[-1].strip(),
                primary_entity="teammate invitation",
                requested_features=["invite teammate"],
                must_show=["invite form", "invitation outcome"],
                permitted_mutations=["create_isolated_record"],
                safe_action_policy="authorized_mutations",
            )
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


class ObjectiveParsingProvider:
    async def structured(self, prompt: str, schema):
        assert "Interpret this demo request" in prompt
        return ObjectiveSpec(
            raw="different text", demo_type="workflow_demo", audience="sales engineers",
            depth="thorough", requested_features=["invoice", "reporting"],
            primary_entity="invoice workflow", must_show=["invoice", "invented claim"],
            exclusions=["billing"], success_criteria=["invoice outcome is visible"],
        )


class DefaultModeObjectiveProvider:
    async def structured(self, prompt: str, schema):
        return ObjectiveSpec(raw="ignored")


@pytest.mark.asyncio
async def test_model_default_cannot_downgrade_explicit_full_tour():
    service = UrlGenerationService(
        ProductionPlanningService(DefaultModeObjectiveProvider())
    )
    parsed, _ = await service._understand_objective(
        "Create a complete, evidence-grounded walkthrough of every safe primary section"
    )
    assert parsed.demo_type == "full_walkthrough"
    assert parsed.video_type == "full_tour"


@pytest.mark.asyncio
async def test_verified_rehearsal_is_reused_and_legacy_row_witness_is_migrated(tmp_path: Path):
    """A downstream retry must not submit a second record or retain row dumps."""
    run_id = "reused-rehearsal"
    source = "https://example.test/leads"
    capability = ActionCapability(
        kind="form", purpose="New lead", source_url=source,
        entry_target=Target(name="New lead", selector="#new-lead", source_url=source),
        form_schema=FormSchema(source_url=source, fields=[
            FormField(name="Phone", selector="#phone", control_type="phone", required=True),
        ]),
        submit_target=Target(name="Create lead", selector="#create", source_url=source),
    )
    _, dataset = hydrate_operations(compile_rehearsal_operations(capability), product_key=source)
    generated_phone = dataset["Phone"]
    capability = capability.model_copy(update={
        "verified": True,
        "outcome_target": Target(
            name=f"Riya Kapoor {generated_phone} Fresh Open",
            text=f"Riya Kapoor {generated_phone} Fresh Open", source_url=source,
        ),
        "outcome_evidence": [f"rehearsal-visible-outcome:Riya Kapoor {generated_phone}"],
    })
    context = ProductContext(
        url=source, title="Example", application_type="dashboard",
        objective=ObjectiveSpec(
            raw="Create an isolated lead-management record", primary_entity="lead management",
            permitted_mutations=["create_isolated_record"], safe_action_policy="authorized_side_effects",
        ),
        page_knowledge=[PageKnowledge(url=source, title="Leads", purpose="Lead management", fingerprint="leads")],
        capabilities=[capability.model_dump(mode="json")],
    )
    artifacts = RunArtifacts(tmp_path, run_id)
    artifacts.write_json("discovery/product-context.json", context.model_dump(mode="json"))
    artifacts.write_json("discovery/rehearsal-attempt.json", {
        "capability_id": capability.id, "status": "outcome_verified", "recording": "disabled",
    })
    # This stage returns before opening Playwright. A bare instance makes any
    # accidental browser/dependency use fail instead of creating a record.
    service = UrlGenerationService.__new__(UrlGenerationService)

    recovered = await service.rehearsal_stage(
        run_id=run_id, url=source, objective=context.objective.raw,
        artifact_root=tmp_path, cloud_rehearsal=True,
    )

    migrated = ActionCapability.model_validate(recovered.capabilities[0])
    assert migrated.outcome_target.name == "verified created record"
    assert migrated.outcome_target.text == generated_phone
    assert migrated.outcome_evidence == ["rehearsal-visible-outcome:verified-created-record"]
    report = json.loads((artifacts.root / "discovery" / "rehearsal-report.json").read_text())
    assert "Riya Kapoor" not in json.dumps(report)


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
async def test_objective_understanding_preserves_safe_deterministic_scope_and_rejects_ungrounded_terms():
    service = UrlGenerationService(ProductionPlanningService(ObjectiveParsingProvider()))
    parsed, report = await service._understand_objective(
        "Create a thorough walkthrough of the invoice workflow; exclude billing."
    )

    assert report["status"] == "model_grounded"
    assert parsed.raw == "Create a thorough walkthrough of the invoice workflow; exclude billing."
    # Thorough depth does not by itself broaden a feature request into a
    # whole-product tour; the provider may keep the narrower workflow mode.
    assert parsed.demo_type == "workflow_demo"
    assert parsed.depth == "thorough"
    assert parsed.primary_entity == "invoice workflow"
    assert "invoice" in parsed.must_show
    assert "invented claim" not in parsed.must_show
    assert "billing" in parsed.exclusions


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
    with pytest.raises(PlanningValidationError, match="page lacks readable local evidence"):
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
    # Visible navigation replaces the prior duplicate direct route load, and
    # a read-only local form inspection proves the destination was explored.
    assert len(trace.events) == 4
    assert trace.events[-1].kind is OperationKind.VERIFY_STATE
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
    assert (tmp_path / "runs" / "url-run" / "discovery" / "product-knowledge.json").exists()
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
