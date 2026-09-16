"""Typed, serialisable contracts between planning, execution and presentation."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class SemanticOperation(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: OperationKind
    intent: str
    target: Target | None = None
    value: Any = None
    preconditions: list[Postcondition] = Field(default_factory=list)
    postconditions: list[Postcondition] = Field(default_factory=list)
    critical: bool = True
    # These fields are planning facts, not executor instructions.  Keeping
    # them with the semantic action prevents the later presentation layer from
    # having to guess whether an observed scroll was context, explanation or
    # proof of an outcome.
    story_phase: (
        Literal["establish", "explore", "explain", "demonstrate", "verify", "transition"] | None
    ) = None
    # A single verified visual beat can satisfy adjacent editorial duties
    # (for example, explore and explain the same readable card). This keeps
    # page completeness explicit without forcing a cloud browser to replay an
    # identical scroll merely to create a second trace event.
    page_contract_phases: list[
        Literal["establish", "explore", "explain", "demonstrate", "verify", "transition"]
    ] = Field(default_factory=list)
    page_url: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    # A page chapter is only complete once every selected visible content group
    # has received a readable scene.  These are planning facts, carried into
    # the trace so the director never has to infer coverage from route names.
    required_content_groups: list[str] = Field(default_factory=list)
    covered_content_groups: list[str] = Field(default_factory=list)
    # Explicit policy is attached only when a runtime ActionIntent supplies
    # one.  ``unspecified`` preserves compatibility with older validated plans
    # whose operation kind is still checked by the planning safety policy.
    side_effect_policy: Literal["unspecified", "read_only", "authorized_mutation", "blocked"] = (
        "unspecified"
    )


class ActionIntent(BaseModel):
    """Provider-neutral runtime gesture proposed from live UI evidence.

    ``gesture`` is deliberately a small browser vocabulary.  It does not
    encode a CRM, canvas, workflow-builder, or any other product's semantics;
    the model supplies the observed target and expected state for this run.
    """

    schema_version: int = Field(default=1, ge=1)
    id: str = Field(default_factory=lambda: str(uuid4()))
    goal: str = Field(min_length=3, max_length=240)
    gesture: Literal[
        "observe",
        "click",
        "type",
        "key",
        "scroll",
        "hover",
        "pointer_sequence",
        "drag",
        "upload",
        "select",
        "submit",
        "wait",
        "verify",
    ]
    target: Target | None = None
    destination: Target | None = None
    value: Any = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    expected_state: list[Postcondition] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list, max_length=24)
    side_effect_policy: Literal["read_only", "authorized_mutation", "blocked"] = "read_only"

    @model_validator(mode="after")
    def validate_gesture(self) -> ActionIntent:
        page_key = self.gesture == "key" and self.parameters.get("scope") == "page"
        if (
            self.gesture
            in {"click", "type", "key", "scroll", "hover", "drag", "upload", "select", "submit"}
            and self.target is None
            and not page_key
        ):
            raise ValueError(f"{self.gesture} gestures require a grounded target")
        if self.gesture == "drag" and self.destination is None:
            raise ValueError("drag gestures require a grounded destination")
        if self.gesture == "pointer_sequence" and not (
            self.parameters.get("points") or self.parameters.get("relative_points")
        ):
            raise ValueError("pointer_sequence gestures require observed points or relative_points")
        if self.gesture == "submit" and not self.expected_state:
            raise ValueError("submit gestures require an evidence-backed expected state")
        if self.gesture == "submit" and self.side_effect_policy != "authorized_mutation":
            raise ValueError("submit gestures require explicit mutation authorization")
        if self.side_effect_policy == "blocked" and self.gesture not in {
            "observe",
            "wait",
            "verify",
        }:
            raise ValueError("blocked side-effect policy cannot dispatch an interaction")
        return self

    def to_operation(self) -> SemanticOperation:
        """Compile a gesture into the universal executor contract."""
        kind = {
            "observe": OperationKind.READ_VALUE,
            "click": OperationKind.CLICK,
            "type": OperationKind.FILL_TEXT,
            "key": OperationKind.KEY_PRESS,
            "scroll": OperationKind.SCROLL_TO,
            "hover": OperationKind.HOVER,
            "pointer_sequence": OperationKind.POINTER_SEQUENCE,
            "drag": OperationKind.DRAG,
            "upload": OperationKind.UPLOAD,
            "select": OperationKind.SELECT_OPTION,
            "submit": OperationKind.SUBMIT,
            "wait": OperationKind.WAIT_FOR_STATE,
            "verify": OperationKind.VERIFY_STATE,
        }[self.gesture]
        value = self.value
        if self.gesture == "drag":
            value = {**self.parameters, "destination": self.destination.model_dump(mode="json")}
        elif self.parameters:
            value = {**self.parameters, **({"value": self.value} if self.value is not None else {})}
        return SemanticOperation(
            id=self.id,
            kind=kind,
            intent=self.goal,
            target=self.target,
            value=value,
            postconditions=list(self.expected_state),
            critical=self.side_effect_policy != "read_only",
            side_effect_policy=self.side_effect_policy,
            evidence_refs=list(self.evidence_refs),
        )


class WorkflowStep(BaseModel):
    id: str
    intent: str
    operation: SemanticOperation
    from_state: WorkflowState | None = None
    to_state: WorkflowState | None = None
    # Planning metadata is deliberately carried with the executable step.  It
    # lets validation, narration, presentation, and targeted repair agree on
    # why a step exists without re-interpreting the operation later.
    page_requirement: str | None = None
    importance: Literal["critical", "important", "supporting"] = "important"
    narration_intent: str = "Explain the visible result and its relevance."
    visual_intent: str = "Keep the relevant page context readable."
    fallback_strategy: str | None = None
    allowed_retries: int = Field(default=1, ge=0, le=3)
    completion_criteria: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    page_phase: (
        Literal["establish", "explore", "explain", "demonstrate", "verify", "transition"] | None
    ) = None


class ReplanDecision(BaseModel):
    """Auditable replacement for the currently failing workflow suffix."""

    reason: str = Field(min_length=3, max_length=500)
    replacement_steps: list[WorkflowStep] = Field(min_length=1, max_length=24)
    evidence_refs: list[str] = Field(default_factory=list, max_length=24)
    preserve_story: bool = True
    # The engine fills this in when persisting the decision.  A dispatched
    # operation must never appear in its replacement list.
    failed_operation_id: str | None = None
    dispatched: bool = False


class DemoPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=3)
    narrative_goal: str
    audience: str
    target_duration_seconds: int = Field(ge=5, le=600)
    # Generic/test plans may legitimately be very short. Production planning
    # always writes a stricter objective-derived envelope.
    minimum_duration_seconds: int = Field(default=5, ge=5, le=600)
    maximum_duration_seconds: int = Field(default=600, ge=30, le=900)
    selected_workflow: str
    workflow_steps: list[WorkflowStep] = Field(min_length=1)
    synthetic_data_plan: dict[str, Any] = Field(default_factory=dict)
    expected_outcomes: list[str] = Field(min_length=1)
    important_elements: list[str] = Field(default_factory=list)
    excluded_areas: list[str] = Field(default_factory=list)
    viewport_strategy: str
    risk_flags: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_duration_envelope(self) -> DemoPlan:
        if (
            not self.minimum_duration_seconds
            <= self.target_duration_seconds
            <= self.maximum_duration_seconds
        ):
            raise ValueError(
                "target_duration_seconds must fall within the approved duration envelope"
            )
        return self


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
    evidence: list[str] = Field(default_factory=list, max_length=24)
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


class InteractionEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    operation_id: str
    kind: OperationKind
    intent: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Action dispatch is distinct from scene completion. The cursor/click
    # renderer needs the former; dwell and postcondition QA use the latter.
    action_at: datetime | None = None
    target: Target | None = None
    postconditions: list[Postcondition] = Field(default_factory=list, max_length=24)
    target_rect: Rect | None = None
    viewport: Viewport | None = None
    scroll: dict[str, float] = Field(default_factory=dict)
    page_url: str | None = None
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    # Provider-neutral before/after comparison. This is an audit witness,
    # never an instruction to replay an action.
    state_delta: dict[str, Any] = Field(default_factory=dict)
    side_effects: list[str] = Field(default_factory=list)
    # A compact audit trail for a bounded semantic re-ground.  This is deliberately
    # evidence, rather than an instruction to replay an operation indefinitely.
    recovery: list[dict[str, Any]] = Field(default_factory=list)
    screenshot_path: str | None = None
    success: bool
    duration_ms: int = Field(ge=0)
    page_contract_phases: list[
        Literal["establish", "explore", "explain", "demonstrate", "verify", "transition"]
    ] = Field(default_factory=list)
    required_content_groups: list[str] = Field(default_factory=list)
    covered_content_groups: list[str] = Field(default_factory=list)
    # Ordered scroll witnesses prove that a presentation scroll was a real
    # browser motion, rather than a renderer jump between two screenshots.
    scroll_path: list[dict[str, float]] = Field(default_factory=list)


class DemoTrace(BaseModel):
    run_id: str
    objective: str
    started_at: datetime
    # The provider recorder may start before navigation, but this clock origin
    # is set only once the requested page is stable. It lets presentation trim
    # provisioning/navigation noise and place beats on the verified product
    # frame rather than a blank opening.
    recording_started_at: datetime | None = None
    completed_at: datetime | None = None
    events: list[InteractionEvent] = Field(default_factory=list)
    final_state: WorkflowState = WorkflowState.NEW
    outcome_verified: bool = False
    browser_context_id: str | None = None
    viewport_decision: ViewportDecision | None = None
    browser_zoom_percent: int = Field(default=100, ge=50, le=200)
    source_frame_rate: float | None = Field(default=None, gt=0)
    page_states: list[dict[str, Any]] = Field(default_factory=list)
    loading_periods: list[dict[str, Any]] = Field(default_factory=list)
    dom_snapshot_refs: list[str] = Field(default_factory=list)
    accessibility_snapshot_refs: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    # Runtime plan changes are evidence, not hidden control flow.  Each entry
    # records the failed step, whether its side effect was dispatched, and the
    # replacement steps selected from the newly observed state.
    replan_decisions: list[dict[str, Any]] = Field(default_factory=list)
    # Capability decisions are copied from discovery/planning so recovery and
    # QA can explain which evidence-grounded interaction contract was active
    # during production, without re-querying a provider or guessing later.
    capability_resolutions: list[CapabilityResolution] = Field(default_factory=list, max_length=160)
    state_snapshots: list[StateSnapshot] = Field(default_factory=list, max_length=600)
    action_attempts: list[ActionAttempt] = Field(default_factory=list, max_length=600)
    verification_results: list[VerificationResult] = Field(default_factory=list, max_length=600)


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
        "internal",
        "none",
    ]
    action: Literal[
        "re_render", "targeted_reexecution", "regenerate_narration", "provider_retry", "fail"
    ]
    reasons: list[str] = Field(default_factory=list)
    retry_from_stage: str | None = None
    retry_boundary: str | None = None


class DiscoveryBudget(BaseModel):
    max_time_seconds: int = Field(default=60, ge=1)
    max_pages: int = Field(default=6, ge=1)
    max_actions: int = Field(default=24, ge=1)
    max_model_calls: int = Field(default=3, ge=0)
    max_depth: int = Field(default=2, ge=0)


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


class ObservedElement(BaseModel):
    """Non-secret, DOM-backed evidence available to a workflow planner."""

    tag: str
    role: str | None = None
    name: str
    selector: str
    href: str | None = None
    element_type: str | None = None
    required: bool = False
    options: list[str] = Field(default_factory=list)
    autocomplete: str | None = None
    text: str | None = None
    source_url: str | None = None
    actionable: bool = True
    navigation_scope: Literal["primary", "footer", "secondary", "unknown"] = "unknown"
    # Interaction affordances observed from native/ARIA state. Keeping these
    # as evidence avoids guessing drag/drop semantics from a product name or
    # a hardcoded route recipe.
    draggable: bool = False
    dropzone: bool = False
    shadow_host: bool = False


class CapabilityEvidence(BaseModel):
    """One non-secret observation supporting a runtime capability."""

    id: str = Field(min_length=1, max_length=240)
    kind: Literal["dom", "accessibility", "screenshot", "text", "geometry", "state", "network"]
    source_url: str | None = None
    summary: str = Field(min_length=1, max_length=420)
    confidence: float = Field(default=0.5, ge=0, le=1)


class RuntimeCapability(BaseModel):
    """A product-neutral capability discovered in the current UI state.

    This is deliberately not a website recipe.  It describes what the
    observed surface can do and which generic interaction strategies may be
    attempted; the semantic meaning is supplied by the current objective and
    evidence, never by an application name or route.
    """

    id: str = Field(default_factory=lambda: str(uuid4()), max_length=120)
    kind: Literal[
        "navigate",
        "inspect",
        "form",
        "modal",
        "detail",
        "table",
        "drag_drop",
        "pointer",
        "canvas",
        "graph",
        "keyboard",
        "upload",
        "rich_text",
        "virtualized_table",
        "iframe",
        "shadow_dom",
    ]
    purpose: str = Field(min_length=1, max_length=300)
    source_url: str
    targets: list[Target] = Field(default_factory=list, max_length=24)
    strategies: list[
        Literal[
            "dom",
            "accessibility",
            "stagehand",
            "playwright",
            "keyboard",
            "pointer",
            "visual_grounding",
            "screenshot_verification",
        ]
    ] = Field(default_factory=list, max_length=12)
    evidence: list[CapabilityEvidence] = Field(default_factory=list, max_length=32)
    expected_outcomes: list[str] = Field(default_factory=list, max_length=16)
    confidence: float = Field(default=0.0, ge=0, le=1)
    reversible: bool = True
    verified: bool = False


class CapabilityResolution(BaseModel):
    """Auditable resolution of an intent against one observed UI state."""

    schema_version: int = Field(default=1, ge=1)
    intent: str = Field(min_length=3, max_length=300)
    required_capabilities: list[str] = Field(default_factory=list, max_length=16)
    candidates: list[RuntimeCapability] = Field(default_factory=list, max_length=48)
    selected_capability_id: str | None = None
    selected_strategy: str | None = None
    unresolved: list[str] = Field(default_factory=list, max_length=16)
    rationale: list[str] = Field(default_factory=list, max_length=24)
    confidence: float = Field(default=0.0, ge=0, le=1)
    source_url: str | None = None


class StateSnapshot(BaseModel):
    """A redacted observation of the browser state at one evidence boundary."""

    id: str = Field(default_factory=lambda: str(uuid4()), max_length=120)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    url: str
    title: str = ""
    visible_text_hash: str | None = None
    dom_hash: str | None = None
    accessibility_hash: str | None = None
    screenshot_ref: str | None = None
    viewport: Viewport | None = None
    scroll: dict[str, float] = Field(default_factory=dict)
    loading: bool = False
    focused_target: Target | None = None
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    overlays: list[str] = Field(default_factory=list, max_length=24)


class VerificationResult(BaseModel):
    """Explicit postcondition verdict for one dispatched or read-only intent."""

    id: str = Field(default_factory=lambda: str(uuid4()), max_length=120)
    intent_id: str
    status: Literal["passed", "failed", "inconclusive", "skipped"]
    expected: list[Postcondition] = Field(default_factory=list, max_length=24)
    observed_state_id: str | None = None
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    confidence: float = Field(default=0.0, ge=0, le=1)
    reason: str = Field(default="", max_length=500)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ActionAttempt(BaseModel):
    """Durable action lifecycle record, including retries and verification."""

    id: str = Field(default_factory=lambda: str(uuid4()), max_length=120)
    intent: ActionIntent
    dispatched: bool = False
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    before_state_id: str | None = None
    after_state_id: str | None = None
    verification: VerificationResult | None = None
    retry_of: str | None = None
    error: str | None = Field(default=None, max_length=500)


class InteractionIntent(BaseModel):
    """A single user-facing outcome the interaction kernel must achieve.

    This is intentionally separate from ``SemanticOperation``: an intent is
    a goal, while an operation is only one possible browser gesture that may
    satisfy it after observing the live UI.
    """

    schema_version: int = Field(default=1, ge=1)
    id: str = Field(default_factory=lambda: str(uuid4()), max_length=120)
    objective: str = Field(min_length=3, max_length=420)
    audience_value: str = Field(min_length=3, max_length=420)
    safety_policy: Literal["read_only", "authorized_mutation", "blocked"] = "read_only"
    required_outcomes: list[Postcondition] = Field(default_factory=list, max_length=24)
    excluded_actions: list[str] = Field(default_factory=list, max_length=24)


class Affordance(BaseModel):
    """One action possibility grounded in the current visible UI snapshot."""

    id: str = Field(default_factory=lambda: str(uuid4()), max_length=120)
    label: str = Field(min_length=1, max_length=240)
    role: str | None = Field(default=None, max_length=80)
    method: Literal[
        "click",
        "type",
        "keypress",
        "hover",
        "scroll",
        "drag",
        "select",
        "upload",
        "wait",
    ]
    target: Target | None = None
    geometry: dict[str, float] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list, max_length=24)
    confidence: float = Field(default=0.0, ge=0, le=1)
    risk: Literal["none", "low", "medium", "high"] = "none"
    visible: bool = True
    enabled: bool = True


class ActionCandidate(BaseModel):
    """A proposed atomic gesture, never a product-specific workflow command."""

    id: str = Field(default_factory=lambda: str(uuid4()), max_length=120)
    intent_id: str
    action: ActionIntent
    affordance_id: str | None = None
    source_snapshot_id: str
    preconditions: list[Postcondition] = Field(default_factory=list, max_length=24)
    expected_outcomes: list[Postcondition] = Field(default_factory=list, max_length=24)
    fallback_candidate_ids: list[str] = Field(default_factory=list, max_length=8)
    confidence: float = Field(default=0.0, ge=0, le=1)
    safety_verified: bool = False
    rehearsal_required: bool = False


class OutcomeVerification(BaseModel):
    """Evidence-backed verdict that an interaction achieved its intent."""

    id: str = Field(default_factory=lambda: str(uuid4()), max_length=120)
    intent_id: str
    status: Literal["passed", "failed", "inconclusive"]
    expected: list[Postcondition] = Field(default_factory=list, max_length=24)
    observed_state_id: str | None = None
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    state_delta: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0, le=1)
    reason: str = Field(default="", max_length=500)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class InteractionRecoveryDecision(BaseModel):
    """A bounded recovery decision for an unexpected post-action state."""

    id: str = Field(default_factory=lambda: str(uuid4()), max_length=120)
    failed_attempt_id: str
    source_snapshot_id: str
    decision: Literal["reobserve", "retry_before_dispatch", "replace_suffix", "stop"]
    reason: str = Field(min_length=3, max_length=500)
    replacement_candidate_ids: list[str] = Field(default_factory=list, max_length=24)
    side_effect_dispatched: bool = False
    evidence_refs: list[str] = Field(default_factory=list, max_length=24)


class CapabilityProfile(BaseModel):
    """Capabilities proven for the current product/version, not assumed."""

    schema_version: int = Field(default=1, ge=1)
    product_fingerprint: str
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    capabilities: list[str] = Field(default_factory=list, max_length=64)
    verified_intents: list[str] = Field(default_factory=list, max_length=64)
    blocked_capabilities: list[str] = Field(default_factory=list, max_length=64)
    evidence_refs: list[str] = Field(default_factory=list, max_length=128)


class InteractionSnapshot(BaseModel):
    """Rich multimodal observation captured at an interaction boundary."""

    schema_version: int = Field(default=1, ge=1)
    id: str = Field(default_factory=lambda: str(uuid4()), max_length=120)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    url: str
    title: str = ""
    visible_text: str = Field(default="", max_length=12_000)
    visible_affordances: list[Affordance] = Field(default_factory=list, max_length=160)
    screenshot_ref: str | None = None
    dom_snapshot_ref: str | None = None
    accessibility_snapshot_ref: str | None = None
    viewport: Viewport | None = None
    scroll: dict[str, float] = Field(default_factory=dict)
    focused_target: Target | None = None
    loading: bool = False
    overlays: list[str] = Field(default_factory=list, max_length=24)
    iframe_count: int = Field(default=0, ge=0)
    shadow_root_count: int = Field(default=0, ge=0)
    visual_surface: Literal["dom", "canvas", "mixed", "unknown"] = "unknown"
    evidence_refs: list[str] = Field(default_factory=list, max_length=64)
    fingerprint: str | None = None


class InteractionTrace(BaseModel):
    """Canonical interaction evidence consumed by presentation and QA."""

    schema_version: int = Field(default=1, ge=1)
    run_id: str
    objective: str
    snapshots: list[StateSnapshot] = Field(default_factory=list, max_length=600)
    observations: list[InteractionSnapshot] = Field(default_factory=list, max_length=600)
    attempts: list[ActionAttempt] = Field(default_factory=list, max_length=600)
    verifications: list[OutcomeVerification] = Field(default_factory=list, max_length=600)
    recoveries: list[InteractionRecoveryDecision] = Field(default_factory=list, max_length=160)
    capability_profile: CapabilityProfile | None = None
    complete: bool = False

    @model_validator(mode="after")
    def complete_requires_verified_outcomes(self) -> InteractionTrace:
        if self.complete:
            # Failed attempts may be retained for audit when a bounded
            # recovery later proves the same intent.  Completion is decided by
            # the latest verdict per intent, not by erasing that history.
            latest: dict[str, OutcomeVerification] = {}
            for item in self.verifications:
                latest[item.intent_id] = item
            if any(item.status != "passed" for item in latest.values()):
                raise ValueError("a complete interaction trace has failed verifications unresolved")
        return self


class ProductContext(BaseModel):
    url: str
    title: str
    application_type: str
    authentication_state: Literal["unknown", "authenticated", "login_required"] = "unknown"
    visible_text: str = ""
    content_blocks: list[str] = Field(default_factory=list)
    relevant_routes: list[str] = Field(default_factory=list)
    navigation: list[ObservedElement] = Field(default_factory=list)
    elements: list[ObservedElement] = Field(default_factory=list)
    # Hints from previously verified runs. They are advisory only and are
    # populated exclusively after matching against the current DOM.
    successful_action_hints: list[dict[str, Any]] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0, le=1)
    objective: ObjectiveSpec | None = None
    page_knowledge: list[PageKnowledge] = Field(default_factory=list)
    feature_knowledge: list[FeatureKnowledge] = Field(default_factory=list)
    relationships: list[ProductRelationship] = Field(default_factory=list, max_length=80)
    candidate_demo_flows: list[CandidateDemoFlow] = Field(default_factory=list)
    # Concrete interaction capabilities are collected during reversible
    # exploration. They remain dictionaries here to preserve resumability of
    # older discovery artifacts; new writers use ``ActionCapability`` below.
    capabilities: list[dict[str, Any]] = Field(default_factory=list)
    capability_resolutions: list[CapabilityResolution] = Field(default_factory=list, max_length=80)
    # Discovery records only safe read-only navigation probes. A route visit is
    # never proof that an application workflow or side effect was executed.
    exploration_actions: list[str] = Field(default_factory=list)
    rejected_routes: list[str] = Field(default_factory=list)
    # The effective budget is evidence from discovery, not an implicit
    # runtime override. Full walkthroughs may expand it only after visible
    # primary navigation proves the default bound is insufficient.
    effective_discovery_budget: DiscoveryBudget | None = None


class ProductKnowledge(BaseModel):
    """Versioned, reusable product understanding grounded in page evidence."""

    project_id: str | None = None
    product_fingerprint: str
    version: str
    application_type: str
    navigation: list[ObservedElement] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    feature_map: list[FeatureKnowledge] = Field(default_factory=list)
    relationships: list[ProductRelationship] = Field(default_factory=list, max_length=120)
    page_knowledge: list[PageKnowledge] = Field(default_factory=list)
    workflow_knowledge: list[CandidateDemoFlow] = Field(default_factory=list)
    form_schemas: list[FormSchema] = Field(default_factory=list)
    capabilities: list[dict[str, Any]] = Field(default_factory=list)
    capability_resolutions: list[CapabilityResolution] = Field(default_factory=list, max_length=160)
    interaction_patterns: list[str] = Field(default_factory=list)
    known_blockers: list[str] = Field(default_factory=list)
    successful_actions: list[dict[str, Any]] = Field(default_factory=list)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_verified_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ScenePlan(BaseModel):
    """Typed boundary between a validated workflow and the journey director."""

    id: str
    story_phase: Literal[
        "context", "enter", "explain", "demonstrate", "verify", "transition", "close"
    ]
    page_url: str
    evidence_refs: list[str] = Field(min_length=1)
    operation_ids: list[str] = Field(default_factory=list)
    required_content: list[str] = Field(default_factory=list)
    required_content_groups: list[str] = Field(default_factory=list)
    covered_content_groups: list[str] = Field(default_factory=list)
    required_dwell_seconds: float = Field(default=2.0, ge=0.5, le=60)
    completion_criteria: list[str] = Field(min_length=1)
    action_classification: Literal["essential", "transitional", "dead_time"] = "essential"
    camera_state: dict[str, Any] = Field(default_factory=dict)
    cursor_behavior: dict[str, Any] = Field(default_factory=dict)
    scroll_behavior: dict[str, Any] = Field(default_factory=dict)
    caption_intent: str = "Explain visible evidence and its viewer value."
    transition: Literal["cut", "dissolve", "match_scroll", "hold"] = "cut"
    rejection_conditions: list[str] = Field(default_factory=list)


class ObjectiveRelationship(BaseModel):
    """A requested supporting relationship grounded during exploration."""

    source: str = Field(min_length=1, max_length=160)
    target: str = Field(min_length=1, max_length=160)
    relation: Literal["context_for", "configures", "depends_on", "proves"] = "context_for"
    required: bool = True


class ProductRelationship(BaseModel):
    """Observed relationship between product surfaces or concepts.

    Relationships are evidence, not executable routing rules. Explicit
    endpoints and citations let planning explain supporting pages generically.
    """

    source: str = Field(min_length=1, max_length=160)
    target: str = Field(min_length=1, max_length=160)
    relation: Literal[
        "context_for",
        "configures",
        "depends_on",
        "enables",
        "reveals",
        "proves",
        "related_to",
    ] = "related_to"
    source_url: str | None = None
    target_url: str | None = None
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(default=0.0, ge=0, le=1)


class ObjectiveSpec(BaseModel):
    schema_version: int = Field(default=1, ge=1)
    raw: str
    demo_type: Literal["full_walkthrough", "feature_walkthrough", "workflow_demo"] = "workflow_demo"
    video_type: Literal[
        "sales_demo",
        "feature_walkthrough",
        "full_tour",
        "onboarding",
        "training",
        "changelog",
        "support",
        "portfolio",
    ] = "feature_walkthrough"
    audience: str = "product prospect"
    audience_profile: AudienceProfile = Field(default_factory=AudienceProfile)
    purpose: str = Field(default="", max_length=240)
    tone: Literal["conversational", "concise", "technical", "persuasive"] = "conversational"
    depth: Literal["overview", "standard", "thorough"] = "standard"
    requested_features: list[str] = Field(default_factory=list)
    primary_entity: str | None = Field(default=None, max_length=160)
    supporting_relationships: list[ObjectiveRelationship] = Field(default_factory=list)
    must_show: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list, max_length=24)
    safe_action_policy: Literal["read_only", "authorized_side_effects"] = "read_only"
    permitted_mutations: list[Literal["create_isolated_record"]] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    minimum_duration_seconds: int = Field(default=60, ge=15, le=600)
    target_duration_seconds: int = Field(default=120, ge=30, le=600)
    maximum_duration_seconds: int = Field(default=180, ge=30, le=900)
    safe_actions_only: bool = True


class UnderstandingPreview(BaseModel):
    """Bounded, non-recording product understanding returned before generation.

    This contract is deliberately descriptive: it contains no selectors,
    credentials, or executable browser instructions.  A later generation run
    must still re-ground the evidence before planning and recording.
    """

    status: Literal["READY", "AUTH_REQUIRED", "BLOCKED", "FAILED"] = "READY"
    url: str
    prompt: str = ""
    suggested_prompt: str
    objective: ObjectiveSpec
    product_title: str = ""
    application_type: str = "web_application"
    product_fingerprint: str = ""
    knowledge_version: str = ""
    relevant_areas: list[str] = Field(default_factory=list, max_length=40)
    relationships: list[dict[str, Any]] = Field(default_factory=list, max_length=40)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    blockers: list[str] = Field(default_factory=list, max_length=20)
    evidence_refs: list[str] = Field(default_factory=list, max_length=80)
    pages_inspected: list[str] = Field(default_factory=list, max_length=20)
    cached: bool = False
    inspected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PageKnowledge(BaseModel):
    url: str
    title: str
    purpose: str
    visible_sections: list[str] = Field(default_factory=list)
    scroll_landmarks: list[str] = Field(default_factory=list)
    actionable_controls: list[str] = Field(default_factory=list)
    visible_facts: list[str] = Field(default_factory=list)
    loading_behavior: list[str] = Field(default_factory=list)
    route_state: str | None = None
    form_schemas: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    screenshot_evidence: str | None = None
    dom_evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    accessibility_evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    geometry_evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    fingerprint: str
    inspected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class FeatureKnowledge(BaseModel):
    name: str
    purpose: str
    entry_urls: list[str] = Field(default_factory=list)
    related_urls: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    expected_outcomes: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    relevance_score: float = Field(ge=0, le=1)


class CandidateDemoFlow(BaseModel):
    name: str
    # Evidence-only context discovered before production. These pages ground
    # a relationship or prerequisite without forcing a configuration tour
    # into a video whose viewer asked to see the operational experience.
    supporting_page_urls: list[str] = Field(default_factory=list)
    page_urls: list[str] = Field(default_factory=list)
    rationale: list[str] = Field(default_factory=list)
    expected_outcomes: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    estimated_duration_seconds: int = Field(default=90, ge=15, le=600)
    evidence_coverage: list[str] = Field(default_factory=list)
    semantic_steps: list[str] = Field(default_factory=list)
    rejected_reason: str | None = None
    score: float = Field(ge=0, le=1)


class DemoBrief(BaseModel):
    """Reviewable boundary between discovered product knowledge and a DemoPlan.

    The brief deliberately contains no selectors or executable instructions.
    It states *why* a page is in the story and which observed evidence must be
    carried forward.  Planning can therefore be inspected or rejected before
    a fresh production browser is opened.
    """

    objective: ObjectiveSpec
    audience: str = Field(min_length=1, max_length=160)
    duration_seconds: int = Field(ge=15, le=900)
    selected_flow: str = Field(min_length=1, max_length=180)
    included_pages: list[str] = Field(default_factory=list)
    supporting_pages: list[str] = Field(default_factory=list)
    page_story_roles: dict[str, str] = Field(default_factory=dict)
    required_outcomes: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class ExplorationReport(BaseModel):
    """Auditable result of discovery, before a production browser is opened."""

    pages_inspected: list[str] = Field(default_factory=list)
    actions_probed: list[str] = Field(default_factory=list)
    model_calls: int = Field(default=0, ge=0)
    blockers: list[str] = Field(default_factory=list)
    rejected_routes: list[str] = Field(default_factory=list)
    candidate_flow_names: list[str] = Field(default_factory=list)
    relationships: list[ProductRelationship] = Field(default_factory=list, max_length=80)
    stop_reason: str = ""


class FormField(BaseModel):
    name: str
    selector: str
    control_type: str
    required: bool = False
    options: list[str] = Field(default_factory=list)
    # Relationships are discovered from live control state (for example a
    # dependent select enabled after another field changes), never inferred
    # from a product name or route.
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    validation_messages: list[str] = Field(default_factory=list, max_length=8)
    confidence: float = Field(default=0.5, ge=0, le=1)


class FormSchema(BaseModel):
    source_url: str
    fields: list[FormField] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    # A form may visibly state that required inputs exist while a component
    # hides their native controls behind an unresolved widget.  That is not
    # permission to submit partial data during an authorised demo rehearsal.
    unresolved_required_fields: bool = False


class ActionCapability(BaseModel):
    """A reversible interaction discovered before a production run.

    This is deliberately generic: it records a form/modal/detail capability,
    never a Lead/Booking-specific implementation path.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: Literal["form", "modal", "detail", "navigation"]
    purpose: str = Field(min_length=1, max_length=300)
    source_url: str
    entry_target: Target
    form_schema: FormSchema | None = None
    submit_target: Target | None = None
    # A creation workflow is executable only when the success state has a
    # separate semantic witness (new row, detail title, toast, confirmation).
    # It may not reuse the submit button as proof.
    outcome_target: Target | None = None
    close_target: Target | None = None
    outcome_evidence: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    safe_to_probe: bool = True
    verified: bool = False


class WorkflowProposal(BaseModel):
    """LLM output is constrained to semantic steps and observed DOM evidence."""

    narrative_goal: str = Field(min_length=3, max_length=500)
    selected_workflow: str = Field(min_length=1, max_length=500)
    # Editorial full walkthroughs legitimately need more than twenty semantic
    # beats: Home content, then meaningful local exploration on each primary
    # page. The bound still prevents unbounded crawling while allowing a
    # two-to-three-minute story.
    steps: list[SemanticOperation] = Field(min_length=1, max_length=60)
    expected_outcomes: list[str] = Field(min_length=1, max_length=60)
    important_elements: list[str] = Field(default_factory=list, max_length=60)
    excluded_areas: list[str] = Field(default_factory=list, max_length=30)
    risk_flags: list[str] = Field(default_factory=list, max_length=30)


# These models intentionally reference later-defined evidence models.  Rebuild
# once the module has loaded so validation remains strict at runtime while the
# declarations stay grouped by architectural boundary above.
ProductContext.model_rebuild()
ProductKnowledge.model_rebuild()
StateSnapshot.model_rebuild()
VerificationResult.model_rebuild()
ActionAttempt.model_rebuild()
