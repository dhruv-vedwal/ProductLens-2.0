"""Audit runtime source for acceptance-project-specific coupling.

The audit intentionally scans only ``src/productlens`` for runtime coupling and
reports benchmark fixtures separately.  Fixture names are data for tests; they
must never become selectors, routes, choreography, or narration rules in the
product runtime.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

FORBIDDEN_RUNTIME_IDENTIFIERS = (
    "dhruv-sys.vercel.app",
    "study-plan-red.vercel.app",
    "chit-chat connect",
    "sociopedia",
    "promptrouter",
    "calmprep",
    "cookplan ai",
    "smartsevak",
    "datansh solutions",
    # Domain phrases that previously leaked from the interview-prep fixture
    # into the shared narration fallback.  Fixture/test data may use them;
    # production runtime must derive wording from observed evidence instead.
    "challenge bank",
    "no-ai challenges",
    "problem-solving patterns",
    "daily plan checkpoint",
)


def audit(runtime_root: Path) -> dict[str, object]:
    offenders: list[dict[str, object]] = []
    files_scanned = 0
    for source in sorted(runtime_root.rglob("*.py")):
        files_scanned += 1
        text = source.read_text(encoding="utf-8").casefold()
        matches = [item for item in FORBIDDEN_RUNTIME_IDENTIFIERS if item in text]
        if matches:
            offenders.append(
                {
                    "path": str(source.relative_to(runtime_root)).replace("\\", "/"),
                    "identifiers": matches,
                    "classification": "runtime-coupling",
                }
            )
    return {
        "audit_version": "1",
        "created_at": datetime.now(UTC).isoformat(),
        "scope": str(runtime_root),
        "files_scanned": files_scanned,
        "forbidden_identifiers": list(FORBIDDEN_RUNTIME_IDENTIFIERS),
        "runtime_offenders": offenders,
        "benchmark_fixture_policy": "Benchmark choreography is isolated under benchmark/ and is not runtime behavior.",
        "result": "pass" if not offenders else "fail",
        "genericity_statement": (
            "Runtime planning and execution consume objective and observed evidence; "
            "no acceptance-site names, routes, or project-specific choreography are embedded."
            if not offenders
            else "Remove the reported acceptance-project coupling before production use."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, default=Path("src/productlens"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/audits/project-generality.json"))
    args = parser.parse_args()
    report = audit(args.runtime_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if report["result"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
