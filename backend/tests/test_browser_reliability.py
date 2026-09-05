import pytest

from productlens.browser.grounding import TargetCandidate, choose_candidate
from productlens.browser.recovery import RecoveryBudget
from productlens.contracts.models import FailureCode, Target


def test_grounding_uses_confidence_threshold():
    chosen = choose_candidate(
        Target(name="Create", confidence_required=0.8),
        [
            TargetCandidate("#wrong", "text", 0.2),
            TargetCandidate("[data-testid=create]", "stable", 0.99),
        ],
    )
    assert chosen.selector == "[data-testid=create]"
    with pytest.raises(LookupError):
        choose_candidate(
            Target(name="Create", confidence_required=0.9), [TargetCandidate("#weak", "text", 0.5)]
        )


def test_recovery_is_budgeted_and_targeted():
    budget = RecoveryBudget()
    assert budget.allow_reground(FailureCode.TARGET_RESOLUTION_FAILURE)
    assert not budget.allow_reground(FailureCode.TARGET_RESOLUTION_FAILURE)
    assert not budget.allow_reground(FailureCode.STATE_VERIFICATION_FAILURE)
