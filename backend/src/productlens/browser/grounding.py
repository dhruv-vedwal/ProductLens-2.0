from __future__ import annotations

from dataclasses import dataclass

from productlens.contracts.models import Target


@dataclass(frozen=True)
class TargetCandidate:
    selector: str
    evidence: str
    confidence: float


class SelectorCache:
    def __init__(self):
        self._entries: dict[tuple[str, str], TargetCandidate] = {}

    def put(self, page_key: str, intent: str, candidate: TargetCandidate) -> None:
        self._entries[(page_key, intent)] = candidate

    def get(self, page_key: str, intent: str) -> TargetCandidate | None:
        return self._entries.get((page_key, intent))


def choose_candidate(target: Target, candidates: list[TargetCandidate]) -> TargetCandidate:
    viable = [
        candidate for candidate in candidates if candidate.confidence >= target.confidence_required
    ]
    if not viable:
        raise LookupError(f"No target grounding met confidence for {target.name}")
    return max(viable, key=lambda candidate: candidate.confidence)
