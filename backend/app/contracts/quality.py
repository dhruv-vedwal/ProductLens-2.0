"""Quality scoring and repair decision contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class QualityReport(BaseModel):
    execution_score: float = Field(ge=0, le=1)
    workflow_score: float = Field(ge=0, le=1)
    visual_score: float = Field(ge=0, le=1)
    story_score: float = Field(ge=0, le=1)
    audio_score: float = Field(ge=0, le=1)
    synchronization_score: float = Field(ge=0, le=1)
    viewport_score: float = Field(ge=0, le=1)
    overall_score: float = Field(ge=0, le=1)
    hard_failures: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    layer_evidence: dict[str, list[str]] = Field(default_factory=dict)
    owner_by_failure: dict[str, str] = Field(default_factory=dict)


class RepairDecision(BaseModel):
    category: Literal[
        "discovery",
        "workflow",
        "presentation",
        "execution",
        "narration",
        "video_qa",
        "provider",
        "visual_review",
        "external_input",
        "internal",
        "none",
    ]
    action: Literal[
        "re_render",
        "targeted_reexecution",
        "regenerate_narration",
        "provider_retry",
        "needs_input",
        "fail",
    ]
    reasons: list[str] = Field(default_factory=list)
    retry_from_stage: str | None = None
    retry_boundary: str | None = None
