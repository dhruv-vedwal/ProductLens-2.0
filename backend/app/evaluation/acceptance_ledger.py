"""Versioned consecutive-run ledger for locked live acceptance scenarios.

Historical diagnostic artifacts remain regression evidence. They must never
count toward MVP signoff. A scenario is accepted only after ten consecutive
evidence-backed runs, each with certified outcomes, entity-specific witnesses,
a usable multimodal report, and a documented sample-quality review.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.evaluation.witnesses import specific_entity_fields

LEDGER_SCHEMA_VERSION = 1
REQUIRED_CONSECUTIVE_RUNS = 10
HISTORICAL_DIAGNOSTICS_RELATIVE = Path("validation") / "historical-diagnostics.json"
MVP_TARGETS_RELATIVE = Path("validation") / "mvp-targets.json"

REQUIRED_RUN_GATES = (
    "certified_outcome_graph",
    "entity_or_diagram_witness",
    "no_behavioral_qa_failure",
    "usable_multimodal_report",
    "valid_video_technical_report",
    "sample_quality_manual_review",
    "generality_audit",
)


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected object at {path}")
    return payload


def historical_run_ids(backend_root: Path) -> set[str]:
    path = backend_root / HISTORICAL_DIAGNOSTICS_RELATIVE
    if not path.is_file():
        return set()
    payload = load_json(path)
    return {
        str(item.get("run_id"))
        for item in payload.get("runs", [])
        if isinstance(item, dict) and item.get("run_id")
    }


def mvp_scenarios(backend_root: Path) -> list[dict[str, Any]]:
    payload = load_json(backend_root / MVP_TARGETS_RELATIVE)
    return [item for item in payload.get("supplied_targets", []) if isinstance(item, dict)]


def _usable_multimodal(report: dict[str, Any]) -> bool:
    status = str(report.get("status") or "")
    failures = [str(item) for item in report.get("hard_failures", [])]
    if any("MULTIMODAL_REVIEW_UNAVAILABLE" in item for item in failures):
        return False
    return status == "complete" and not failures


def _entity_or_diagram_witness(record: dict[str, Any]) -> bool:
    diagram = record.get("diagram") or {}
    if isinstance(diagram, dict) and diagram.get("labels_verified") and diagram.get(
        "topology_verified"
    ):
        nodes = diagram.get("nodes") or []
        connectors = diagram.get("connectors") or []
        if isinstance(nodes, list) and isinstance(connectors, list) and len(nodes) >= 2 and connectors:
            return True
    fields = specific_entity_fields(record.get("matched_form_fields") or {})
    return len(fields) >= 2


def evaluate_run_record(
    record: dict[str, Any],
    *,
    historical_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Decide whether one recorded run may extend a consecutive streak."""

    run_id = str(record.get("run_id") or "")
    failures: list[str] = []
    if not run_id:
        failures.append("missing_run_id")
    if historical_ids and (
        run_id in historical_ids or any(run_id.startswith(item) for item in historical_ids)
    ):
        failures.append("historical_diagnostic_not_accepted")
    if record.get("historical"):
        failures.append("historical_diagnostic_not_accepted")
    if not record.get("certified_outcome_graph"):
        failures.append("certified_outcome_graph")
    if not _entity_or_diagram_witness(record):
        failures.append("entity_or_diagram_witness")
    if record.get("behavioral_qa_failures"):
        failures.append("no_behavioral_qa_failure")
    multimodal = record.get("multimodal") if isinstance(record.get("multimodal"), dict) else {}
    if not _usable_multimodal(multimodal):
        failures.append("usable_multimodal_report")
    video = record.get("video") if isinstance(record.get("video"), dict) else {}
    if video.get("hard_failures") or not record.get("valid_video"):
        failures.append("valid_video_technical_report")
    if not record.get("manual_review_passed"):
        failures.append("sample_quality_manual_review")
    if record.get("generality_audit") is not True:
        failures.append("generality_audit")
    if record.get("product_specific_runtime"):
        failures.append("product_specific_runtime")
    accepted = not failures
    return {
        "run_id": run_id,
        "scenario": record.get("scenario"),
        "accepted": accepted,
        "repair_count": int(record.get("repair_count") or 0),
        "first_pass": accepted and int(record.get("repair_count") or 0) == 0,
        "failed_gates": failures,
    }


def consecutive_accepted_streak(evaluations: list[dict[str, Any]]) -> int:
    streak = 0
    for item in evaluations:
        if item.get("accepted"):
            streak += 1
        else:
            streak = 0
    return streak


def scenario_progress(
    records: list[dict[str, Any]],
    *,
    historical_ids: set[str] | None = None,
    required: int = REQUIRED_CONSECUTIVE_RUNS,
) -> dict[str, Any]:
    evaluations = [
        evaluate_run_record(record, historical_ids=historical_ids) for record in records
    ]
    streak = consecutive_accepted_streak(evaluations)
    accepted_count = sum(1 for item in evaluations if item.get("accepted"))
    first_pass = sum(1 for item in evaluations if item.get("first_pass"))
    return {
        "required_consecutive_runs": required,
        "consecutive_accepted": streak,
        "accepted_runs": accepted_count,
        "first_pass_runs": first_pass,
        "first_pass_rate": (first_pass / len(evaluations)) if evaluations else 0.0,
        "signoff_ready": streak >= required,
        "evaluations": evaluations,
    }


def empty_ledger(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "status": "not_accepted",
        "required_consecutive_runs": REQUIRED_CONSECUTIVE_RUNS,
        "required_gates": list(REQUIRED_RUN_GATES),
        "scenarios": {
            str(item.get("name")): {
                "url": item.get("url"),
                "objective": item.get("objective"),
                "records": [],
                "progress": scenario_progress([]),
            }
            for item in scenarios
        },
    }


def refresh_ledger(ledger: dict[str, Any], *, historical_ids: set[str] | None = None) -> dict[str, Any]:
    scenarios = {
        name: {
            **payload,
            "progress": scenario_progress(
                list(payload.get("records") or []),
                historical_ids=historical_ids,
                required=int(ledger.get("required_consecutive_runs") or REQUIRED_CONSECUTIVE_RUNS),
            ),
        }
        for name, payload in dict(ledger.get("scenarios") or {}).items()
        if isinstance(payload, dict)
    }
    signoff = bool(scenarios) and all(
        item.get("progress", {}).get("signoff_ready") for item in scenarios.values()
    )
    return {
        **ledger,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "scenarios": scenarios,
        "status": "accepted" if signoff else "not_accepted",
        "mvp_signoff": signoff,
    }
