"""Runtime interaction, trace, and verification contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from app.contracts.common import (
    OperationKind,
    Postcondition,
    Rect,
    Target,
    Viewport,
    ViewportDecision,
    WorkflowState,
)
from app.contracts.discovery import CapabilityResolution
from app.contracts.planning import SemanticOperation


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
            if self.destination is None:
                # Keep the invariant explicit for callers constructing an
                # instance through a non-standard Pydantic path.
                raise ValueError("drag gestures require a grounded destination")
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


class SurfaceState(BaseModel):
    """One visible interaction surface and its ownership relationship."""

    id: str = Field(min_length=1, max_length=160)
    kind: Literal[
        "page",
        "modal",
        "drawer",
        "popover",
        "menu",
        "listbox",
        "overlay",
        "iframe",
        "shadow_root",
        "canvas",
        "editor",
    ]
    label: str = Field(default="", max_length=240)
    owner_id: str | None = Field(default=None, max_length=160)
    geometry: Rect | None = None
    visible: bool = True
    blocking: bool = False
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)


class ControlDescriptor(BaseModel):
    """A behavior-classified control reconciled across observations."""

    stable_id: str = Field(min_length=8, max_length=160)
    surface_id: str = Field(min_length=1, max_length=160)
    role: str = Field(default="", max_length=80)
    label: str = Field(default="", max_length=240)
    tag: str = Field(default="", max_length=40)
    input_type: str = Field(default="", max_length=80)
    behavior_class: Literal[
        "text_input",
        "email_input",
        "phone_input",
        "multiline_input",
        "time_input",
        "native_select",
        "combobox",
        "autocomplete",
        "dependent_async",
        "radio",
        "checkbox",
        "native_date",
        "date_picker",
        "time_slot",
        "submit",
        "button",
        "tab",
        "accordion",
        "canvas_tool",
        "canvas_surface",
        "drag_target",
        "file_picker",
        "rich_text",
        "unknown",
    ] = "unknown"
    value: str | None = Field(default=None, max_length=500)
    options: list[str] = Field(default_factory=list, max_length=100)
    geometry: Rect | None = None
    visible: bool = True
    enabled: bool = True
    read_only: bool = False
    expanded: bool | None = None
    classification_confidence: float = Field(default=0.0, ge=0, le=1)
    selector_hints: list[str] = Field(default_factory=list, max_length=8)
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)


class ControlDependency(BaseModel):
    """An observed enabling/population relationship between two controls."""

    parent_control_id: str = Field(min_length=8, max_length=160)
    child_control_id: str = Field(min_length=8, max_length=160)
    condition: str = Field(min_length=3, max_length=300)
    effect: Literal["enabled", "visible", "populated", "filtered", "required"]
    confidence: float = Field(default=0.0, ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)


class PageState(BaseModel):
    """Normalized live-page state shared by discovery, execution, and QA."""

    id: str = Field(default_factory=lambda: str(uuid4()), max_length=160)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    url: str
    route_identity: str = Field(default="", max_length=500)
    title: str = Field(default="", max_length=300)
    viewport: Viewport | None = None
    scroll: dict[str, float] = Field(default_factory=dict)
    ready: bool = False
    loading: bool = False
    animating: bool = False
    focused_control_id: str | None = Field(default=None, max_length=160)
    visual_surface: Literal["dom", "canvas", "mixed", "unknown"] = "unknown"
    surfaces: list[SurfaceState] = Field(default_factory=list, max_length=80)
    controls: list[ControlDescriptor] = Field(default_factory=list, max_length=240)
    dependencies: list[ControlDependency] = Field(default_factory=list, max_length=160)
    screenshot_ref: str | None = None
    dom_snapshot_ref: str | None = None
    accessibility_snapshot_ref: str | None = None
    fingerprint: str | None = None
    evidence_refs: list[str] = Field(default_factory=list, max_length=64)


class StateTransition(BaseModel):
    """Evidence-backed difference between two normalized page states."""

    id: str = Field(default_factory=lambda: str(uuid4()), max_length=160)
    before_state_id: str = Field(min_length=1, max_length=160)
    after_state_id: str = Field(min_length=1, max_length=160)
    action_intent_id: str | None = Field(default=None, max_length=160)
    changed_control_ids: list[str] = Field(default_factory=list, max_length=160)
    added_control_ids: list[str] = Field(default_factory=list, max_length=160)
    removed_control_ids: list[str] = Field(default_factory=list, max_length=160)
    opened_surface_ids: list[str] = Field(default_factory=list, max_length=80)
    closed_surface_ids: list[str] = Field(default_factory=list, max_length=80)
    url_changed: bool = False
    confidence: float = Field(default=0.0, ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list, max_length=64)


class DiagramNode(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=160)
    kind: str = Field(default="component", max_length=80)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    visual_evidence_ref: str
    label_evidence_ref: str | None = None


class DiagramConnector(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    source_node_id: str = Field(min_length=1, max_length=160)
    target_node_id: str = Field(min_length=1, max_length=160)
    kind: str = Field(default="directed", max_length=80)
    visual_evidence_ref: str


class DiagramState(BaseModel):
    """Semantic editor result backed by committed surface-change evidence."""

    id: str = Field(default_factory=lambda: str(uuid4()), max_length=160)
    surface_control_id: str | None = Field(default=None, max_length=160)
    nodes: list[DiagramNode] = Field(default_factory=list, max_length=80)
    connectors: list[DiagramConnector] = Field(default_factory=list, max_length=160)
    labels_verified: bool = False
    topology_verified: bool = False
    screenshot_ref: str | None = None
    evidence_refs: list[str] = Field(default_factory=list, max_length=160)


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
    page_state: PageState | None = None


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


class SemanticMoment(BaseModel):
    """A stable editorial join point derived from verified browser evidence."""

    id: str = Field(min_length=1, max_length=120)
    kind: Literal[
        "context",
        "navigation",
        "inspection",
        "interaction",
        "reveal",
        "verification",
        "transition",
    ]
    event_ids: list[str] = Field(min_length=1, max_length=32)
    page_url: str | None = None
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    viewer_value: str = Field(min_length=3, max_length=420)
    start_at: datetime
    end_at: datetime
    verified: bool = False

    @model_validator(mode="after")
    def valid_interval(self) -> SemanticMoment:
        if self.end_at < self.start_at:
            raise ValueError("semantic moment end must not precede start")
        return self


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
    moments: list[SemanticMoment] = Field(default_factory=list, max_length=600)
    behavioral_states: list[PageState] = Field(default_factory=list, max_length=600)
    state_transitions: list[StateTransition] = Field(default_factory=list, max_length=600)
    diagram_states: list[DiagramState] = Field(default_factory=list, max_length=120)


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
