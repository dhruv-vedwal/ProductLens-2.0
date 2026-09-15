"""Validate durable run claims at the supported 1/10/50/100 worker levels."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from productlens.evaluation.concurrency import run_claim_benchmark


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/concurrency-benchmark.json"))
    parser.add_argument("--database-dir", type=Path)
    args = parser.parse_args()
    with TemporaryDirectory(dir=args.database_dir) as directory:
        root = Path(directory)
        reports = [
            run_claim_benchmark(root / f"claims-{level}.sqlite3", runs=level)
            for level in (1, 10, 50, 100)
        ]
    payload = {
        "schema_version": 1,
        "levels": reports,
        "passed": all(item["all_claimed_once"] for item in reports),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
