"""Editorial narration and presentation contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.contracts.common import AudienceProfile, Rect


class EditorialFact(BaseModel):
    """A concise claim with the observed evidence that permits it."""

    text: str = Field(min_length=1, max_length=420)
    evidence: list[str] = Field(min_length=1)


class EditorialBrief(BaseModel):
    """Product understanding used to direct a demo, never to execute browser actions."""

    title: str = Field(min_length=1, max_length=90)
    product_purpose: str = Field(min_length=1, max_length=420)
    opening_message: str = Field(min_length=1, max_length=420)
    navigation_order: list[str] = Field(default_factory=list)
    facts: list[EditorialFact] = Field(default_factory=list)
    excluded_areas: list[str] = Field(default_factory=list)


class EditorialScene(BaseModel):
    """A visible, evidence-backed unit of the final walkthrough."""

    id: str
    operation_id: str | None = None
    title: str = Field(min_length=1, max_length=120)
    purpose: str = Field(min_length=1, max_length=420)
    narration: str = Field(min_length=1, max_length=520)
    evidence: list[str] = Field(min_length=1)
    # ``type`` and ``submit`` are distinct presentational beats. Treating
    # them as generic observation hid the causal form journey and also let a
    # model replace their carefully grounded narration with field-label copy.
    interaction: Literal["opening", "scroll", "navigate", "click", "type", "submit", "observe"]
    required_dwell_seconds: float = Field(ge=1.0, le=20.0)
    completion_criteria: list[str] = Field(min_length=1)
    caption_safe_zone: Literal["bottom", "top"] = "bottom"
    story_phase: Literal[
        "context", "enter", "explain", "demonstrate", "verify", "transition", "close"
    ] = "explain"
    page_url: str | None = None
    visible_proof: list[str] = Field(default_factory=list)
    action_classification: Literal["essential", "transitional", "dead_time"] = "essential"
    transition: Literal["cut", "dissolve", "match_scroll", "hold"] = "cut"


class EditorialStoryboard(BaseModel):
    brief: EditorialBrief
    scenes: list[EditorialScene] = Field(min_length=1)
    minimum_duration_seconds: int = Field(ge=5, le=600)


class EditorialNarrationLine(BaseModel):
    """A model-proposed rewrite for one immutable storyboard scene."""

    id: str = Field(min_length=1)
    narration: str = Field(min_length=10, max_length=360)


class EditorialNarrationDraft(BaseModel):
    """Small editorial-only contract; it cannot alter execution or timing."""

    lines: list[EditorialNarrationLine] = Field(min_length=1, max_length=60)


class NarrationSegment(BaseModel):
    """One approved line shared by captions, cursor timing, and optional TTS."""

    scene_id: str = Field(min_length=1, max_length=120)
    event_id: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=10, max_length=520)
    # Editorial lines carry evidence IDs; deterministic compatibility lines may
    # retain a redacted browser-state mapping until they are converted into
    # evidence references by the owning storyboard layer.
    evidence: list[str] = Field(min_length=1, max_length=24)
    facts: list[str] | dict[str, Any] = Field(default_factory=list)
    opening: bool = False
    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_interval(self) -> NarrationSegment:
        if (
            self.start_seconds is not None
            and self.end_seconds is not None
            and self.end_seconds <= self.start_seconds
        ):
            raise ValueError("narration segment end must be after its start")
        return self


class NarrationScript(BaseModel):
    """Versioned script contract owned by the storyboard, not the renderer."""

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["caption_only", "tts"] = "caption_only"
    audience: str = Field(default="product prospect", min_length=1, max_length=160)
    audience_profile: AudienceProfile = Field(default_factory=AudienceProfile)
    timing_owner: Literal["scene", "measured_audio"] = "scene"
    segments: list[NarrationSegment] = Field(min_length=1, max_length=60)


class CameraDecision(BaseModel):
    event_id: str
    focus: Rect
    zoom: float = Field(ge=1, le=4)
    reason: str


class PresentationPlan(BaseModel):
    trace_run_id: str
    camera: list[CameraDecision]
    cursor_event_ids: list[str]
    cursor_paths: list[dict[str, Any]] = Field(default_factory=list)
    captions: list[dict[str, Any]] = Field(default_factory=list)
