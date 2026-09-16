"""Discovery and product-understanding contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from app.contracts.common import AudienceProfile, Target


class DiscoveryBudget(BaseModel):
    max_time_seconds: int = Field(default=60, ge=1)
    max_pages: int = Field(default=6, ge=1)
    max_actions: int = Field(default=24, ge=1)
    max_model_calls: int = Field(default=3, ge=0)
    max_depth: int = Field(default=2, ge=0)


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
