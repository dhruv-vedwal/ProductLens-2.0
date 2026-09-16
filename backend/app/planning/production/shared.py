"""Shared URL helpers and planning validation errors."""

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

def _canonical_url(value: str) -> str:
    """Normalize equivalent browser URL spellings for planning transitions."""
    return canonical_product_url(value)


def _route_key(value: str) -> tuple[str, str, str]:
    """Return the transport-independent route identity used for navigation.

    Query parameters commonly encode filters, pagination, or SPA state.  They
    must be preserved in evidence and postconditions, but they must not make a
    route look unobserved when the same visible control exposed its canonical
    path without those parameters.
    """
    parsed = urlsplit(_canonical_url(value))
    return parsed.scheme, parsed.netloc, parsed.path


def _semantic_words(value: str) -> set[str]:
    """Return meaningful words for conservative semantic target matching."""
    return {token for token in re.findall(r"[a-z0-9]+", (value or "").casefold()) if len(token) > 2}


class PlanningValidationError(ValueError):
    pass


SIDE_EFFECTING = {OperationKind.SUBMIT, OperationKind.CHECK, OperationKind.UNCHECK}
NO_POSTCONDITION_REQUIRED = {
    OperationKind.SCROLL_TO,
    OperationKind.READ_VALUE,
    # Keyboard text/commit gestures are verified by the following scene
    # witness (DOM/scene-graph or explicit state transition), not by a
    # brittle key-label visibility assertion.
    OperationKind.KEY_PRESS,
}

__all__ = [
    "_canonical_url",
    "_route_key",
    "_semantic_words",
    "PlanningValidationError",
    "SIDE_EFFECTING",
    "NO_POSTCONDITION_REQUIRED",
]
