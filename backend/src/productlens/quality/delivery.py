from __future__ import annotations

from typing import Any

from productlens.contracts.models import QualityReport


def delivery_report(
    *,
    artifacts: dict[str, bool],
    execution: dict[str, Any],
    story: dict[str, Any],
    video: dict[str, Any],
    synchronization: dict[str, Any] | None = None,
    visual_review: dict[str, Any] | None = None,
    viewport_score: float = 1.0,
) -> dict[str, Any]:
    synchronization = synchronization or {}
    visual_review = visual_review or {}
    missing = [name for name, present in artifacts.items() if not present]
    hard_failures = (
        missing
        + list(execution.get("hard_failures", []))
        + list(story.get("hard_failures", []))
        + list(video.get("hard_failures", []))
        + list(synchronization.get("hard_failures", []))
        + list(visual_review.get("hard_failures", []))
    )
    # Keep the delivery verdict explainable.  A flat failure list made it
    # impossible for the retry coordinator to prove which layer owned a
    # rejection, especially when the same symptom appeared in visual and
    # synchronization reports.  Preserve first-seen ownership and the raw
    # layer evidence without exposing provider secrets.
    layer_inputs = {
        "delivery": missing,
        "execution": list(execution.get("hard_failures", [])),
        "story": list(story.get("hard_failures", [])),
        "video": list(video.get("hard_failures", [])),
        "synchronization": list(synchronization.get("hard_failures", [])),
        "visual_review": list(visual_review.get("hard_failures", [])),
    }
    owner_by_failure: dict[str, str] = {}
    for layer, failures in layer_inputs.items():
        for failure in failures:
            owner_by_failure.setdefault(str(failure), layer)
    hard_failures = list(dict.fromkeys(str(item) for item in hard_failures))
    quality = QualityReport(
        execution_score=float(execution.get("execution_score", 0)),
        workflow_score=float(execution.get("workflow_score", execution.get("execution_score", 0))),
        visual_score=float(video.get("visual_score", 0)),
        story_score=float(story.get("story_score", 0)),
        audio_score=float(synchronization.get("audio_score", 1)),
        synchronization_score=float(synchronization.get("synchronization_score", 1)),
        viewport_score=viewport_score,
        overall_score=0.0 if hard_failures else 1.0,
        hard_failures=hard_failures,
        layer_evidence={
            layer: [str(item) for item in failures]
            for layer, failures in layer_inputs.items()
            if failures
        },
        owner_by_failure=owner_by_failure,
        warnings=[
            *story.get("warnings", []), *video.get("warnings", []),
            *synchronization.get("warnings", []), *visual_review.get("warnings", []),
        ],
    )
    return {
        "deliverable": not hard_failures,
        **quality.model_dump(mode="json"),
        "artifacts": artifacts,
        "visual_review": visual_review,
        "scores": {
            "execution": quality.execution_score,
            "story": quality.story_score,
            "visual": quality.visual_score,
        },
    }
