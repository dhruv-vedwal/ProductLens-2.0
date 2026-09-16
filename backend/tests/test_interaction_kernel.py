import pytest

from productlens.contracts.models import (
    ActionAttempt,
    ActionCandidate,
    ActionIntent,
    Affordance,
    InteractionIntent,
    OutcomeVerification,
    StateSnapshot,
    Target,
)
from productlens.interaction.kernel import InteractionKernel, affordances_for_snapshot


def _intent(*, safety: str = "read_only") -> InteractionIntent:
    return InteractionIntent(
        objective="Open the primary feature",
        audience_value="Show the feature entry point",
        safety_policy=safety,
    )


def _candidate(intent: InteractionIntent, snapshot: StateSnapshot, *, safe: bool = True):
    action = ActionIntent(
        goal="Open the primary feature",
        gesture="click",
        target=Target(name="Primary feature", role="button"),
        side_effect_policy=intent.safety_policy,
        expected_state=[],
    )
    return ActionCandidate(
        intent_id=intent.id,
        action=action,
        source_snapshot_id=snapshot.id,
        confidence=0.95,
        safety_verified=safe,
    )


def test_kernel_selects_only_grounded_safe_candidates():
    kernel = InteractionKernel(run_id="run-1", objective="Open the feature")
    intent = _intent()
    snapshot = StateSnapshot(url="https://example.test", title="Home")
    kernel.register_intent(intent)
    kernel.record_snapshot(snapshot)
    candidate = _candidate(intent, snapshot)

    assert kernel.select([candidate]).id == candidate.id


def test_kernel_rejects_unverified_candidate():
    kernel = InteractionKernel(run_id="run-1", objective="Open the feature")
    intent = _intent()
    snapshot = StateSnapshot(url="https://example.test", title="Home")
    kernel.register_intent(intent)
    kernel.record_snapshot(snapshot)

    with pytest.raises(ValueError, match="no safe"):
        kernel.select([_candidate(intent, snapshot, safe=False)])


def test_dispatched_attempt_cannot_be_replayed_as_retry():
    kernel = InteractionKernel(run_id="run-1", objective="Open the feature")
    intent = _intent()
    attempt = ActionAttempt(
        intent=ActionIntent(goal=intent.objective, gesture="observe"), dispatched=True
    )
    kernel.record_attempt(attempt)

    with pytest.raises(ValueError, match="dispatched side effect"):
        kernel.record_attempt(
            ActionAttempt(
                intent=ActionIntent(goal=intent.objective, gesture="observe"),
                retry_of=attempt.id,
            )
        )


def test_complete_trace_requires_passed_verifications():
    kernel = InteractionKernel(run_id="run-1", objective="Open the feature")
    intent = _intent()
    kernel.register_intent(intent)
    kernel.record_verification(
        OutcomeVerification(intent_id=intent.id, status="failed", reason="not visible")
    )

    with pytest.raises(ValueError, match="failed verifications"):
        kernel.trace(complete=True)


def test_affordances_are_limited_to_current_visible_snapshot():
    current = StateSnapshot(url="https://example.test", title="Home")
    affordances = [
        Affordance(label="Current", method="click", evidence_refs=[current.id]),
        Affordance(label="Other", method="click", evidence_refs=["snapshot:other:1"]),
        Affordance(label="Hidden", method="click", evidence_refs=[current.id], visible=False),
    ]

    assert [item.label for item in affordances_for_snapshot(affordances, current.id)] == ["Current"]
