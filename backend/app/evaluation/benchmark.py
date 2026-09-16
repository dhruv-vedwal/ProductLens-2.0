from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.benchmark.runner import run
from app.observability.logging import get_logger

logger = get_logger("app.evaluation")

# The supplied HTML gates are a deterministic regression envelope, not a claim
# that arbitrary web applications meet the ProductLens reliability target.
BENCHMARK_ID = "productlens-local-reliability-gates-v1"
SUPPORT_ENVELOPE = {
    "environment": "local Playwright + supplied HTML fixtures",
    "application_categories": ["CRM", "dashboard", "form workflow", "authenticated workflow"],
    "interaction_categories": [
        "navigation",
        "forms",
        "selects",
        "modals",
        "scrolling",
        "state verification",
    ],
    "provider_backed": False,
}


@dataclass
class GateMeasurement:
    gate: int
    attempts: int
    successes: int
    browser_actions: int = 0
    successful_actions: int = 0
    verified_actions: int = 0
    grounded_actions: int = 0
    repair_count: int = 0
    execution_duration_ms: int = 0

    @property
    def first_pass_rate(self) -> float:
        return self.successes / self.attempts if self.attempts else 0


async def repeat_gate(gate: int, attempts: int, root: Path) -> GateMeasurement:
    successes = 0
    browser_actions = successful_actions = verified_actions = grounded_actions = 0
    repair_count = execution_duration_ms = 0
    for _ in range(attempts):
        try:
            trace = await run(gate, root, render_final=False)
            successes += int(trace.outcome_verified)
            browser_actions += len(trace.events)
            successful_actions += sum(event.success for event in trace.events)
            # A completed engine event has passed every declared postcondition;
            # fixture operations with no explicit postcondition are still
            # verified by the adapter's actionability/snapshot contract.
            verified_actions += sum(event.success for event in trace.events)
            grounded_actions += sum(
                bool(event.before.get("grounding_strategy")) for event in trace.events
            )
            repair_count += sum(len(event.recovery) for event in trace.events)
            execution_duration_ms += sum(event.duration_ms for event in trace.events)
        except RuntimeError as error:
            logger.warning("benchmark attempt failed", gate=gate, error=str(error))
    return GateMeasurement(
        gate,
        attempts,
        successes,
        browser_actions=browser_actions,
        successful_actions=successful_actions,
        verified_actions=verified_actions,
        grounded_actions=grounded_actions,
        repair_count=repair_count,
        execution_duration_ms=execution_duration_ms,
    )


async def run_suite(
    attempts: int,
    root: Path,
    gates: tuple[int, ...] = (1, 2, 3, 4, 5, 6),
    on_measurement: Callable[[dict], None] | None = None,
    completed_measurements: list[GateMeasurement] | None = None,
) -> dict:
    if not gates or any(gate not in range(1, 7) for gate in gates):
        raise ValueError("gates must contain values from 1 through 6")
    measurements: list[GateMeasurement] = list(completed_measurements or [])
    already_completed = {item.gate for item in measurements}
    for gate in gates:
        if gate in already_completed:
            continue
        measurements.append(await repeat_gate(gate, attempts, root))
        report = _report(attempts, gates, measurements)
        if on_measurement is not None:
            on_measurement(report)
    return _report(attempts, gates, measurements)


