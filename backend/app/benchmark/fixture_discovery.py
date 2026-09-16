"""Bounded deterministic discovery used solely by local benchmark gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from app.contracts.models import DiscoveryBudget


@dataclass
class DiscoveryResult:
    selected_route: str
    excluded_routes: list[str]
    evidence: list[str] = field(default_factory=list)
    visited_routes: list[str] = field(default_factory=list)


class FixtureTargetedDiscovery:
    """Fixture navigation vocabulary; never imported by live URL discovery."""

    ROUTE_KEYWORDS: ClassVar[dict[str, tuple[str, list[str]]]] = {
        "invite": ("section-users.html", ["section-billing.html", "section-settings.html"]),
        "teammate": ("section-users.html", ["section-billing.html", "section-settings.html"]),
        "export": ("section-reports.html", ["section-billing.html", "section-settings.html"]),
        "report": ("section-reports.html", ["section-billing.html", "section-settings.html"]),
    }

    def select(self, objective: str, budget: DiscoveryBudget | None = None) -> DiscoveryResult:
        budget = budget or DiscoveryBudget()
        normalized = objective.lower()
        for keyword, (route, excluded) in self.ROUTE_KEYWORDS.items():
            if keyword in normalized:
                if budget.max_pages < 2:
                    raise ValueError("Discovery budget cannot establish a relevant route")
                return DiscoveryResult(
                    selected_route=route,
                    excluded_routes=excluded,
                    visited_routes=["hub.html", route],
                    evidence=[
                        f"objective contains {keyword!r}",
                        "relevant route selected",
                        "stop condition met",
                    ],
                )
        raise ValueError("No fixture route for objective; do not crawl by default")
