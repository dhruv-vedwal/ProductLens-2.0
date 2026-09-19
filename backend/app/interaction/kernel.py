"""Provider-neutral interaction lifecycle and safety boundary.

The kernel deliberately does not know about CRM records, diagram nodes, routes,
or product-specific selectors. Providers supply observations and browser
events; this module decides whether a candidate is grounded and whether the
workflow may advance after independent verification.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from app.contracts.models import (
    ActionAttempt,
    ActionCandidate,
    Affordance,
    CapabilityProfile,
    InteractionIntent,
    InteractionRecoveryDecision,
    InteractionSnapshot,
    InteractionTrace,
    OutcomeVerification,
    StateSnapshot,
)


@dataclass(frozen=True, slots=True)
class InteractionPolicy:
    """Bounds applied before a provider is permitted to dispatch an action."""

    minimum_confidence: float = 0.80
    allow_high_risk: bool = False
    max_recovery_decisions: int = 8

    def __post_init__(self) -> None:
        if not 0 <= self.minimum_confidence <= 1:
            raise ValueError("minimum_confidence must be between 0 and 1")
        if self.max_recovery_decisions < 0:
            raise ValueError("max_recovery_decisions cannot be negative")


@dataclass
class InteractionKernel:
    """Small deterministic state boundary shared by interaction providers."""

    run_id: str
    objective: str
    policy: InteractionPolicy = field(default_factory=InteractionPolicy)
    _intents: dict[str, InteractionIntent] = field(default_factory=dict)
    _snapshots: dict[str, StateSnapshot] = field(default_factory=dict)
    _observations: dict[str, InteractionSnapshot] = field(default_factory=dict)
    _candidates: dict[str, ActionCandidate] = field(default_factory=dict)
    _attempts: list[ActionAttempt] = field(default_factory=list)
    _verifications: list[OutcomeVerification] = field(default_factory=list)
    _recoveries: list[InteractionRecoveryDecision] = field(default_factory=list)
    _capability_profile: CapabilityProfile | None = None

    def register_intent(self, intent: InteractionIntent) -> None:
        if intent.id in self._intents:
            raise ValueError(f"duplicate interaction intent: {intent.id}")
        self._intents[intent.id] = intent

    def record_snapshot(self, snapshot: StateSnapshot) -> None:
        self._snapshots[snapshot.id] = snapshot

    def record_observation(self, observation: InteractionSnapshot) -> None:
        """Record a multimodal snapshot and expose its state identity."""
        self._observations[observation.id] = observation
        self.record_snapshot(
            StateSnapshot(
                id=observation.id,
                captured_at=observation.captured_at,
                url=observation.url,
                title=observation.title,
                screenshot_ref=observation.screenshot_ref,
                viewport=observation.viewport,
                scroll=observation.scroll,
                focused_target=observation.focused_target,
                loading=observation.loading,
                overlays=list(observation.overlays),
                evidence_refs=observation.evidence_refs,
            )
        )

    def propose(self, candidate: ActionCandidate) -> None:
        """Register a grounded candidate without dispatching it."""
        if candidate.intent_id not in self._intents:
            raise ValueError("candidate references an unknown interaction intent")
        if candidate.source_snapshot_id not in self._snapshots:
            raise ValueError("candidate must reference a recorded source snapshot")
        if candidate.action.id in {item.action.id for item in self._candidates.values()}:
            raise ValueError(f"duplicate action candidate: {candidate.action.id}")
        self._candidates[candidate.id] = candidate

    def select(self, candidates: Iterable[ActionCandidate]) -> ActionCandidate:
        """Select a safe, grounded candidate; dispatch remains provider-owned."""
        values = list(candidates)
        for candidate in values:
            if candidate.id not in self._candidates:
                self.propose(candidate)
        eligible = [
            candidate
            for candidate in values
            if candidate.confidence >= self.policy.minimum_confidence
            and candidate.safety_verified
            and candidate.action.gesture != "observe"
            and not (
                candidate.action.side_effect_policy == "authorized_mutation"
                and not self.policy.allow_high_risk
                and candidate.action.gesture == "submit"
                and candidate.rehearsal_required
            )
        ]
        if not eligible:
            raise ValueError("no safe, sufficiently grounded action candidate")
        return max(eligible, key=lambda item: (item.confidence, item.id))

    def record_attempt(self, attempt: ActionAttempt) -> None:
        """Persist an attempt and enforce the no-replay-after-dispatch rule."""
        if attempt.retry_of and attempt.retry_of not in {item.id for item in self._attempts}:
            raise ValueError("retry_of must reference an earlier action attempt")
        if attempt.retry_of:
            parent = next(item for item in self._attempts if item.id == attempt.retry_of)
            if parent.dispatched:
                raise ValueError("a dispatched side effect cannot be replayed as a retry")
        self._attempts.append(attempt)

    def record_verification(self, verification: OutcomeVerification) -> None:
        self._verifications.append(verification)

    def record_recovery(self, recovery: InteractionRecoveryDecision) -> None:
        if len(self._recoveries) >= self.policy.max_recovery_decisions:
            raise ValueError("interaction recovery budget exhausted")
        if recovery.failed_attempt_id not in {item.id for item in self._attempts}:
            raise ValueError("recovery must reference a recorded action attempt")
        self._recoveries.append(recovery)

    def set_capability_profile(self, profile: CapabilityProfile) -> None:
        self._capability_profile = profile

    def trace(self, *, complete: bool = False) -> InteractionTrace:
        """Return a serialisable trace; completion requires passed verification."""
        return InteractionTrace(
            run_id=self.run_id,
            objective=self.objective,
            snapshots=list(self._snapshots.values()),
            observations=list(self._observations.values()),
            attempts=list(self._attempts),
            verifications=list(self._verifications),
            recoveries=list(self._recoveries),
            capability_profile=self._capability_profile,
            complete=complete,
        )


def affordances_for_snapshot(
    affordances: Iterable[Affordance], snapshot_id: str
) -> list[Affordance]:
    """Keep only visible, enabled affordances grounded in a snapshot.

    The kernel never invents targets. An empty result is a deliberate blocker
    that callers must resolve with a fresh observation or a safe stop.
    """

    prefix = f"snapshot:{snapshot_id}:"
    return [
        item
        for item in affordances
        if item.visible
        and item.enabled
        and any(
            reference == snapshot_id or reference.startswith(prefix)
            for reference in item.evidence_refs
        )
    ]