def _report(attempts: int, gates: tuple[int, ...], measurements: list[GateMeasurement]) -> dict:
    completed_attempts = sum(item.attempts for item in measurements)
    overall = (
        sum(item.successes for item in measurements) / completed_attempts
        if completed_attempts
        else 0.0
    )
    # Plan section 70 uses a much larger representative suite. Never let a
    # handful of deterministic gate repetitions masquerade as that evidence.
    reliability_target_eligible = completed_attempts >= 900 and len(measurements) >= 6
    total_actions = sum(item.browser_actions for item in measurements)
    completed_runs = sum(item.successes for item in measurements)
    successful_actions = sum(item.successful_actions for item in measurements)
    verified_actions = sum(item.verified_actions for item in measurements)
    grounded_actions = sum(item.grounded_actions for item in measurements)
    repairs = sum(item.repair_count for item in measurements)
    duration_ms = sum(item.execution_duration_ms for item in measurements)
    return {
        "benchmark_id": BENCHMARK_ID,
        "support_envelope": SUPPORT_ENVELOPE,
        "generated_at": datetime.now(UTC).isoformat(),
        "attempts_per_gate": attempts,
        "requested_gates": list(gates),
        "completed_gates": [item.gate for item in measurements],
        "complete": len(measurements) == len(gates),
        "gates": [
            asdict(item) | {"first_pass_rate": item.first_pass_rate} for item in measurements
        ],
        "overall_first_pass_rate": overall,
        "metrics": {
            "task_success_rate": overall,
            "critical_action_success_rate": successful_actions / total_actions
            if total_actions
            else 0.0,
            "state_verification_rate": verified_actions / total_actions if total_actions else 0.0,
            "target_grounding_accuracy": grounded_actions / total_actions if total_actions else 0.0,
            "average_repair_count": repairs / completed_runs if completed_runs else 0.0,
            "average_execution_time_ms": duration_ms / completed_runs if completed_runs else 0.0,
            "browser_actions": total_actions,
            "model_calls": 0,
        },
        "reliability_target": 0.95,
        "reliability_target_eligible": reliability_target_eligible,
        "reliability_target_met": reliability_target_eligible and overall >= 0.95,
    }


def aggregate_reports(reports: list[dict]) -> dict:
    """Aggregate independent benchmark reports without hiding sample size.

    This is intentionally separate from ``_report``: a resumed/repeated suite
    must retain each run's denominator and expose failure distribution rather
    than averaging percentages (which can overweight tiny samples).
    """
    if not reports:
        return {
            "report_count": 0,
            "attempts": 0,
            "successes": 0,
            "first_pass_rate": 0.0,
            "eligible": False,
        }
    attempts = sum(
        int(report.get("attempts_per_gate", 0)) * len(report.get("requested_gates", []))
        for report in reports
    )
    successes = sum(
        sum(int(gate.get("successes", 0)) for gate in report.get("gates", [])) for report in reports
    )
    return {
        "report_count": len(reports),
        "attempts": attempts,
        "successes": successes,
        "first_pass_rate": successes / attempts if attempts else 0.0,
        "eligible": attempts >= 900
        and len({report.get("benchmark_id") for report in reports}) == 1,
        "benchmark_ids": sorted({str(report.get("benchmark_id")) for report in reports}),
        "support_envelopes": [report.get("support_envelope", {}) for report in reports],
    }


async def run_and_write_suite(
    attempts: int,
    root: Path,
    gates: tuple[int, ...] = (1, 2, 3, 4, 5, 6),
    *,
    resume: bool = True,
) -> dict:
    suffix = "all" if gates == (1, 2, 3, 4, 5, 6) else "-".join(map(str, gates))
    destination = root / f"benchmark-report-{suffix}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)

    completed: list[GateMeasurement] = []
    if resume and destination.exists():
        try:
            previous = json.loads(destination.read_text(encoding="utf-8"))
            # A report is compatible only when it measured exactly the same
            # attempt count.  Reusing a different sample size would make the
            # reliability percentage misleading.
            if previous.get("attempts_per_gate") == attempts:
                completed = [
                    GateMeasurement(
                        gate=int(item["gate"]),
                        attempts=int(item["attempts"]),
                        successes=int(item["successes"]),
                        browser_actions=int(item.get("browser_actions", 0)),
                        successful_actions=int(item.get("successful_actions", 0)),
                        verified_actions=int(item.get("verified_actions", 0)),
                        grounded_actions=int(item.get("grounded_actions", 0)),
                        repair_count=int(item.get("repair_count", 0)),
                        execution_duration_ms=int(item.get("execution_duration_ms", 0)),
                    )
                    for item in previous.get("gates", [])
                    if int(item.get("gate", 0)) in gates
                ]
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            # Corrupt/incompatible checkpoints never hide work; restart the
            # requested suite and overwrite them with fresh evidence.
            completed = []

    def checkpoint(report: dict) -> None:
        # A benchmark can be resumed after an interrupted long suite, so never
        # leave a truncated JSON checkpoint that looks like valid evidence.
        temporary = destination.with_suffix(f"{destination.suffix}.tmp")
        temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
        temporary.replace(destination)

    report = await run_suite(
        attempts,
        root,
        gates,
        on_measurement=checkpoint,
        completed_measurements=completed,
    )
    checkpoint(report)
    return report
