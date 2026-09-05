"""Planning adapter for deterministic benchmark fixtures only."""

from __future__ import annotations

from productlens.benchmark.fixture_discovery import FixtureTargetedDiscovery
from productlens.contracts.models import DemoPlan, OperationKind, SemanticOperation, WorkflowStep


class FixturePlanningService:
    """Compile a known fixture objective without entering the live planner."""

    def plan(self, objective: str) -> DemoPlan:
        try:
            discovery = FixtureTargetedDiscovery().select(objective)
            target = discovery.selected_route
            excluded = discovery.excluded_routes
        except ValueError:
            # Some primitive gates validate execution rather than discovery.
            # Keep their fixture plan explicitly local and non-semantic.
            target, excluded = "fixture application", []
        operation = SemanticOperation(
            kind=OperationKind.NAVIGATE,
            intent=f"Open benchmark fixture area: {target}",
            value=target,
        )
        step = WorkflowStep(id="fixture-discover-relevant-workflow", intent=operation.intent, operation=operation)
        return DemoPlan(
            objective=objective,
            narrative_goal=objective,
            audience="benchmark evaluator",
            target_duration_seconds=45,
            selected_workflow=target,
            workflow_steps=[step],
            expected_outcomes=[objective],
            important_elements=[],
            excluded_areas=excluded,
            viewport_strategy="fixture primitive validation",
            risk_flags=["fixture-only deterministic planning"],
            stop_conditions=["fixture route evidence established"],
        )
