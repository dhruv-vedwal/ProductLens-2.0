import pytest

from productlens.orchestration.lifecycle import RunStage
from productlens.services.stage_contracts import STAGE_CONTRACTS, stage_contract, stage_lifecycle


def test_all_persisted_stage_names_have_one_lifecycle_boundary():
    assert [item.name for item in STAGE_CONTRACTS] == [
        "DISCOVERY",
        "PLANNING",
        "EXECUTION",
        "NARRATION",
        "RENDER",
        "VIDEO_QA",
    ]
    assert stage_lifecycle("DISCOVERY") is RunStage.DISCOVERING
    assert stage_lifecycle("VIDEO_QA") is RunStage.VIDEO_QA


def test_unknown_stage_fails_closed():
    with pytest.raises(ValueError, match="unknown generation stage"):
        stage_contract("NOT_A_STAGE")
