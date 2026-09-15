"""Evidence-backed state graph utilities for adaptive workflow planning.

The graph is intentionally product-neutral: callers provide observed state
fingerprints and verified transitions.  Search therefore cannot invent a route
that has not been observed, while still allowing the planner to choose a safer
or more relevant path when a UI changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from heapq import heappop, heappush


@dataclass(frozen=True)
class EvidenceEdge:
    source: str
    target: str
    action_id: str
    cost: float = 1.0
    verified: bool = True


@dataclass
class EvidenceGraph:
    """Directed graph of observed states and verified action transitions."""

    states: set[str] = field(default_factory=set)
    edges: list[EvidenceEdge] = field(default_factory=list)

    def add_state(self, fingerprint: str) -> None:
        if not fingerprint:
            raise ValueError("state fingerprint cannot be empty")
        self.states.add(fingerprint)

    def add_transition(self, edge: EvidenceEdge) -> None:
        if edge.source not in self.states or edge.target not in self.states:
            raise ValueError("transitions must reference observed states")
        if not edge.verified:
            return
        self.edges.append(edge)

    def shortest_verified_path(self, source: str, target: str) -> list[EvidenceEdge]:
        """Return the least-cost verified path using Dijkstra's algorithm."""
        if source not in self.states or target not in self.states:
            return []
        adjacency: dict[str, list[EvidenceEdge]] = {}
        for edge in self.edges:
            adjacency.setdefault(edge.source, []).append(edge)
        queue: list[tuple[float, str]] = [(0.0, source)]
        distances = {source: 0.0}
        previous: dict[str, EvidenceEdge] = {}
        while queue:
            distance, current = heappop(queue)
            if distance != distances.get(current):
                continue
            if current == target:
                break
            for edge in adjacency.get(current, []):
                next_distance = distance + max(0.001, edge.cost)
                if next_distance < distances.get(edge.target, float("inf")):
                    distances[edge.target] = next_distance
                    previous[edge.target] = edge
                    heappush(queue, (next_distance, edge.target))
        if target not in previous and source != target:
            return []
        result: list[EvidenceEdge] = []
        current = target
        while current != source:
            edge = previous[current]
            result.append(edge)
            current = edge.source
        return list(reversed(result))
