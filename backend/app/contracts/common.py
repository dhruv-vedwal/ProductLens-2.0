"""Shared foundational contracts used across discovery, planning, and presentation."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class FailureCode(StrEnum):
    PLANNING_FAILURE = "PLANNING_FAILURE"
    DISCOVERY_FAILURE = "DISCOVERY_FAILURE"
    AUTH_FAILURE = "AUTH_FAILURE"
    CAPTCHA_FAILURE = "CAPTCHA_FAILURE"
    APPLICATION_BLOCKER = "APPLICATION_BLOCKER"
    TARGET_RESOLUTION_FAILURE = "TARGET_RESOLUTION_FAILURE"
    EXECUTION_FAILURE = "EXECUTION_FAILURE"
    STATE_VERIFICATION_FAILURE = "STATE_VERIFICATION_FAILURE"
    CAPTURE_FAILURE = "CAPTURE_FAILURE"
    TRACE_FAILURE = "TRACE_FAILURE"
    PRESENTATION_FAILURE = "PRESENTATION_FAILURE"
    NARRATION_FAILURE = "NARRATION_FAILURE"
    AUDIO_FAILURE = "AUDIO_FAILURE"
    RENDER_FAILURE = "RENDER_FAILURE"
    VIDEO_QA_FAILURE = "VIDEO_QA_FAILURE"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    TIMEOUT = "TIMEOUT"
    UNSUPPORTED_APPLICATION = "UNSUPPORTED_APPLICATION"
    UNSUPPORTED_INTERACTION = "UNSUPPORTED_INTERACTION"


class AudienceProfile(BaseModel):
    """Structured viewer context shared by planning and editorial layers."""

    type: Literal[
        "general_user",
        "sales",
        "recruiter",
        "founder",
        "prospect",
        "support",
        "onboarding",
        "internal",
        "developer",
        "administrator",
        "end_user",
        "buyer",
    ] = "prospect"
    priorities: list[str] = Field(default_factory=list, max_length=12)
    vocabulary: Literal["plain", "technical", "executive"] = "plain"
    depth: Literal["overview", "standard", "deep"] = "standard"
    narration_style: Literal["conversational", "concise", "technical", "persuasive"] = (
        "conversational"
    )
    workflow_preferences: list[str] = Field(default_factory=list, max_length=12)


class WorkflowState(StrEnum):
    NEW = "NEW"
    LOGIN_PAGE = "LOGIN_PAGE"
    AUTHENTICATED = "AUTHENTICATED"
    OPENING_PAGE = "OPENING_PAGE"
    FEATURE_CONTEXT = "FEATURE_CONTEXT"
    DETAIL_VIEW = "DETAIL_VIEW"
    ACTION_IN_PROGRESS = "ACTION_IN_PROGRESS"
    OUTCOME_VISIBLE = "OUTCOME_VISIBLE"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class OperationKind(StrEnum):
    NAVIGATE = "Navigate"
    OPEN_NAVIGATION_ITEM = "OpenNavigationItem"
    CLICK = "Click"
    FILL_TEXT = "FillText"
    FILL_EMAIL = "FillEmail"
    FILL_PHONE = "FillPhone"
    SELECT_OPTION = "SelectOption"
    SELECT_DATE = "SelectDate"
    SELECT_DATE_RANGE = "SelectDateRange"
    CHECK = "Check"
    UNCHECK = "Uncheck"
    CHOOSE_RADIO = "ChooseRadio"
    SEARCH = "Search"
    APPLY_FILTER = "ApplyFilter"
    OPEN_MODAL = "OpenModal"
    CLOSE_MODAL = "CloseModal"
    SCROLL_TO = "ScrollTo"
    SUBMIT = "Submit"
    WAIT_FOR_STATE = "WaitForState"
    READ_VALUE = "ReadValue"
    VERIFY_STATE = "VerifyState"
    CREATE_RECORD = "CreateRecord"
    # Universal interaction vocabulary for unfamiliar interfaces.  These are
    # browser gestures, not product/domain actions; their semantic targets and
    # parameters are discovered at runtime from evidence.
    KEY_PRESS = "KeyPress"
    HOVER = "Hover"
    POINTER_SEQUENCE = "PointerSequence"
    DRAG = "Drag"
    UPLOAD = "Upload"


class Target(BaseModel):
    """Semantic target, never a coordinate as the primary identity."""

    name: str
    test_id: str | None = None
    role: str | None = None
    label: str | None = None
    text: str | None = None
    selector: str | None = None
    source_url: str | None = None
    confidence_required: float = Field(default=0.8, ge=0, le=1)


class Postcondition(BaseModel):
    # ``focused`` and ``options_visible`` are interaction witnesses rather
    # than product-specific actions.  ``surface_changed`` is used by pointer
    # editors (canvas/SVG) where no DOM value exists to compare.  Keeping them
    # in the contract makes a dispatched gesture insufficient on its own: the
    # browser must prove the state the viewer is meant to see.
    kind: Literal[
        "url",
        "visible",
        "value",
        "test_state",
        "text",
        "changed",
        "focused",
        "options_visible",
        "surface_changed",
        "overlay_clear",
    ]
    expected: Any
    target: Target | None = None
    timeout_ms: int = Field(default=5_000, ge=1)


class Rect(BaseModel):
    x: float
    y: float
    width: float
    height: float


class Viewport(BaseModel):
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    device_scale_factor: float = Field(default=1.0, gt=0)


class ViewportDecision(BaseModel):
    """A recorded browser viewport decision, distinct from editorial camera zoom."""

    viewport: Viewport
    browser_zoom_percent: int = Field(default=100, ge=50, le=200)
    score: float = Field(ge=0)
    evidence: list[str] = Field(default_factory=list)


class ProviderConfig(BaseModel):
    name: str
    provider_type: Literal["llm", "tts", "browser", "storage"]
    credential_reference: str | None = None
    active: bool = True

    @field_validator("credential_reference")
    @classmethod
    def only_allow_references(cls, value: str | None) -> str | None:
        if value and (" " in value or value.startswith("sk-") or len(value) > 160):
            raise ValueError("credential_reference must be a secret reference, never a credential")
        return value
