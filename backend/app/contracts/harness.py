"""Contracts for the trace-only interaction capability harness.

The harness is intentionally independent from narration and rendering.  These
models are the durable boundary between a browser agent that proved an outcome
and downstream presentation stages that may explain it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator


class HarnessMode(StrEnum):
    CAPABILITY = "capability"
    CERTIFICATION = "certification"
    PRODUCTION_TRACE = "production_trace"


class HarnessStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    VERIFIED = "VERIFIED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class HarnessFailureCode(StrEnum):
    URL_LOAD_FAILED = "URL_LOAD_FAILED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    CAPTCHA_BLOCKED = "CAPTCHA_BLOCKED"
    OTP_REQUIRED = "OTP_REQUIRED"
    TARGET_NOT_VISIBLE = "TARGET_NOT_VISIBLE"
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
    TARGET_OCCLUDED = "TARGET_OCCLUDED"
    UNSAFE_ACTION = "UNSAFE_ACTION"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    DISPATCH_FAILED = "DISPATCH_FAILED"
    STATE_DID_NOT_CHANGE = "STATE_DID_NOT_CHANGE"
    POSTCONDITION_FAILED = "POSTCONDITION_FAILED"
    OUTCOME_UNVERIFIED = "OUTCOME_UNVERIFIED"
    EXPLORATION_BUDGET_EXCEEDED = "EXPLORATION_BUDGET_EXCEEDED"
    STEP_BUDGET_EXCEEDED = "STEP_BUDGET_EXCEEDED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    TRACE_INCOMPLETE = "TRACE_INCOMPLETE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class HarnessLimits(BaseModel):
    max_steps: int = Field(default=24, ge=1, le=500)
    max_pages: int = Field(default=6, ge=1, le=100)
    max_exploration_steps: int = Field(default=12, ge=0, le=250)
    max_model_calls: int = Field(default=3, ge=0, le=100)
    max_duration_seconds: int = Field(default=900, ge=1, le=86_400)


class InteractionHarnessRequest(BaseModel):
    """User-safe request for a trace-only capability run."""

    request_id: str | None = None
    url: HttpUrl
    objective: str = Field(min_length=3, max_length=2_000)
    mode: HarnessMode = HarnessMode.CAPABILITY
    audience: str = Field(default="product prospect", min_length=2, max_length=120)
    auth_reference: str | None = None
    side_effect_policy: Literal["read_only", "reversible", "explicitly_authorized"] = "read_only"
    limits: HarnessLimits = Field(default_factory=HarnessLimits)
    required_outcomes: list[str] = Field(default_factory=list, max_length=24)
    excluded_actions: list[str] = Field(default_factory=list, max_length=24)
    cloud_browser: bool | None = None
    project_id: str | None = None

    @field_validator("auth_reference")
    @classmethod
    def opaque_auth_reference(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("secret://productlens/"):
            raise ValueError("auth_reference must be an opaque ProductLens secret reference")
        return value


class HarnessOutcome(BaseModel):
    verified: bool = False
    evidence_ids: list[str] = Field(default_factory=list, max_length=128)
    summary: str = Field(default="", max_length=500)


class HarnessFailure(BaseModel):
    code: HarnessFailureCode
    message: str = Field(min_length=1, max_length=500)
    owning_layer: Literal["discovery", "planning", "execution", "verification", "provider", "budget", "internal"]
    last_verified_event_id: str | None = None
    safe_retry_boundary: str | None = None
    side_effect_dispatched: bool = False
    evidence_ids: list[str] = Field(default_factory=list, max_length=64)


class InteractionHarnessResult(BaseModel):
    schema_version: int = Field(default=1, ge=1)
    run_id: str
    status: HarnessStatus
    objective_fingerprint: str
    trace_artifact: str | None = None
    outcome: HarnessOutcome = Field(default_factory=HarnessOutcome)
    metrics: dict[str, Any] = Field(default_factory=dict)
    failure: HarnessFailure | None = None

