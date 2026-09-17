"""ProductionPlanningService composed from planning mixins."""

from __future__ import annotations

from app.planning.production.compile import RouteCompileMixin
from app.planning.production.core import PlanningCoreMixin
from app.planning.production.grounding import GroundingMixin
from app.planning.production.shared import *
from app.planning.production.validation import ValidationMixin


class ProductionPlanningService(
    PlanningCoreMixin,
    GroundingMixin,
    RouteCompileMixin,
    ValidationMixin,
):
    """Structured LLM planning with ProductLens-owned evidence and safety validation."""

__all__ = [
    "ProductionPlanningService",
]
