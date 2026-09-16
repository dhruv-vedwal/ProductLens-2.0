"""Classify run artifacts for safe retention review.

This command is intentionally dry-run only: evidence is classified, never
deleted. A human can use the JSON output to approve a narrowly scoped cleanup
after confirming that no run lineage or user material is being discarded.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.quality.consistency import validate_selected_candidate_consistency


def classify_run(path: Path) -> dict[str, Any]:
    files = [item for item in path.rglob("*") if item.is_file()]
    has_video = (path / "final" / "demo.mp4").is_file()
    has_trace = (path / "execution" / "trace.json").is_file()
    has_plan = (path / "plan.json").is_file()
    has_discovery = any(
        (path / relative).exists()
        for relative in ("discovery", "exploration-report.json", "objective.json")
    )
    consistency_failures: list[str] = []
    plan_path = path / "plan.json"
    if plan_path.is_file():
        try:
            consistency_failures = validate_selected_candidate_consistency(
                json.loads(plan_path.read_text(encoding="utf-8"))
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
            consistency_failures = ["INVALID_PLAN_ARTIFACT"]
    if consistency_failures:
        category = "review_stale_plan"
        reason = "candidate metadata disagrees with executable workflow"
    elif has_video:
        category = "retain_deliverable"
        reason = "final video is present"
    elif has_trace or (has_plan and has_discovery):
        category = "retain_evidence"
        reason = "run contains durable planning or execution evidence"
    elif files:
        category = "review_partial"
        reason = "partial artifacts require lineage review"
    else:
        category = "review_empty"
        reason = "directory contains no files"
    return {
        "run_id": path.name,
        "path": path.as_posix(),
        "category": category,
        "reason": reason,
        "file_count": len(files),
        "consistency_failures": consistency_failures,
    }


def classify_root(root: Path) -> dict[str, Any]:
    runs_root = root / "runs"
    records = []
    if runs_root.is_dir():
        # A run is any directory with a run marker. This also handles the
        # historical nested ``runs/runs`` layout without treating arbitrary
        # cache directories as deletable artifacts.
        candidates = {
            path.parent
            for marker in ("objective.json", "plan.json", "execution", "discovery", "final")
            for path in runs_root.rglob(marker)
            if path.exists()
        }
        records = [classify_run(path) for path in sorted(candidates)]
    counts: dict[str, int] = {}
    for record in records:
        counts[record["category"]] = counts.get(record["category"], 0) + 1
    return {
        "version": 1,
        "root": root.as_posix(),
        "dry_run": True,
        "counts": counts,
        "runs": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="artifact root containing the runs directory")
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    args = parser.parse_args()
    report = classify_root(args.root.resolve())
    payload = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
