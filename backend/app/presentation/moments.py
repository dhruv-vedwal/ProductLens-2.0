"""Derive stable editorial moments from verified execution evidence."""

from __future__ import annotations

from datetime import timedelta

from app.contracts.models import DemoTrace, OperationKind, SemanticMoment


def _moment_kind(event_kind: OperationKind) -> str:
    if event_kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}:
        return "navigation"
    if event_kind in {
        OperationKind.FILL_TEXT,
        OperationKind.FILL_EMAIL,
        OperationKind.FILL_PHONE,
        OperationKind.SELECT_OPTION,
        OperationKind.SELECT_DATE,
        OperationKind.SELECT_DATE_RANGE,
        OperationKind.SUBMIT,
        OperationKind.CREATE_RECORD,
        OperationKind.DRAG,
        OperationKind.POINTER_SEQUENCE,
    }:
        return "interaction"
    if event_kind in {OperationKind.VERIFY_STATE, OperationKind.READ_VALUE}:
        return "verification"
    if event_kind in {OperationKind.SCROLL_TO, OperationKind.WAIT_FOR_STATE}:
        return "inspection"
    return "context"


def build_semantic_moments(trace: DemoTrace) -> list[SemanticMoment]:
    """Create one deterministic, evidence-linked moment per successful event.

    Keeping this derivation deterministic means captions, camera decisions and
    optional narration can all join on the same IDs without asking a model to
    invent a second timeline after capture.
    """
    moments: list[SemanticMoment] = []
    for index, event in enumerate(trace.events):
        if not event.success:
            continue
        start = event.action_at or event.occurred_at
        end = event.occurred_at + timedelta(milliseconds=max(1, event.duration_ms))
        evidence = [f"recovery:{index}" for index, _ in enumerate(event.recovery)]
        if event.page_url:
            evidence.append(f"page:{event.page_url}")
        if event.target and event.target.name:
            evidence.append(f"element:{event.target.name}")
        moments.append(
            SemanticMoment(
                id=f"moment-{event.id}",
                kind=_moment_kind(event.kind),
                event_ids=[event.id],
                page_url=event.page_url,
                evidence_refs=list(dict.fromkeys(evidence)),
                viewer_value=event.intent,
                start_at=start,
                end_at=max(start, end),
                verified=event.success,
            )
        )
    return moments


def sync_edl_from_moments(trace: DemoTrace) -> dict:
    """Return a durable, renderer-neutral edit decision list."""
    return {
        "schema_version": 1,
        "run_id": trace.run_id,
        "source": "verified_demo_trace",
        "moments": [
            {
                "id": moment.id,
                "kind": moment.kind,
                "event_ids": moment.event_ids,
                "page_url": moment.page_url,
                "evidence_refs": moment.evidence_refs,
                "viewer_value": moment.viewer_value,
                "start_at": moment.start_at.isoformat(),
                "end_at": moment.end_at.isoformat(),
                "verified": moment.verified,
                "action_classification": (
                    "transitional"
                    if moment.kind in {"navigation", "transition"}
                    else "essential"
                    if moment.verified
                    else "dead_time"
                ),
            }
            for moment in trace.moments
        ],
    }
