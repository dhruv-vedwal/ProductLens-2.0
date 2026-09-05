from __future__ import annotations

from enum import StrEnum

from productlens.contracts.models import FailureCode


class RunStage(StrEnum):
    QUEUED = "QUEUED"
    FEASIBILITY_CHECK = "FEASIBILITY_CHECK"
    DISCOVERING = "DISCOVERING"
    PLAN_READY = "PLAN_READY"
    PLAN_VALIDATED = "PLAN_VALIDATED"
    EXPLORATORY_EXECUTION = "EXPLORATORY_EXECUTION"
    WORKFLOW_VALIDATED = "WORKFLOW_VALIDATED"
    PRODUCTION_EXECUTION = "PRODUCTION_EXECUTION"
    TRACE_READY = "TRACE_READY"
    PRESENTATION_PLANNED = "PRESENTATION_PLANNED"
    NARRATION_READY = "NARRATION_READY"
    RENDERING = "RENDERING"
    RENDERED = "RENDERED"
    VIDEO_QA = "VIDEO_QA"
    QA_PASSED = "QA_PASSED"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


RETRYABLE = {
    FailureCode.PROVIDER_FAILURE,
    FailureCode.TIMEOUT,
    FailureCode.TARGET_RESOLUTION_FAILURE,
}
TRANSITIONS = {stage: {RunStage.FAILED} for stage in RunStage}
for before, after in zip(list(RunStage)[:-2], list(RunStage)[1:-1]):
    TRANSITIONS[before].add(after)
TRANSITIONS[RunStage.QA_PASSED].add(RunStage.COMPLETE)


def validate_transition(current: RunStage, target: RunStage) -> None:
    if target not in TRANSITIONS[current]:
        raise ValueError(f"invalid lifecycle transition {current} -> {target}")


def retry_stage(failure: FailureCode, failed_at: RunStage) -> RunStage | None:
    if failure not in RETRYABLE:
        return None
    if failure is FailureCode.TARGET_RESOLUTION_FAILURE:
        return RunStage.PRODUCTION_EXECUTION
    if failure is FailureCode.PROVIDER_FAILURE:
        return failed_at
    return RunStage.PRODUCTION_EXECUTION
