"""Trace-only interaction harness boundary.

This module deliberately does not narrate, render, or repair a browser run. It
validates the authoritative DemoTrace produced by the shared execution engine
and promotes it only when every observed interaction and its final outcome are
verified. Presentation code consumes the resulting certificate; it cannot
turn an incomplete trace into a success.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.artifacts.store import RunArtifacts, materialize_trace_lifecycle
from app.contracts import (
    DemoTrace,
    HarnessFailure,
    HarnessFailureCode,
    HarnessOutcome,
    HarnessStatus,
    InteractionHarnessRequest,
    InteractionHarnessResult,
)


def objective_fingerprint(request: InteractionHarnessRequest) -> str:
    """Return a stable, secret-free identity for an idempotent harness run."""

    payload = {
        "url": str(request.url),
        "objective": request.objective.strip(),
        "mode": request.mode.value,
        "audience": request.audience,
        "side_effect_policy": request.side_effect_policy,
        "required_outcomes": sorted(request.required_outcomes),
        "excluded_actions": sorted(request.excluded_actions),
        "limits": request.limits.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class HarnessValidation:
    result: InteractionHarnessResult
    trace: DemoTrace


def classify_harness_exception(error: BaseException) -> tuple[HarnessFailureCode, str]:
    """Map provider/executor errors to the stable harness failure taxonomy."""
    message = str(error).casefold()
    rules: tuple[tuple[tuple[str, ...], HarnessFailureCode, str], ...] = (
        (("captcha", "recaptcha", "hcaptcha"), HarnessFailureCode.CAPTCHA_BLOCKED, "provider"),
        (("page.goto", "net::", "connection refused", "navigation failed"), HarnessFailureCode.URL_LOAD_FAILED, "execution"),
        (("otp", "one-time", "two-factor", "2fa"), HarnessFailureCode.OTP_REQUIRED, "provider"),
        (("auth", "login", "credential", "unauthorized", "forbidden"), HarnessFailureCode.AUTH_REQUIRED, "execution"),
        (("timeout", "timed out", "deadline"), HarnessFailureCode.PROVIDER_TIMEOUT, "provider"),
        (("budget", "maximum steps", "max pages"), HarnessFailureCode.STEP_BUDGET_EXCEEDED, "budget"),
        (("ambiguous", "multiple targets"), HarnessFailureCode.TARGET_AMBIGUOUS, "execution"),
        (("occluded", "covered"), HarnessFailureCode.TARGET_OCCLUDED, "execution"),
        (("not visible", "target not found", "no safe"), HarnessFailureCode.TARGET_NOT_VISIBLE, "execution"),
        (("unsafe", "side effect"), HarnessFailureCode.UNSAFE_ACTION, "execution"),
        (("postcondition", "outcome", "verification"), HarnessFailureCode.OUTCOME_UNVERIFIED, "verification"),
    )
    for needles, code, owner in rules:
        if any(needle in message for needle in needles):
            return code, owner
    return HarnessFailureCode.INTERNAL_ERROR, "internal"


def validation_for_exception(
    request: InteractionHarnessRequest,
    trace: DemoTrace | None,
    error: BaseException,
    *,
    run_id: str | None = None,
) -> HarnessValidation:
    """Create a typed blocked result when execution fails before certification."""
    if trace is None:
        now = datetime.now(UTC)
        trace = DemoTrace(
            run_id=run_id or request.request_id or "unknown",
            objective=request.objective,
            started_at=now,
            completed_at=now,
            errors=[{"type": type(error).__name__, "message": str(error)[:500]}],
        )
    code, owner = classify_harness_exception(error)
    return _failure(
        run_id=trace.run_id,
        fingerprint=objective_fingerprint(request),
        code=code,
        message=str(error) or code.value,
        owning_layer=owner,
        trace=trace,
        dispatched=any(event.action_at is not None for event in trace.events),
    )


def _failure(
    *,
    run_id: str,
    fingerprint: str,
    code: HarnessFailureCode,
    message: str,
    owning_layer: str,
    trace: DemoTrace,
    event_id: str | None = None,
    dispatched: bool = False,
    evidence_ids: list[str] | None = None,
) -> HarnessValidation:
    failure = HarnessFailure(
        code=code,
        message=message[:500],
        owning_layer=owning_layer,  # type: ignore[arg-type]
        last_verified_event_id=event_id,
        safe_retry_boundary=("re-observe" if dispatched else "last verified observation"),
        side_effect_dispatched=dispatched,
        evidence_ids=list(evidence_ids or []),
    )
    return HarnessValidation(
        result=InteractionHarnessResult(
            run_id=run_id,
            status=HarnessStatus.BLOCKED,
            objective_fingerprint=fingerprint,
            outcome=HarnessOutcome(verified=False),
            metrics={"events": len(trace.events), "verified_events": 0},
            failure=failure,
        ),
        trace=trace,
    )


def validate_trace(
    request: InteractionHarnessRequest,
    trace: DemoTrace,
    *,
    trace_artifact: str = "execution/trace.json",
) -> HarnessValidation:
    """Validate a trace without consulting narration, rendering, or an LLM.

    The validator intentionally uses conservative gates. A trace can be useful
    diagnostic evidence without being promotable to presentation. In particular,
    dispatched actions with no verification and any failed event are blockers.
    """

    trace = materialize_trace_lifecycle(trace)
    fingerprint = objective_fingerprint(request)
    if not trace.events:
        return _failure(
            run_id=trace.run_id,
            fingerprint=fingerprint,
            code=HarnessFailureCode.TRACE_INCOMPLETE,
            message="The interaction produced no browser events",
            owning_layer="execution",
            trace=trace,
        )
    failed_events = [event for event in trace.events if not event.success]
    if failed_events:
        event = failed_events[-1]
        return _failure(
            run_id=trace.run_id,
            fingerprint=fingerprint,
            code=HarnessFailureCode.POSTCONDITION_FAILED,
            message=f"Interaction event {event.operation_id} did not complete successfully",
            owning_layer="verification",
            trace=trace,
            event_id=event.id,
            dispatched=event.action_at is not None,
            evidence_ids=[f"trace:event:{event.id}"],
        )
    unresolved = [
        item
        for item in trace.verification_results
        if item.status in {"failed", "inconclusive"}
    ]
    if unresolved:
        item = unresolved[-1]
        return _failure(
            run_id=trace.run_id,
            fingerprint=fingerprint,
            code=HarnessFailureCode.OUTCOME_UNVERIFIED,
            message=f"Verification for intent {item.intent_id} is {item.status}",
            owning_layer="verification",
            trace=trace,
            evidence_ids=list(item.evidence_refs),
        )
    failed_attempts = [
        attempt
        for attempt in trace.action_attempts
        if attempt.verification is not None
        and attempt.verification.status != "passed"
    ]
    if failed_attempts:
        attempt = failed_attempts[-1]
        return _failure(
            run_id=trace.run_id,
            fingerprint=fingerprint,
            code=HarnessFailureCode.OUTCOME_UNVERIFIED,
            message=f"Action {attempt.id} does not have a passed verification",
            owning_layer="verification",
            trace=trace,
            event_id=attempt.id,
        )
    dispatched_without_verification = [
        attempt
        for attempt in trace.action_attempts
        if attempt.dispatched and attempt.verification is None
    ]
    if dispatched_without_verification:
        attempt = dispatched_without_verification[-1]
        return _failure(
            run_id=trace.run_id,
            fingerprint=fingerprint,
            code=HarnessFailureCode.TRACE_INCOMPLETE,
            message=f"Dispatched action {attempt.id} has no verification record",
            owning_layer="execution",
            trace=trace,
            dispatched=True,
        )
    final_state = getattr(trace.final_state, "value", trace.final_state)
    if not trace.outcome_verified or final_state != "COMPLETE":
        return _failure(
            run_id=trace.run_id,
            fingerprint=fingerprint,
            code=HarnessFailureCode.OUTCOME_UNVERIFIED,
            message="The browser trace does not contain a verified terminal outcome",
            owning_layer="verification",
            trace=trace,
        )
    if request.required_outcomes:
        evidence_text = _trace_evidence_text(trace)
        evidence_terms = _outcome_terms(evidence_text)
        for required in request.required_outcomes:
            terms = _outcome_terms(required)
            # A two-term outcome such as "connected diagram" must have both
            # semantic terms in an observed witness; a one-term outcome can be
            # proven by that single domain term plus the normal verification
            # record. Generic action success is never enough.
            minimum = min(2, len(terms))
            if not terms or len(terms & evidence_terms) < minimum:
                return _failure(
                    run_id=trace.run_id,
                    fingerprint=fingerprint,
                    code=HarnessFailureCode.OUTCOME_UNVERIFIED,
                    message=f"Required outcome is not grounded in an independent witness: {required}",
                    owning_layer="verification",
                    trace=trace,
                    evidence_ids=[f"trace:event:{event.id}" for event in trace.events],
                )
    evidence_ids = [
        f"trace:event:{event.id}"
        for event in trace.events
        if event.success
    ]
    result = InteractionHarnessResult(
        run_id=trace.run_id,
        status=HarnessStatus.VERIFIED,
        objective_fingerprint=fingerprint,
        trace_artifact=trace_artifact,
        outcome=HarnessOutcome(
            verified=True,
            evidence_ids=evidence_ids,
            summary="The requested interaction completed with verified browser evidence",
        ),
        metrics={
            "events": len(trace.events),
            "verified_events": len([event for event in trace.events if event.success]),
            "pages": len({event.page_url for event in trace.events if event.page_url}),
            "mode": request.mode.value,
        },
    )
    return HarnessValidation(result=result, trace=trace)


def persist_validation(
    artifacts: RunArtifacts,
    request: InteractionHarnessRequest,
    validation: HarnessValidation,
) -> None:
    """Persist the certificate and all trace lifecycle projections atomically."""

    artifacts.write_json("harness/request.json", request.model_dump(mode="json"))
    artifacts.save_trace(validation.trace)
    _persist_harness_artifact_contract(artifacts, request, validation)
    artifacts.write_json("harness/result.json", validation.result.model_dump(mode="json"))
    if validation.result.failure is not None:
        artifacts.write_json(
            "harness/failure.json", validation.result.failure.model_dump(mode="json")
        )
    # The manifest is written last so operators can verify the complete
    # capability result, including its certificate or failure report.
    artifacts.write_manifest()


_SECRET_KEY = re.compile(
    r"(?:password|passcode|token|secret|api[_ -]?key|authorization|cookie|otp)",
    re.IGNORECASE,
)
_SECRET_TARGET = re.compile(
    r"(?:password|passcode|token|secret|api[_ -]?key|authorization|one[- ]?time|otp)",
    re.IGNORECASE,
)
_OUTCOME_STOPWORDS = {
    "a",
    "an",
    "and",
    "be",
    "has",
    "is",
    "it",
    "of",
    "the",
    "to",
    "with",
}


def _redact_artifact_value(value: Any, *, key: str = "", target_hint: str = "") -> Any:
    """Redact credential-shaped values before a harness artifact is persisted."""
    if _SECRET_KEY.search(key) or _SECRET_TARGET.search(target_hint) and key in {
        "value",
        "text",
        "expected",
    }:
        return "[REDACTED]"
    if isinstance(value, dict):
        hint = target_hint
        target = value.get("target")
        if isinstance(target, dict):
            hint = " ".join(
                str(target.get(item) or "") for item in ("name", "label", "role")
            )
        return {
            str(item): _redact_artifact_value(child, key=str(item), target_hint=hint)
            for item, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_artifact_value(item, key=key, target_hint=target_hint) for item in value]
    return value


def _persist_harness_artifact_contract(
    artifacts: RunArtifacts,
    request: InteractionHarnessRequest,
    validation: HarnessValidation,
) -> None:
    """Project the canonical trace into the standalone harness artifact layout."""
    trace = validation.trace
    artifacts.write_json(
        "interaction-run.json",
        {
            "schema_version": 1,
            "run_id": trace.run_id,
            "objective_fingerprint": objective_fingerprint(request),
            "mode": request.mode.value,
            "status": validation.result.status.value,
            "started_at": trace.started_at.isoformat(),
            "completed_at": trace.completed_at.isoformat() if trace.completed_at else None,
            "trace_artifact": "execution/trace.json",
            "outcome_verified": trace.outcome_verified,
        },
    )
    actions = [
        _redact_artifact_value(item.model_dump(mode="json"))
        for item in trace.action_attempts
    ]
    artifacts.write_text(
        "actions.jsonl",
        "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in actions),
    )
    artifacts.write_json(
        "state-transitions.json",
        [item.model_dump(mode="json") for item in trace.state_transitions],
    )
    # Runtime capture stores evidence beside the execution trace. The harness
    # contract deliberately exposes a stable top-level location so consumers
    # do not need to know which browser provider produced it.
    source_observations = artifacts.root / "execution" / "observations"
    destination = artifacts.root / "observations"
    screenshots = destination / "screenshots"
    accessibility = destination / "accessibility"
    screenshots.mkdir(parents=True, exist_ok=True)
    accessibility.mkdir(parents=True, exist_ok=True)
    if source_observations.is_dir():
        for source in source_observations.iterdir():
            if not source.is_file():
                continue
            if source.suffix.lower() == ".png":
                shutil.copy2(source, screenshots / source.name)
            elif source.suffix.lower() == ".json":
                shutil.copy2(source, destination / source.name)
                # Accessibility is captured in the same redacted evidence
                # envelope; retain an explicit stable reference for clients
                # that expect a dedicated accessibility directory.
                shutil.copy2(source, accessibility / source.name)


def _outcome_terms(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", value.casefold())
        if token not in _OUTCOME_STOPWORDS
    }


def _trace_evidence_text(trace: DemoTrace) -> str:
    """Collect only persisted witness text, not selectors or coordinates."""
    chunks: list[str] = []
    for event in trace.events:
        chunks.extend(
            [
                event.intent,
                *(str(item.expected) for item in event.postconditions),
                str(event.after.get("verified_outcome") or ""),
                str(event.after.get("interaction_evidence") or ""),
            ]
        )
    for state in trace.diagram_states:
        chunks.extend(
            [
                *(node.label for node in state.nodes),
                *(connector.kind or "connector" for connector in state.connectors),
                # These are semantic witness terms, not a claim derived from
                # a click.  A DiagramState exists only after a committed
                # surface change with screenshot evidence; ``visible`` is
                # therefore tied to the same state witness.
                "diagram" if state.nodes else "",
                "visible" if state.nodes else "",
                "connected" if state.topology_verified else "",
            ]
        )
    return " ".join(chunks)


def load_and_validate(
    artifacts: RunArtifacts,
    request: InteractionHarnessRequest,
    *,
    trace_path: Path | None = None,
) -> HarnessValidation:
    """Load a previously captured trace and apply the same promotion gate."""

    path = trace_path or (artifacts.execution / "trace.json")
    trace = DemoTrace.model_validate(json.loads(path.read_text(encoding="utf-8")))
    validation = validate_trace(request, trace)
    persist_validation(artifacts, request, validation)
    return validation
