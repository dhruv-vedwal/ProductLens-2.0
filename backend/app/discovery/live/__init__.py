"""Bounded, DOM-first product discovery for supported web applications."""

from app.discovery.live.helpers import (  # noqa: F401
    _tokens,
    _canonical_route,
    _route_depth,
    adaptive_exploration_budget,
    _classify,
    _route_score,
    _objective_spec,
    _normalized_terms,
    _route_objective_score,
    _relationship_supporting_routes,
    _relationship_child_controls,
    _focused_relationship_evidence_complete,
    _relationship_page_roles,
    _derive_product_relationships,
    _REVERSIBLE_ACTION_WORDS,
    _UNSAFE_ACTION_WORDS,
    _capability_target,
    _page_knowledge,
    _restore_missing_page_landmarks,
    _bounded_page_navigation,
)
from app.discovery.live.service import LiveDiscovery  # noqa: F401
from app.discovery.live.page_capture import PageCaptureMixin  # noqa: F401
from app.discovery.live.probes import CapabilityProbeMixin  # noqa: F401
from app.discovery.live.orchestration import DiscoveryOrchestrationMixin  # noqa: F401

__all__ = [
    "_tokens",
    "_canonical_route",
    "_route_depth",
    "adaptive_exploration_budget",
    "_classify",
    "_route_score",
    "_objective_spec",
    "_normalized_terms",
    "_route_objective_score",
    "_relationship_supporting_routes",
    "_relationship_child_controls",
    "_focused_relationship_evidence_complete",
    "_relationship_page_roles",
    "_derive_product_relationships",
    "_REVERSIBLE_ACTION_WORDS",
    "_UNSAFE_ACTION_WORDS",
    "_capability_target",
    "_page_knowledge",
    "_restore_missing_page_landmarks",
    "_bounded_page_navigation",
    "LiveDiscovery",
    "PageCaptureMixin",
    "CapabilityProbeMixin",
    "DiscoveryOrchestrationMixin",
]
