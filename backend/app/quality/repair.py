"""Classify failures so retries target the broken layer instead of replaying a demo."""

from __future__ import annotations

from app.contracts.models import RepairDecision


def classify_repair(hard_failures: list[str]) -> RepairDecision:
    if not hard_failures:
        return RepairDecision(category="none", action="fail")
    failures = set(hard_failures)
    external_markers = (
        "AUTH_REQUIRED",
        "CREDENTIAL",
        "CAPTCHA",
        "ACCOUNT_LOCKED",
        "PERMISSION_DENIED",
    )
    if any(any(marker in item for marker in external_markers) for item in failures):
        return RepairDecision(
            category="external_input",
            action="needs_input",
            reasons=sorted(failures),
            retry_boundary="user-or-provider-input",
        )
    if any(
        marker in item
        for item in failures
        for marker in ("MULTIMODAL_REVIEW_UNAVAILABLE", "SEMANTIC_VISUAL_REVIEW")
    ):
        return RepairDecision(
            category="visual_review",
            action="provider_retry",
            reasons=sorted(failures),
            retry_from_stage="VIDEO_QA",
            retry_boundary="multimodal-review",
        )
    discovery_markers = (
        "DISCOVERY",
        "STALE_KNOWLEDGE",
        "RELEVANCE",
        "NO_VIABLE_CANDIDATE",
        "BEHAVIOR_OBSERVATION_REQUIRED",
        "REHEARSAL_CAPABILITY_UNAVAILABLE",
        "REHEARSAL_CAPABILITY_INVALID",
        "REHEARSAL_OUTCOME_UNVERIFIED",
        "CANDIDATE_FLOW_SCOPE",
    )
    if any(any(marker in item for marker in discovery_markers) for item in failures):
        return RepairDecision(
            category="discovery",
            action="targeted_reexecution",
            reasons=sorted(failures),
            retry_from_stage="DISCOVERY",
            retry_boundary="targeted-exploration",
        )
    workflow_markers = (
        "WORKFLOW_VALIDATION",
        "POSTCONDITION",
        "CERTIFIED_OUTCOME",
        "UNSAFE_SIDE_EFFECT",
        "MISSING_SELECTED",
    )
    if any(any(marker in item for marker in workflow_markers) for item in failures):
        return RepairDecision(
            category="workflow",
            action="targeted_reexecution",
            reasons=sorted(failures),
            retry_from_stage="PLANNING",
            retry_boundary="candidate-flow-validation",
        )
    if any("PROVIDER" in item or "NARRATION_MISSING" in item for item in failures):
        return RepairDecision(
            # Provider failures can arise in discovery, planning, or narration;
            # the worker supplies its active durable stage when it schedules a
            # provider retry instead of guessing here.
            category="provider",
            action="provider_retry",
            reasons=sorted(failures),
            retry_boundary="provider-call",
        )
    editorial_markers = (
        "EDITORIAL",
        "NARRATION",
        "GENERIC_ROUTE",
        "UNSUPPORTED_CLAIM",
        "UNSUPPORTED_OR_WRONG_SCENE",
    )
    if any(any(marker in item for marker in editorial_markers) for item in failures):
        return RepairDecision(
            category="narration",
            action="regenerate_narration",
            reasons=sorted(failures),
            retry_from_stage="NARRATION",
        )
    edl_markers = (
        "SYNC",
        "EDL",
        "CAPTION",
        "CURSOR",
        "CAMERA",
        "CROP",
        "ZOOM",
        "SCROLL",
        "COLLISION",
    )
    if any(any(marker in item for marker in edl_markers) for item in failures):
        return RepairDecision(
            category="presentation",
            action="re_render",
            reasons=sorted(failures),
            retry_from_stage="NARRATION",
            retry_boundary="sync-edl",
        )
    presentation_markers = (
        "RENDER",
        "VIDEO",
        "BLACK",
        "FROZEN",
        "BITRATE",
        "FRAME_RATE",
    )
    if any(any(marker in item for marker in presentation_markers) for item in failures):
        return RepairDecision(
            category="video_qa" if any("QA" in item for item in failures) else "presentation",
            action="re_render",
            reasons=sorted(failures),
            retry_from_stage="RENDER",
            retry_boundary="rendered-trace",
        )
    planning_markers = (
        "PAGE_NOT_EXPLORED",
        "NAVIGATED_PAGE",
        "RETURNED_TO",
        "DIRECT_ROUTE",
        "JOURNEY_HAS_NAVIGATION",
        "CAPTURE_DURATION",
    )
    if any(any(marker in item for marker in planning_markers) for item in failures):
        return RepairDecision(
            category="execution",
            action="targeted_reexecution",
            reasons=sorted(failures),
            retry_from_stage="PLANNING",
        )
    execution_markers = (
        "TRACE",
        "OBJECTIVE_OUTCOME",
        "WORKFLOW_OUTCOME",
        "EXECUTION_",
        "NAVIGATED_PAGE",
    )
    if any(any(marker in item for marker in execution_markers) for item in failures):
        return RepairDecision(
            category="execution",
            action="targeted_reexecution",
            reasons=sorted(failures),
            retry_from_stage="EXECUTION",
        )
    return RepairDecision(category="internal", action="fail", reasons=sorted(failures))
