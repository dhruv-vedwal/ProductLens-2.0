from __future__ import annotations

import json
import re
from pathlib import Path

from app.artifacts.store import RunArtifacts
from app.contracts.models import (
    BehavioralProductModel,
    CertifiedWorkflowGraph,
    DemoPlan,
    ProductContext,
)
from app.discovery.live import (
    _objective_spec,
)
from app.planning.brief import build_demo_brief
from app.planning.state_machine import WorkflowStateMachine
from app.presentation.editorial import (
    build_editorial_storyboard,
    enrich_editorial_brief,
    enrich_editorial_storyboard,
)
from app.quality.editorial import inspect_editorial_preflight


class PlanMixin:
    async def plan_stage(
        self,
        *,
        run_id: str,
        objective: str,
        artifact_root: Path,
        allow_external_side_effects: bool,
        audience: str,
        target_duration_seconds: int,
        trace_only: bool = False,
    ) -> DemoPlan:
        """Plan from persisted discovery evidence; no browser or provider session is reused."""
        artifacts = RunArtifacts(artifact_root, run_id)
        context = ProductContext.model_validate(
            json.loads(
                (artifacts.root / "discovery" / "product-context.json").read_text(encoding="utf-8")
            )
        )
        behavioral_model = None
        behavioral_path = artifacts.root / (
            context.behavioral_model_ref or "discovery/behavioral-product-model.json"
        )
        if behavioral_path.is_file():
            behavioral_model = BehavioralProductModel.model_validate_json(
                behavioral_path.read_text(encoding="utf-8")
            )
            context = context.model_copy(
                update={
                    "behavioral_model_ref": str(
                        behavioral_path.relative_to(artifacts.root)
                    ).replace("\\", "/"),
                    "behavioral_uncertainties": behavioral_model.unresolved_uncertainties,
                }
            )
        elif context.behavioral_model_ref:
            raise ValueError("behavioral product model is required by the discovery contract")
        # Reconcile the persisted model interpretation with the deterministic
        # request parser at the production boundary. Older discovery runs may
        # have upgraded a focused "thorough" request to full_walkthrough;
        # resuming them must not reintroduce the broad route crawl that the
        # current objective explicitly excludes.
        deterministic_objective = _objective_spec(objective)
        # Safety permissions are part of the user's objective contract, not a
        # model preference.  An objective that explicitly authorizes one
        # isolated synthetic record must retain that permission even when an
        # older discovery artifact (or a conservative model response) stored
        # ``read_only``.  Reconcile only the deterministic, request-derived
        # mutation fields; never broaden permissions beyond the request.
        if (
            context.objective is not None
            and deterministic_objective.permitted_mutations
            and (
                context.objective.permitted_mutations
                != deterministic_objective.permitted_mutations
                or context.objective.safe_action_policy
                != deterministic_objective.safe_action_policy
                or context.objective.safe_actions_only
                != deterministic_objective.safe_actions_only
            )
        ):
            context = context.model_copy(
                update={
                    "objective": context.objective.model_copy(
                        update={
                            "safe_action_policy": deterministic_objective.safe_action_policy,
                            "permitted_mutations": deterministic_objective.permitted_mutations,
                            "safe_actions_only": deterministic_objective.safe_actions_only,
                        }
                    )
                }
            )
        if (
            context.objective is not None
            and deterministic_objective.demo_type != "full_walkthrough"
            and context.objective.demo_type == "full_walkthrough"
        ):
            context = context.model_copy(
                update={
                    "objective": context.objective.model_copy(
                        update={
                            "demo_type": deterministic_objective.demo_type,
                            "minimum_duration_seconds": deterministic_objective.minimum_duration_seconds,
                            "target_duration_seconds": deterministic_objective.target_duration_seconds,
                            "maximum_duration_seconds": deterministic_objective.maximum_duration_seconds,
                            "depth": deterministic_objective.depth,
                        }
                    )
                }
            )
        # The model may echo an entire noun phrase (for example, "the
        # authenticated booking workflow") into ``primary_entity``.  Feature
        # grounding is intentionally token/entity based, so reconcile the
        # persisted interpretation with the deterministic request parser for
        # focused objectives.  This prevents provider phrasing from turning a
        # valid discovered feature into an apparently unsupported entity.
        if (
            context.objective is not None
            and deterministic_objective.demo_type != "full_walkthrough"
            and deterministic_objective.primary_entity
            and (
                context.objective.primary_entity != deterministic_objective.primary_entity
                or context.objective.must_show != deterministic_objective.must_show
            )
        ):
            context = context.model_copy(
                update={
                    "objective": context.objective.model_copy(
                        update={
                            "primary_entity": deterministic_objective.primary_entity,
                            "must_show": deterministic_objective.must_show,
                        }
                    )
                }
            )
        if (
            context.objective is not None
            and deterministic_objective.demo_type != "full_walkthrough"
            and deterministic_objective.supporting_relationships
        ):
            # Relationship/context language is part of the request contract,
            # not optional model decoration.  Restore explicit deterministic
            # relationships when resuming discovery written by an older model
            # pass that omitted them.
            context = context.model_copy(
                update={
                    "objective": context.objective.model_copy(
                        update={
                            "supporting_relationships": deterministic_objective.supporting_relationships,
                        }
                    )
                }
            )
        # Discovery artifacts may have been produced by an older objective
        # writer that treated prose pairs (for example, ``Home identity``) as
        # relationships.  Re-ground the relationship list at the planning
        # boundary as well, so editorial preflight cannot require a setup page
        # for a dependency the user never explicitly requested.
        if context.objective is not None and context.objective.supporting_relationships:
            raw_objective = context.objective.raw.casefold()
            explicit_relationships = []
            connector = r"(?:->|→|configures|explains|supports|in the context of|configured by|with context from|using)"
            for relation in context.objective.supporting_relationships:
                source_phrase = " ".join(re.findall(r"[a-z0-9]{3,}", relation.source.casefold()))
                target_phrase = " ".join(re.findall(r"[a-z0-9]{3,}", relation.target.casefold()))
                relation_words = set(
                    re.findall(
                        r"[a-z0-9]{3,}",
                        f"{relation.source} {relation.target}".casefold(),
                    )
                )
                if relation_words & {"it", "them", "this", "that", "these", "those"}:
                    continue
                if (
                    source_phrase
                    and target_phrase
                    and (
                        re.search(
                            rf"{re.escape(source_phrase)}\s*{connector}\s*{re.escape(target_phrase)}",
                            raw_objective,
                        )
                        or re.search(
                            rf"{re.escape(target_phrase)}\s*{connector}\s*{re.escape(source_phrase)}",
                            raw_objective,
                        )
                    )
                ):
                    explicit_relationships.append(relation)
            context = context.model_copy(
                update={
                    "objective": context.objective.model_copy(
                        update={
                            "supporting_relationships": explicit_relationships,
                        }
                    )
                }
            )
        # Persist the human-reviewable story boundary before compiling browser
        # operations.  This proves production was selected from discovery
        # knowledge rather than from a recording-time route sweep.
        demo_brief = build_demo_brief(
            context,
            objective=objective,
            audience=audience,
            duration_seconds=target_duration_seconds,
        )
        artifacts.write_json("planning/demo-brief.json", demo_brief.model_dump(mode="json"))
        plan = await self.planner.plan(
            objective=objective,
            context=context,
            allow_external_side_effects=allow_external_side_effects,
            audience=audience,
            target_duration_seconds=target_duration_seconds,
        )
        workflow_graph_path = artifacts.root / "planning" / "certified-workflow-graph.json"
        if (
            context.objective is not None
            and "create_isolated_record" in context.objective.permitted_mutations
        ):
            if not workflow_graph_path.is_file():
                raise ValueError(
                    "authorized creation objective requires a certified hidden-rehearsal workflow"
                )
            workflow_graph = CertifiedWorkflowGraph.model_validate_json(
                workflow_graph_path.read_text(encoding="utf-8")
            )
            plan = plan.model_copy(update={"certified_workflow": workflow_graph})
        # Keep the runtime capability decision as an inspectable planning
        # artifact.  It is evidence-backed and advisory; execution still
        # re-grounds each selected target before dispatch.
        artifacts.write_json(
            "planning/capability-resolutions.json",
            [item.model_dump(mode="json") for item in context.capability_resolutions],
        )
        artifacts.write_json("discovery/product-context.json", context.model_dump(mode="json"))
        artifacts.write_json("plan.json", plan.model_dump(mode="json"))
        if plan.certified_script is not None:
            artifacts.write_json(
                "planning/certified-demo-script.json",
                plan.certified_script.model_dump(mode="json"),
            )
        artifacts.write_json(
            "planning/validated-state-graph.json",
            WorkflowStateMachine.from_operations(
                [step.operation for step in plan.workflow_steps]
            ).artifact(),
        )
        if trace_only:
            # Capability/certification runs prove browser behavior only. Do
            # not spend provider calls on editorial prose or reject a valid
            # interaction because a future narration brief is still being
            # refined; the verified trace is the sole promotion boundary.
            artifacts.write_json(
                "planning/trace-only.json",
                {
                    "mode": "trace_only",
                    "presentation_stages": "not_started",
                    "trace_consumers": ["verification", "scene_segmentation"],
                },
            )
            return plan
        storyboard = build_editorial_storyboard(context, plan)
        storyboard = await enrich_editorial_brief(context, storyboard, self.planner.provider)
        storyboard = await enrich_editorial_storyboard(
            context,
            storyboard,
            self.planner.provider,
            {step.operation.id: step.operation for step in plan.workflow_steps},
        )
        artifacts.write_json(
            "presentation/editorial-brief.json", storyboard.brief.model_dump(mode="json")
        )
        # Keep the extraction/editorial boundary inspectable. The brief's facts
        # are the approved, evidence-cited claims; narration is generated only
        # after this immutable fact set exists and is persisted for repairs.
        artifacts.write_json(
            "narration/fact-extraction.json",
            {
                "schema_version": 1,
                "source": "page-knowledge-and-editorial-brief",
                "facts": [item.model_dump(mode="json") for item in storyboard.brief.facts],
                "excluded_areas": storyboard.brief.excluded_areas,
            },
        )
        artifacts.write_json("presentation/storyboard.json", storyboard.model_dump(mode="json"))
        editorial_preflight = inspect_editorial_preflight(
            context=context, plan=plan, storyboard=storyboard
        )
        artifacts.write_json("qa/editorial-preflight.json", editorial_preflight)
        if editorial_preflight["hard_failures"]:
            raise RuntimeError(
                f"Editorial preflight rejected plan: {editorial_preflight['hard_failures']}"
            )
        return plan

