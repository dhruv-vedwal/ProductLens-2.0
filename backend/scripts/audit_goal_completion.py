"""Build a conservative requirement-to-evidence audit for the MVP goal.

This report is intentionally not a test runner and never infers success from
an MP4 alone.  It records which durable contracts, implementation boundaries,
regression suites, and live acceptance records are present so an operator can
see exactly what is still unproven.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from productlens.urls import canonical_product_url


def _exists(root: Path, *relative: str) -> bool:
    return all((root / item).exists() for item in relative)


def _target_key(value: object) -> str:
    try:
        return canonical_product_url(str(value or ""))
    except (TypeError, ValueError):
        return str(value or "").strip().rstrip("/").casefold()


def _manifest_evidence(path: Path, expected_targets: Path | None = None) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "accepted": 0, "historical_urls": 0, "rejected": 0}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {"status": "invalid", "accepted": 0, "historical_urls": 0, "rejected": 0}
    runs = payload.get("runs", []) if isinstance(payload, dict) else []
    attempts = payload.get("attempts", []) if isinstance(payload, dict) else []
    rejected = payload.get("rejected", []) if isinstance(payload, dict) else []
    all_records = [item for item in [*runs, *attempts, *rejected] if isinstance(item, dict)]
    urls = {_target_key(item.get("url")) for item in all_records if item.get("url")}
    expected: set[str] = set()
    if expected_targets and expected_targets.is_file():
        try:
            target_payload = json.loads(expected_targets.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            target_payload = {}
        if isinstance(target_payload, dict):
            for group in ("supplied_targets", "targets", "fallback_targets"):
                values = target_payload.get(group, [])
                if isinstance(values, list):
                    expected.update(
                        _target_key(item.get("url"))
                        for item in values
                        if isinstance(item, dict) and item.get("url")
                    )
    expected.update(urls)
    terminal: set[str] = set()
    invalid_failures: list[str] = []
    for item in all_records:
        url = _target_key(item.get("url"))
        if not url:
            continue
        status = str(item.get("status") or "").upper()
        if status == "COMPLETE" and item.get("deliverable") and item.get("final_video"):
            terminal.add(url)
        elif status == "FAILED":
            # A rejection is terminal only when the owning layer/error is
            # persisted; an unexplained failed row is not acceptance evidence.
            if item.get("error_code") or item.get("error_type") or item.get("hard_failures"):
                terminal.add(url)
            else:
                invalid_failures.append(url)
    accepted = [
        item for item in runs if isinstance(item, dict) and item.get("status") == "COMPLETE"
    ]
    return {
        "status": "present",
        "accepted": len(accepted),
        "historical_urls": len(urls),
        "attempts": len(attempts),
        "rejected": len(rejected),
        "expected_targets": len(expected),
        "terminal_targets": len(terminal),
        "unresolved_targets": sorted(expected - terminal),
        "invalid_failure_records": sorted(set(invalid_failures)),
        "latest_event": payload.get("latest_event"),
    }


def _sweep_status(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {"status": "invalid"}
    return payload if isinstance(payload, dict) else {"status": "invalid"}


def _static_validation_status(path: Path) -> tuple[str, dict[str, Any]]:
    """Validate the recorded deterministic checks instead of trusting presence alone."""
    if not path.is_file():
        return "missing", {"status": "missing"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return "missing", {"status": "invalid"}
    commands = payload.get("commands") if isinstance(payload, dict) else None
    if not isinstance(commands, dict):
        return "missing", {"status": "invalid"}
    failures = {
        key: value
        for key, value in commands.items()
        if (isinstance(value, str) and value.upper() != "PASS")
        or (isinstance(value, dict) and str(value.get("status", "")).upper() != "PASS")
    }
    evidence = {"status": "present", "failures": failures, "commands": commands}
    return ("evidenced" if not failures else "in_progress"), evidence


def build_audit(root: Path) -> dict[str, Any]:
    """Return conservative evidence statuses without claiming test execution."""
    artifact_root = root / "artifacts"
    checks: list[dict[str, Any]] = []

    def add(name: str, paths: tuple[str, ...], *, note: str) -> None:
        present = _exists(root, *paths)
        checks.append(
            {
                "requirement": name,
                "status": "evidenced" if present else "missing",
                "artifacts": list(paths),
                "note": note,
            }
        )

    add(
        "versioned contracts and persistence",
        (
            "src/productlens/contracts/models.py",
            "src/productlens/persistence/repository.py",
            "alembic/versions",
        ),
        note="Contracts, database facade, and migration history must all be present.",
    )
    add(
        "targeted exploration and product knowledge",
        (
            "src/productlens/discovery/live.py",
            "src/productlens/services/preflight.py",
            "src/productlens/providers/stagehand.py",
        ),
        note="Discovery and the non-recording product scan are separate boundaries.",
    )
    add(
        "generic observation and interaction kernel",
        (
            "src/productlens/execution/engine.py",
            "src/productlens/execution/playwright_adapter.py",
            "src/productlens/execution/state_diff.py",
            "src/productlens/execution/spatial_index.py",
        ),
        note="Execution owns semantic grounding, browser events, verification, and state evidence.",
    )
    add(
        "workflow planning and adaptive replanning",
        (
            "src/productlens/planning/production.py",
            "src/productlens/planning/candidates.py",
            "src/productlens/planning/evidence_graph.py",
            "src/productlens/services/generation.py",
        ),
        note="Planning selects evidence-backed flows; runtime recovery replaces only failed suffixes.",
    )
    add(
        "directed presentation and synchronized narration",
        (
            "src/productlens/presentation/director.py",
            "src/productlens/presentation/editorial.py",
            "src/productlens/video/render.py",
            "video/remotion/src/root.tsx",
        ),
        note="Camera, cursor, scroll, captions, and native source footage share scene evidence.",
    )
    add(
        "layered quality and targeted repair",
        (
            "src/productlens/quality/delivery.py",
            "src/productlens/quality/multimodal.py",
            "src/productlens/quality/repair.py",
            "src/productlens/evaluation/completion_audit.py",
        ),
        note="Delivery requires independent execution, story, visual, synchronization, and evidence checks.",
    )
    add(
        "workers, leases, idempotency, and status streaming",
        (
            "src/productlens/services/jobs.py",
            "src/productlens/workers/local.py",
            "src/productlens/workers/tasks.py",
            "src/productlens/services/stage_contracts.py",
            "src/productlens/api/main.py",
        ),
        note="Durable stages and SSE are implementation evidence; deployment still requires operational validation.",
    )
    add(
        "genericity, capability, and concurrency regression coverage",
        (
            "scripts/audit_project_generality.py",
            "scripts/benchmark_concurrency.py",
            "tests/test_advanced_capabilities.py",
            "tests/test_concurrency.py",
        ),
        note="These files prove the regression boundary exists; command results must be recorded separately.",
    )
    add(
        "golden traces and presentation regression fixtures",
        (
            "validation/golden/demo-trace.json",
            "validation/golden/presentation-baseline.json",
            "tests/test_golden_fixtures.py",
        ),
        note="Provider-neutral golden evidence protects trace, frame, camera, cursor, and scroll invariants.",
    )
    static_status, static_evidence = _static_validation_status(
        artifact_root / "audits" / "static-validation.json"
    )
    checks.append(
        {
            "requirement": "recorded static, typing, genericity, and concurrency validation",
            "status": static_status,
            "artifacts": ["artifacts/audits/static-validation.json"],
            "note": "The audit accepts only a recorded PASS for every deterministic validation command.",
            "validation_evidence": static_evidence,
        }
    )

    live = _manifest_evidence(
        artifact_root / "acceptance" / "public-runs.json",
        root / "validation" / "public-targets.json",
    )
    sweep = _sweep_status(artifact_root / "acceptance" / "public-sweep-status.json")
    live["sweep_status"] = sweep
    live_status = (
        "evidenced"
        if sweep.get("status") == "COMPLETE"
        and live.get("expected_targets", 0)
        and not live.get("unresolved_targets")
        and not live.get("invalid_failure_records")
        else "in_progress"
    )
    checks.append(
        {
            "requirement": "fresh live acceptance for every historical target",
            "status": live_status,
            "artifacts": ["artifacts/acceptance/public-runs.json"],
            "note": "Accepted runs, blocked targets, and owning-layer failures must be reviewed after the sweep.",
            "live_evidence": live,
        }
    )

    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "ProductLensAI 2.0 MVP reliability-first objective",
        "checks": checks,
        "evidenced_count": sum(item["status"] == "evidenced" for item in checks),
        "in_progress_count": sum(item["status"] == "in_progress" for item in checks),
        "missing_count": sum(item["status"] == "missing" for item in checks),
        "complete": all(item["status"] == "evidenced" for item in checks),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/audits/goal-completion.json")
    )
    args = parser.parse_args()
    report = build_audit(args.root.resolve())
    destination = args.output if args.output.is_absolute() else args.root / args.output
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
