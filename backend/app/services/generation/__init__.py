"""Production URL generation: explore, plan, execute, present, and quality-check."""

from __future__ import annotations

from app.services.generation_policy import (
    canonical_url as _canonical_url,
)
from app.services.generation_policy import safe_render_error

from .discover import _product_knowledge_payload, _relevance_graph
from .narrate import _narration_script_contract
from .rehearse import (
    _rehearsal_detail_navigation_witness,
    _rehearsal_outcome_candidates,
    _rehearsal_post_submit_state,
)
from .render import GenerationPreconditionError
from .service import UrlGenerationService

# Keep the legacy import surface stable for local tools and existing callers;
# render-stage code uses the canonical helper directly.
_safe_render_error = safe_render_error

__all__ = [
    "GenerationPreconditionError",
    "UrlGenerationService",
    "_canonical_url",
    "_narration_script_contract",
    "_product_knowledge_payload",
    "_rehearsal_detail_navigation_witness",
    "_rehearsal_outcome_candidates",
    "_rehearsal_post_submit_state",
    "_relevance_graph",
    "_safe_render_error",
]
