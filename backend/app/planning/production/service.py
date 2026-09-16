"""ProductionPlanningService composed from planning mixins."""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse, urlsplit

from pydantic import ValidationError

from app.contracts.models import (
    ActionCapability,
    DemoPlan,
    ObservedElement,
    OperationKind,
    Postcondition,
    ProductContext,
    SemanticOperation,
    Target,
    WorkflowProposal,
    WorkflowStep,
)
from app.observability.logging import redact_prompt_text
from app.planning.candidates import (
    build_page_complete_proposal,
    navigation_control_for_transition,
    select_candidate_flow,
    validate_flow_scope,
)
from app.planning.capabilities import (
    CapabilityCompilationError,
    compile_read_only_form_inspection,
    compile_record_creation,
)
from app.planning.capability_resolution import resolve_capabilities
from app.planning.rehearsal import CapabilitySelectionError, select_rehearsal_capability
from app.planning.side_effects import (
    SideEffectPolicyError,
    authorize_operation,
    side_effect_decision,
)
from app.planning.synthetic import hydrate_operations
from app.providers.errors import ProviderError
from app.providers.interfaces import LLMProvider
from app.urls import canonical_product_url

from app.planning.production.shared import *  # noqa: F403
from app.planning.production.core import PlanningCoreMixin
from app.planning.production.grounding import GroundingMixin
from app.planning.production.compile import RouteCompileMixin
from app.planning.production.validation import ValidationMixin

class ProductionPlanningService(
    PlanningCoreMixin,
    GroundingMixin,
    RouteCompileMixin,
    ValidationMixin,
):
    """Structured LLM planning with ProductLens-owned evidence and safety validation."""
    pass

__all__ = [
    "ProductionPlanningService",
]
