"""Planning and workflow contracts."""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts.common import OperationKind, Postcondition, Target, WorkflowState
from app.contracts.discovery import ObjectiveSpec


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
