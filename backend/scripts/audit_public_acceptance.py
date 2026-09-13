"""Audit the persisted eight-product acceptance set without re-running providers."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path


REQUIRED_ARTIFACTS = (
    "objective.json",
    "discovery/objective-understanding.json",
    "discovery/product-knowledge.json",
    "page-knowledge/00.json",
    "feature-graph.json",
    "candidate-flows.json",
    "plan.json",
    "execution/trace.json",
    "presentation/validated-scene-plan.json",
    "presentation/narration-script.json",
    "presentation/captions.json",
    "qa/delivery-report.json",
)


def _duration(path: Path) -> float | None:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("artifacts/acceptance/public-runs.json"))
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
        video = root / "final" / "demo.mp4"
        delivery_path = root / "qa" / "delivery-report.json"
        delivery: dict[str, object] = {}
        if delivery_path.is_file():
            try:
                delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                delivery = {}
        duration = _duration(video) if video.is_file() else None
        hard_failures = list(delivery.get("hard_failures", []))
        missing_artifacts = [item for item in REQUIRED_ARTIFACTS if not (root / item).is_file()]
        accepted = (
            item.get("status") == "COMPLETE"
            and bool(delivery.get("deliverable"))
            and video.is_file()
            and duration is not None
            and 60 <= duration <= 240
            and not hard_failures
            and not missing_artifacts
        )
        result = {
            "name": item.get("name", item.get("url")),
            "url": item.get("url"),
            "run_id": run_id,
            "accepted": accepted,
            "video": str(video) if video.is_file() else None,
            "duration_seconds": round(duration, 3) if duration is not None else None,
            "hard_failures": hard_failures,
            "missing_artifacts": missing_artifacts,
            "artifact_root": str(root),
        }
        results.append(result)
        if not accepted:
            failures.append(run_id or str(item.get("url", "unknown")))
    report = {
        "schema_version": 1,
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
