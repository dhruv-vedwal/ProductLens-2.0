from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

from app.contracts.harness import InteractionHarnessRequest


class InteractionHarnessAPIRequest(InteractionHarnessRequest):
    """Public trace-only capability request.

    Keeping this as a contract subclass prevents the API from accepting raw
    credentials or silently turning a capability test into a video job.
    """

    """Public alias retaining the shared harness contract."""


class FixtureRequest(BaseModel):
    gate: int = Field(ge=1, le=6)
    objective: str = Field(min_length=3)
    render: bool = True
    project_id: str | None = None


class PresentationOptions(BaseModel):
    """User-owned presentation preferences carried through every run stage.

    These options are intentionally product-neutral.  They configure the
    presentation layer; they never become browser actions or website-specific
    selectors.  Keeping them in the durable job payload prevents the frontend
    from advertising controls that silently disappear at render time.
    """

    include_audio: bool = True
    narration_style: Literal["teach", "showcase"] = "teach"
    pace: float = Field(default=1.0, ge=0.5, le=2.0)
    browser_zoom_percent: int = Field(default=100, ge=50, le=200)
    subtitles_enabled: bool = True
    subtitle_style: Literal["minimal", "apple", "youtube"] = "minimal"
    subtitle_position: Literal["top", "bottom"] = "bottom"
    subtitle_font_size: Literal["sm", "md", "lg"] = "md"
    intro_template: str = Field(default="welcome", min_length=1, max_length=80)
    studio_polish: bool = True
    cursor_style: str = Field(default="default", min_length=1, max_length=80)
    highlight_style: str = Field(default="spotlight", min_length=1, max_length=80)
    click_zoom: bool = True
    export_aspect: Literal["16:9", "9:16", "1:1"] = "16:9"
    export_resolution: Literal["720", "1080", "1440", "2160"] = "1080"
    language: str = Field(default="en", min_length=2, max_length=16)
    accent: str = Field(default="neutral", min_length=2, max_length=32)


class GenerationRequest(BaseModel):
    request_id: str | None = None
    url: HttpUrl
    objective: str = Field(min_length=3, max_length=2_000)
    allow_external_side_effects: bool = False
    # Explicitly narrower than external side effects: permits a discovered
    # isolated demo record only after workflow/outcome validation.
    allow_isolated_record_creation: bool = False
    # ``None`` means capability-aware default: use the configured
    # Browserbase+Stagehand exploration path, while keeping local development
    # viable when no cloud browser credentials are present. An explicit false
    # remains an opt-out for deterministic local fixture work.
    cloud_discovery: bool | None = None
    max_pages: int = Field(default=6, ge=1, le=12)
    render: bool = True
    project_id: str | None = None
    credential_reference: str | None = None
    audience: str = Field(default="product prospect", min_length=2, max_length=120)
    # The duration is an editorial target/minimum, not a renderer stretch
    # instruction. Keep the API envelope aligned with ObjectiveSpec/DemoPlan
    # so a legitimately detailed product story is not rejected at 5 minutes.
    target_duration_seconds: int = Field(default=120, ge=30, le=600)
    presentation: PresentationOptions = Field(default_factory=PresentationOptions)

    @field_validator("credential_reference")
    @classmethod
    def credential_must_be_an_opaque_reference(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("secret://productlens/"):
            raise ValueError("credential_reference must be an opaque ProductLens secret reference")
        return value


class RetryRequest(BaseModel):
    """Explicit retry configuration; safe defaults prevent replaying side effects."""

    allow_external_side_effects: bool = False
    allow_isolated_record_creation: bool = False
    cloud_discovery: bool | None = None
    max_pages: int | None = Field(default=None, ge=1, le=12)
    render: bool | None = None
    credential_reference: str | None = None
    audience: str | None = Field(default=None, min_length=2, max_length=120)
    target_duration_seconds: int | None = Field(default=None, ge=30, le=600)
    presentation: PresentationOptions | None = None
    retry_from_stage: Literal["PLANNING", "EXECUTION", "NARRATION", "RENDER", "VIDEO_QA"] | None = (
        None
    )

    @field_validator("credential_reference")
    @classmethod
    def retry_credential_must_be_an_opaque_reference(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("secret://productlens/"):
            raise ValueError("credential_reference must be an opaque ProductLens secret reference")
        return value


class RetentionRequest(BaseModel):
    older_than_seconds: int = Field(default=0, ge=0, le=31_536_000)
