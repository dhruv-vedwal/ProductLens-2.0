"""Classify failures so retries target the broken layer instead of replaying a demo."""

from __future__ import annotations

from productlens.contracts.models import RepairDecision


def classify_repair(hard_failures: list[str]) -> RepairDecision:
    if not hard_failures:
        return RepairDecision(category="none", action="fail")
    failures = set(hard_failures)
    discovery_markers = ("DISCOVERY", "STALE_KNOWLEDGE", "RELEVANCE", "NO_VIABLE_CANDIDATE")
    if any(any(marker in item for marker in discovery_markers) for item in failures):
        return RepairDecision(
            category="discovery",
            action="targeted_reexecution",
            reasons=sorted(failures),
            retry_from_stage="PLANNING",
            retry_boundary="targeted-exploration",
        )
    workflow_markers = (
        "WORKFLOW_VALIDATION",
        "POSTCONDITION",
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
    presentation_markers = (
        "RENDER",
        "VIDEO",
        "CAPTION",
        "CURSOR",
        "BLACK",
        "FROZEN",
        "BITRATE",
        "FRAME_RATE",
        "CROP",
        "ZOOM",
    )
    if any(any(marker in item for marker in presentation_markers) for item in failures):
        return RepairDecision(
            category="video_qa" if any("QA" in item for item in failures) else "presentation",
            action="re_render",
            reasons=sorted(failures),
            retry_from_stage="RENDERING",
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
        "WORKFLOW",
        "OBJECTIVE",
        "TARGET",
        "OUTCOME",
        "EXECUTION",
        "NAVIGATED_PAGE",
    )
    if any(any(marker in item for marker in execution_markers) for item in failures):
        return RepairDecision(
            category="execution",
            action="targeted_reexecution",
            reasons=sorted(failures),
            retry_from_stage="PRODUCTION_EXECUTION",
        )
    return RepairDecision(category="internal", action="fail", reasons=sorted(failures))
