"""Shared durable stage boundaries for every generation entry point.

Keeping the queue name and lifecycle state in one contract prevents fixture,
URL, broker, and local-worker paths from drifting into subtly different stage
semantics. The contract is provider- and product-neutral.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.orchestration.lifecycle import RunStage


@dataclass(frozen=True, slots=True)
class StageContract:
    name: str
    lifecycle: RunStage


STAGE_CONTRACTS: Final[tuple[StageContract, ...]] = (
    StageContract("DISCOVERY", RunStage.DISCOVERING),
    StageContract("PLANNING", RunStage.PLAN_VALIDATED),
    StageContract("EXECUTION", RunStage.PRODUCTION_EXECUTION),
    StageContract("NARRATION", RunStage.PRESENTATION_PLANNED),
    StageContract("RENDER", RunStage.RENDERING),
    StageContract("VIDEO_QA", RunStage.VIDEO_QA),
)

_BY_NAME: Final[dict[str, StageContract]] = {item.name: item for item in STAGE_CONTRACTS}


def stage_contract(name: str) -> StageContract:
    """Resolve a persisted stage or fail closed for an unknown stage."""
    try:
        return _BY_NAME[name]
    except KeyError as error:
        raise ValueError(f"unknown generation stage: {name}") from error


def stage_lifecycle(name: str) -> RunStage:
    return stage_contract(name).lifecycle
