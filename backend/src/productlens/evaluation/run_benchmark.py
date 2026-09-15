from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from productlens.evaluation.benchmark import run_and_write_suite


def main() -> None:
    parser = argparse.ArgumentParser(description="Run ProductLens reliability gates repeatedly")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--artifact-root", type=Path, default=Path("artifacts/benchmark"))
    parser.add_argument("--gates", type=int, nargs="+", default=[1, 2, 3, 4, 5, 6])
    parser.add_argument("--restart", action="store_true", help="Ignore a compatible checkpoint")
    args = parser.parse_args()
    if args.attempts < 1:
        raise SystemExit("--attempts must be positive")
    gates = tuple(dict.fromkeys(args.gates))
    print(
        json.dumps(
            asyncio.run(
                run_and_write_suite(
                    args.attempts, args.artifact_root, gates, resume=not args.restart
                )
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
