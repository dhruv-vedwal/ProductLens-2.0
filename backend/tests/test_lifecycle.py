import pytest

from app.contracts.models import FailureCode
from app.orchestration.lifecycle import RunStage, retry_stage, validate_transition


def test_lifecycle_rejects_skipping_validation():
    with pytest.raises(ValueError):
        validate_transition(RunStage.QUEUED, RunStage.PRODUCTION_EXECUTION)
    validate_transition(RunStage.QUEUED, RunStage.FEASIBILITY_CHECK)


def test_retry_is_targeted_not_blind():
    assert (
        retry_stage(FailureCode.TARGET_RESOLUTION_FAILURE, RunStage.PRODUCTION_EXECUTION)
        is RunStage.PRODUCTION_EXECUTION
    )
    assert (
        retry_stage(FailureCode.STATE_VERIFICATION_FAILURE, RunStage.PRODUCTION_EXECUTION) is None
    )
