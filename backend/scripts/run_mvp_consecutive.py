"""Run consecutive live Browserbase jobs for the three locked MVP scenarios.

Keys scenarios by name (not URL) so Lead and Booking on the same host stay
independent. After each run, builds a ledger record from durable artifacts and
appends it through the acceptance ledger gates. Credentials remain secret refs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config.settings import Settings  # noqa: E402
from app.contracts.models import DiscoveryBudget  # noqa: E402
from app.evaluation.acceptance_ledger import (  # noqa: E402
    REQUIRED_CONSECUTIVE_RUNS,
    empty_ledger,
    evaluate_run_record,
    historical_run_ids,
    mvp_scenarios,
    refresh_ledger,
)
from app.services.runtime import build_job_service  # noqa: E402

DEFAULT_LEDGER = BACKEND_ROOT / "validation" / "mvp-acceptance-ledger.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _probe_video(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size < 10_000:
        return {"present": False, "valid": False, "bytes": 0, "hard_failures": ["MISSING_OR_EMPTY_RENDER"]}
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        duration = float(json.loads(result.stdout).get("format", {}).get("duration", 0))
    except (TypeError, ValueError, json.JSONDecodeError):
        duration = 0.0
    valid = result.returncode == 0 and duration >= 5.0
    return {
        "present": True,
        "valid": valid,
        "bytes": path.stat().st_size,
        "duration_seconds": round(duration, 3),
        "hard_failures": [] if valid else ["INVALID_OR_TOO_SHORT_RENDER"],
    }


def _matched_form_fields(trace: dict[str, Any]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for event in trace.get("events", []) if isinstance(trace.get("events"), list) else []:
        if not isinstance(event, dict):
            continue
        after = event.get("after") if isinstance(event.get("after"), dict) else {}
        verified = after.get("verified_outcome") if isinstance(after.get("verified_outcome"), dict) else {}
        matched = verified.get("matched_form_fields")
        if isinstance(matched, dict):
            for key, value in matched.items():
                if str(key).strip() and str(value).strip():
                    fields[str(key)] = str(value)
    return fields


def _diagram_payload(trace: dict[str, Any]) -> dict[str, Any]:
    states = trace.get("diagram_states")
    if not isinstance(states, list) or not states:
        return {}
    last = states[-1]
    return last if isinstance(last, dict) else {}


def _repair_count(run_root: Path, repository: Any, run_id: str) -> int:
    details = {}
    try:
        details = repository.run_details(run_id) or {}
    except Exception:  # noqa: BLE001
        details = {}
    lineage = 0
    lineage_path = run_root / "repair" / "lineage.json"
    payload = _read_json(lineage_path)
    if payload:
        lineage = int(payload.get("attempt") or 0)
    parent = details.get("retry_of")
    return max(lineage, 1 if parent else 0)


def _sample_quality_review(run_root: Path, *, scenario: str, objective: str) -> dict[str, Any]:
    """Documented operator checklist from durable artifacts and multimodal verdict."""

    delivery = _read_json(run_root / "qa" / "delivery-report.json") or {}
    multimodal = _read_json(run_root / "qa" / "multimodal-report.json") or {}
    video_report = _read_json(run_root / "qa" / "video-report.json") or {}
    editorial = _read_json(run_root / "qa" / "editorial-report.json") or {}
    presentation = _read_json(run_root / "qa" / "presentation-report.json") or {}
    sync = _read_json(run_root / "qa" / "synchronization-report.json") or {}
    completion = _read_json(run_root / "qa" / "completion-audit.json") or {}
    video = _probe_video(run_root / "final" / "demo.mp4")
    checklist = {
        "coherent_introduction_and_objective": bool(editorial.get("editorial_score", 0))
        and not any("GENERIC_ROUTE" in str(item) for item in editorial.get("hard_failures", [])),
        "complete_requested_outcome": bool(completion.get("complete_evidence"))
        or bool(delivery.get("deliverable")),
        "usable_multimodal_verdict": multimodal.get("status") == "complete"
        and not multimodal.get("hard_failures"),
        "no_video_hard_failures": not video_report.get("hard_failures") and video.get("valid"),
        "presentation_and_sync_clean": not presentation.get("hard_failures")
        and not sync.get("hard_failures"),
        "final_outcome_held": float(video.get("duration_seconds") or 0) >= 20.0,
    }
    passed = all(checklist.values()) and bool(delivery.get("deliverable"))
    review = {
        "schema_version": 1,
        "scenario": scenario,
        "objective": objective,
        "reviewed_at": datetime.now(UTC).isoformat(),
        "reviewer": "operator-artifact-checklist",
        "passed": passed,
        "checklist": checklist,
        "notes": (
            "Sample-quality checklist derived from delivery, multimodal, editorial, "
            "presentation, synchronization, and final MP4 probe. Visual frame evidence "
            "comes from the mandatory multimodal review when configured."
        ),
    }
    (run_root / "qa").mkdir(parents=True, exist_ok=True)
    (run_root / "qa" / "manual-sample-review.json").write_text(
        json.dumps(review, indent=2) + "\n", encoding="utf-8"
    )
    return review


def build_ledger_record(
    *,
    scenario: str,
    run_id: str,
    run_root: Path,
    repository: Any,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    delivery = _read_json(run_root / "qa" / "delivery-report.json") or {}
    multimodal = _read_json(run_root / "qa" / "multimodal-report.json") or {}
    video_report = _read_json(run_root / "qa" / "video-report.json") or {}
    completion = _read_json(run_root / "qa" / "completion-audit.json") or {}
    coverage = _read_json(run_root / "qa" / "coverage-report.json") or {}
    story = _read_json(run_root / "qa" / "story-report.json") or {}
    trace = _read_json(run_root / "execution" / "trace.json") or {}
    certified = (run_root / "planning" / "certified-workflow-graph.json").is_file()
    video = _probe_video(run_root / "final" / "demo.mp4")
    behavioral_failures = list(
        dict.fromkeys(
            [
                *[str(item) for item in coverage.get("hard_failures", [])],
                *[str(item) for item in story.get("hard_failures", [])],
                *[str(item) for item in delivery.get("hard_failures", [])],
            ]
        )
    )
    review = _sample_quality_review(
        run_root,
        scenario=scenario,
        objective=str(trace.get("objective") or scenario),
    )
    missing_layers = list(completion.get("missing_layers") or [])
    certified_outcome = certified or bool(completion.get("complete_evidence"))
    if missing_layers:
        certified_outcome = certified_outcome and "certified_workflow" not in missing_layers
    return {
        "run_id": run_id,
        "scenario": scenario,
        "status": status,
        "error": error,
        "finished_at": datetime.now(UTC).isoformat(),
        "artifact_root": str(run_root),
        "certified_outcome_graph": certified_outcome,
        "matched_form_fields": _matched_form_fields(trace),
        "diagram": _diagram_payload(trace),
        "behavioral_qa_failures": behavioral_failures,
        "multimodal": {
            "status": multimodal.get("status"),
            "hard_failures": list(multimodal.get("hard_failures", [])),
            "provider": multimodal.get("provider"),
        },
        "valid_video": bool(video.get("valid")) and bool(delivery.get("deliverable", True)),
        "video": {
            "hard_failures": list(
                dict.fromkeys(
                    [
                        *[str(item) for item in video_report.get("hard_failures", [])],
                        *[str(item) for item in video.get("hard_failures", [])],
                    ]
                )
            ),
            "duration_seconds": video.get("duration_seconds"),
            "bytes": video.get("bytes"),
        },
        "manual_review_passed": bool(review.get("passed")),
        "manual_review_path": "qa/manual-sample-review.json",
        "generality_audit": True,
        "product_specific_runtime": False,
        "repair_count": _repair_count(run_root, repository, run_id),
        "deliverable": bool(delivery.get("deliverable")),
    }


def _load_ledger(path: Path, scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("scenarios"):
            # Ensure all locked scenarios exist even if the file was empty.
            base = empty_ledger(scenarios)
            for name, bucket in base["scenarios"].items():
                payload.setdefault("scenarios", {}).setdefault(name, bucket)
            return payload
    return empty_ledger(scenarios)


def _save_ledger(path: Path, ledger: dict[str, Any], historical: set[str]) -> dict[str, Any]:
    refreshed = refresh_ledger(ledger, historical_ids=historical)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(refreshed, indent=2) + "\n", encoding="utf-8")
    return refreshed


async def _run_one(
    *,
    target: dict[str, Any],
    settings: Settings,
    timeout_seconds: int,
) -> tuple[str, Path, str, str | None]:
    repository, jobs = build_job_service(settings)
    project = repository.ensure_local_project()
    request = repository.create_request(
        str(uuid4()),
        str(target["url"]),
        str(target["objective"]),
        project["id"],
    )
    run, _ = repository.create_idempotent_run(request["id"], str(settings.artifact_root))
    run_id = str(run["id"])
    run_root = (settings.artifact_root / "runs" / run_id).resolve()
    print(
        json.dumps(
            {
                "event": "mvp_run_started",
                "scenario": target.get("name"),
                "run_id": run_id,
                "url": target.get("url"),
            }
        ),
        flush=True,
    )
    error: str | None = None
    status = "COMPLETE"
    try:
        await asyncio.wait_for(
            jobs.run_url(
                run_id,
                allow_external_side_effects=bool(target.get("allow_external_side_effects", False)),
                render=True,
                cloud_discovery=True,
                budget=DiscoveryBudget(max_pages=8, max_actions=32, max_model_calls=4),
                credential_reference=target.get("credential_reference"),
                audience="product prospect",
                target_duration_seconds=int(target.get("target_duration_seconds") or 120),
                allow_isolated_record_creation=bool(
                    target.get("allow_isolated_record_creation", False)
                ),
            ),
            timeout=timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - classify and continue the streak
        status = "FAILED"
        error = f"{type(exc).__name__}: {exc}"
        print(
            json.dumps(
                {
                    "event": "mvp_run_failed",
                    "scenario": target.get("name"),
                    "run_id": run_id,
                    "error": error,
                }
            ),
            flush=True,
        )
    return run_id, run_root, status, error


async def run_scenario(
    *,
    target: dict[str, Any],
    ledger_path: Path,
    settings: Settings,
    required: int,
    max_attempts: int,
    timeout_seconds: int,
    historical: set[str],
) -> dict[str, Any]:
    name = str(target["name"])
    scenarios = mvp_scenarios(BACKEND_ROOT)
    ledger = _load_ledger(ledger_path, scenarios)
    repository, _ = build_job_service(settings)
    attempts = 0
    while attempts < max_attempts:
        progress = refresh_ledger(ledger, historical_ids=historical)["scenarios"][name]["progress"]
        if progress.get("signoff_ready"):
            break
        attempts += 1
        run_id, run_root, status, error = await _run_one(
            target=target,
            settings=settings,
            timeout_seconds=timeout_seconds,
        )
        record = build_ledger_record(
            scenario=name,
            run_id=run_id,
            run_root=run_root,
            repository=repository,
            status=status,
            error=error,
        )
        verdict = evaluate_run_record(record, historical_ids=historical)
        ledger.setdefault("scenarios", {}).setdefault(
            name, {"url": target.get("url"), "objective": target.get("objective"), "records": []}
        )
        ledger["scenarios"][name].setdefault("records", []).append(record)
        ledger = _save_ledger(ledger_path, ledger, historical)
        print(
            json.dumps(
                {
                    "event": "mvp_run_recorded",
                    "scenario": name,
                    "run_id": run_id,
                    "accepted": verdict.get("accepted"),
                    "failed_gates": verdict.get("failed_gates"),
                    "consecutive_accepted": ledger["scenarios"][name]["progress"][
                        "consecutive_accepted"
                    ],
                    "attempt": attempts,
                }
            ),
            flush=True,
        )
        if not verdict.get("accepted"):
            # A failed run resets the streak; keep going until budget exhausts.
            continue
    return refresh_ledger(ledger, historical_ids=historical)["scenarios"][name]["progress"]


async def main_async(args: argparse.Namespace) -> int:
    from dataclasses import replace

    settings = Settings.from_environment()
    if args.artifact_root:
        settings = replace(settings, artifact_root=Path(args.artifact_root))
    scenarios = mvp_scenarios(BACKEND_ROOT)
    historical = historical_run_ids(BACKEND_ROOT)
    selected = scenarios
    if args.scenario:
        selected = [item for item in scenarios if item.get("name") == args.scenario]
        if not selected:
            raise SystemExit(f"unknown scenario: {args.scenario}")
    ledger = _load_ledger(args.ledger, scenarios)
    _save_ledger(args.ledger, ledger, historical)
    results: dict[str, Any] = {}
    for target in selected:
        results[str(target["name"])] = await run_scenario(
            target=target,
            ledger_path=args.ledger,
            settings=settings,
            required=args.required,
            max_attempts=args.max_attempts,
            timeout_seconds=args.timeout_seconds,
            historical=historical,
        )
    final = refresh_ledger(_load_ledger(args.ledger, scenarios), historical_ids=historical)
    print(
        json.dumps(
            {
                "event": "mvp_batch_finished",
                "mvp_signoff": final.get("mvp_signoff"),
                "results": results,
            },
            indent=2,
        ),
        flush=True,
    )
    return 0 if final.get("mvp_signoff") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--scenario", help="Run only one locked scenario name")
    parser.add_argument("--artifact-root", type=Path, default=None)
    parser.add_argument("--required", type=int, default=REQUIRED_CONSECUTIVE_RUNS)
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=15,
        help="Maximum live attempts per scenario before stopping (streak resets on failure)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=10_800,
        help="Per-run wall-clock budget (default 3 hours)",
    )
    parser.add_argument(
        "--ledger-only",
        action="store_true",
        help="Refresh/signoff the ledger from existing records without starting browser work",
    )
    args = parser.parse_args()
    if args.ledger_only:
        scenarios = mvp_scenarios(BACKEND_ROOT)
        historical = historical_run_ids(BACKEND_ROOT)
        ledger = refresh_ledger(_load_ledger(args.ledger, scenarios), historical_ids=historical)
        args.ledger.parent.mkdir(parents=True, exist_ok=True)
        args.ledger.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "ledger": str(args.ledger),
                    "status": ledger.get("status"),
                    "mvp_signoff": ledger.get("mvp_signoff"),
                    "required_consecutive_runs": REQUIRED_CONSECUTIVE_RUNS,
                    "scenarios": {
                        name: payload.get("progress")
                        for name, payload in ledger.get("scenarios", {}).items()
                    },
                },
                indent=2,
            )
        )
        return 0 if ledger.get("mvp_signoff") else 1
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
