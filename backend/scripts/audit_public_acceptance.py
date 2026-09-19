"""Audit the persisted eight-product acceptance set without re-running providers."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from scripts.audit_run_acceptance import audit_run
from scripts.run_public_acceptance import _accepted_record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=Path("artifacts/acceptance/public-runs.json")
    )
    parser.add_argument("--minimum", type=int, default=8)
    args = parser.parse_args()
    manifest_path = args.manifest.resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    runs = list(payload.get("runs", []))
    results: list[dict[str, object]] = []
    failures: list[str] = []
    for item in runs:
        run_id = str(item.get("run_id", ""))
        root = manifest_path.parent.parent / "runs" / run_id
        audit = audit_run(
            root,
            require_manual_review=bool(item.get("manual_review_required", False)),
        )
        accepted = audit["deliverable"] and _accepted_record(item)
        result = {
            "name": item.get("name", item.get("url")),
            "url": item.get("url"),
            "run_id": run_id,
            "accepted": accepted,
            "video": str(root / "final" / "demo.mp4")
            if audit["video"]["present"]
            else None,
            "duration_seconds": audit["video"].get("duration_seconds"),
            "hard_failures": audit["hard_failures"],
            "missing_artifacts": audit["missing_artifacts"],
            "acceptance_evidence": item.get("acceptance_evidence"),
            "artifact_root": str(root),
        }
        results.append(result)
        if not accepted:
            failures.append(run_id or str(item.get("url", "unknown")))
    report = {
        "schema_version": 2,
        "created_at": datetime.now(UTC).isoformat(),
        "required_accepted_runs": args.minimum,
        "accepted_runs": sum(bool(item["accepted"]) for item in results),
        "deliverable": len(runs) >= args.minimum and not failures,
        "failures": failures,
        "runs": results,
    }
    output = manifest_path.parent / "final-report.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["deliverable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
