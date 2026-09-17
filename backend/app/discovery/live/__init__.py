"""Bounded, DOM-first product discovery for supported web applications."""

from app.discovery.live.helpers import (
    _REVERSIBLE_ACTION_WORDS,
    _UNSAFE_ACTION_WORDS,
    _bounded_page_navigation,
    _canonical_route,
    _capability_target,
    _classify,
    _derive_product_relationships,
    _focused_relationship_evidence_complete,
    _normalized_terms,
    _objective_spec,
    _page_knowledge,
    _relationship_child_controls,
    _relationship_page_roles,
    _relationship_supporting_routes,
    _restore_missing_page_landmarks,
    _route_depth,
    _route_objective_score,
    _route_score,
    _tokens,
    adaptive_exploration_budget,
)
from app.discovery.live.orchestration import DiscoveryOrchestrationMixin
from app.discovery.live.page_capture import PageCaptureMixin
from app.discovery.live.probes import CapabilityProbeMixin
from app.discovery.live.service import LiveDiscovery

__all__ = [
    "_REVERSIBLE_ACTION_WORDS",
    "_UNSAFE_ACTION_WORDS",
    "CapabilityProbeMixin",
    "DiscoveryOrchestrationMixin",
    "LiveDiscovery",
    "PageCaptureMixin",
    "_bounded_page_navigation",
    "_canonical_route",
    "_capability_target",
    "_classify",
    "_derive_product_relationships",
    "_focused_relationship_evidence_complete",
    "_normalized_terms",
    "_objective_spec",
    "_page_knowledge",
    "_relationship_child_controls",
    "_relationship_page_roles",
    "_relationship_supporting_routes",
    "_restore_missing_page_landmarks",
    "_route_depth",
    "_route_objective_score",
    "_route_score",
    "_tokens",
    "adaptive_exploration_budget",
]
