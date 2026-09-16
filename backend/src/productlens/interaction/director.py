"""Generic interaction direction between perception and browser execution.

The director is deliberately product-neutral.  It turns an observed affordance
into a candidate only when the affordance is visible, enabled, and tied to a
recorded snapshot.  The kernel remains the only component allowed to select a
candidate for dispatch; this facade is useful to exploration, rehearsal, and
future multimodal providers without creating a second execution path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from productlens.contracts.models import (
    ActionCandidate,
    ActionIntent,
    Affordance,
    InteractionIntent,
    InteractionSnapshot,
    Postcondition,
    Target,
)
from productlens.interaction.kernel import InteractionKernel


@dataclass(frozen=True, slots=True)
class CandidateScore:
    """Auditable candidate ranking values; no model claim is hidden here."""

    candidate_id: str
    confidence: float
    evidence_strength: float
    visual_value: float
    safety: float

    @property
    def total(self) -> float:
        return (
            self.confidence * 0.45
            + self.evidence_strength * 0.25
            + self.visual_value * 0.10
            + self.safety * 0.20
        )


@dataclass
class InteractionDirector:
    """Translate current UI evidence into safe, ranked interaction candidates."""

    kernel: InteractionKernel

    def register_objective(self, intent: InteractionIntent) -> None:
        self.kernel.register_intent(intent)

    def observe(self, snapshot: InteractionSnapshot) -> list[Affordance]:
        """Persist one observation and return only visible enabled controls."""
        self.kernel.record_observation(snapshot)
        return [item for item in snapshot.visible_affordances if item.visible and item.enabled]

    async def ground_provider_candidate(
        self,
        *,
        page: Any,
        snapshot: InteractionSnapshot,
        provider_candidate: Any,
    ) -> Affordance | None:
        """Re-ground a semantic-provider suggestion against the live DOM.

        A multimodal provider may return a selector-like hint, but the hint is
        not a target until the current Playwright page proves it resolves to
        exactly one visible, enabled element and supplies fresh geometry.
        Unsupported methods are ignored rather than translated into guesses.
        """
        selector = str(getattr(provider_candidate, "selector", "") or "").strip()
        label = str(
            getattr(provider_candidate, "description", "")
            or getattr(provider_candidate, "label", "")
            or ""
        ).strip()
        method = str(getattr(provider_candidate, "method", "") or "").casefold()
        method = {"key": "keypress"}.get(method, method)
        if (
            not selector
            or not label
            or method
            not in {
                "click",
                "type",
                "keypress",
                "hover",
                "drag",
                "select",
                "upload",
            }
        ):
            return None
        try:
            locator = page.locator(selector)
            if await locator.count() != 1 or not await locator.is_visible():
                return None
            if hasattr(locator, "is_enabled") and not await locator.is_enabled():
                return None
            details = await locator.evaluate(
                """element => {
                    const box = element.getBoundingClientRect();
                    return {
                      role: element.getAttribute('role') || element.tagName.toLowerCase(),
                      label: element.getAttribute('aria-label') || element.innerText ||
                        element.getAttribute('placeholder') || element.getAttribute('name') || '',
                      geometry: {x: box.x, y: box.y, width: box.width, height: box.height},
                      disabled: Boolean(element.disabled),
                    };
                }"""
            )
        except Exception:  # noqa: BLE001 - a stale hint is not a run failure
            return None
        if not isinstance(details, dict) or details.get("disabled"):
            return None
        role = str(details.get("role") or "")[:80] or None
        return Affordance(
            label=label[:240],
            role=role,
            method=method,
            target=Target(
                name=label[:240],
                role=role,
                label=str(details.get("label") or label)[:240],
                selector=selector,
                source_url=snapshot.url,
            ),
            geometry=(details.get("geometry") if isinstance(details.get("geometry"), dict) else {}),
            evidence_refs=[f"snapshot:{snapshot.id}", "provider:semantic-grounded"],
            confidence=0.9,
        )

    def candidate_for_affordance(
        self,
        *,
        intent_id: str,
        snapshot: InteractionSnapshot,
        affordance: Affordance,
        expected_outcomes: list[Postcondition] | None = None,
        preconditions: list[Postcondition] | None = None,
        value: Any = None,
        destination: Target | None = None,
        safety_verified: bool | None = None,
        rehearsal_required: bool = True,
    ) -> ActionCandidate:
        """Build a semantic candidate without inventing a selector or route."""
        if snapshot.id not in {item.id for item in self.kernel.trace().observations}:
            raise ValueError("candidate snapshot must be observed by the kernel")
        if not affordance.visible or not affordance.enabled:
            raise ValueError("candidate affordance must be visible and enabled")
        target = affordance.target or Target(
            name=affordance.label,
            role=affordance.role,
            label=affordance.label,
        )
        gesture = {
            "keypress": "key",
        }.get(affordance.method, affordance.method)
        expected = list(expected_outcomes or [])
        policy = "authorized_mutation" if affordance.risk == "high" else "read_only"
        action = ActionIntent(
            goal=f"Use the visible {affordance.label} control",
            gesture=gesture,
            target=target,
            destination=destination,
            value=value,
            expected_state=expected,
            evidence_refs=list(affordance.evidence_refs) or [f"snapshot:{snapshot.id}"],
            side_effect_policy=policy,
        )
        candidate = ActionCandidate(
            intent_id=intent_id,
            action=action,
            affordance_id=affordance.id,
            source_snapshot_id=snapshot.id,
            preconditions=list(preconditions or []),
            expected_outcomes=expected,
            confidence=max(0.0, min(1.0, affordance.confidence)),
            safety_verified=(
                affordance.risk != "high" if safety_verified is None else safety_verified
            ),
            rehearsal_required=rehearsal_required,
        )
        self.kernel.propose(candidate)
        return candidate

    def rank(
        self, candidates: list[ActionCandidate]
    ) -> list[tuple[ActionCandidate, CandidateScore]]:
        """Rank candidates using explainable evidence and safety signals."""
        ranked: list[tuple[ActionCandidate, CandidateScore]] = []
        for candidate in candidates:
            affordance_evidence = 1.0 if candidate.affordance_id else 0.5
            visual_value = 0.0
            for observation in self.kernel.trace().observations:
                if observation.id != candidate.source_snapshot_id:
                    continue
                item = next(
                    (
                        value
                        for value in observation.visible_affordances
                        if value.id == candidate.affordance_id
                    ),
                    None,
                )
                if item:
                    geometry = item.geometry
                    visual_value = min(
                        1.0,
                        max(
                            0.0,
                            float(geometry.get("width", 0))
                            * float(geometry.get("height", 0))
                            / 120_000,
                        ),
                    )
                break
            score = CandidateScore(
                candidate_id=candidate.id,
                confidence=candidate.confidence,
                evidence_strength=affordance_evidence,
                visual_value=visual_value,
                safety=1.0 if candidate.safety_verified else 0.0,
            )
            ranked.append((candidate, score))
        return sorted(ranked, key=lambda item: (item[1].total, item[0].id), reverse=True)

    def select(self, candidates: list[ActionCandidate]) -> ActionCandidate:
        """Select through the kernel's confidence and safety policy."""
        return self.kernel.select([candidate for candidate, _ in self.rank(candidates)])
