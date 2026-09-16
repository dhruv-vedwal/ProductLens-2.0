"""Aggregate layer-owned failures for benchmark and operations review."""

from __future__ import annotations

from collections import Counter
from typing import Any


def aggregate_failure_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Return counts by failure, owner, and repair boundary without losing runs."""
    failures: Counter[str] = Counter()
    owners: Counter[str] = Counter()
    boundaries: Counter[str] = Counter()
    for report in reports:
        for failure in report.get("hard_failures", []):
            failures[str(failure)] += 1
        for failure, owner in report.get("owner_by_failure", {}).items():
            owners[str(owner)] += 1
        decision = report.get("repair_decision") or {}
        boundary = decision.get("retry_boundary")
        if boundary:
            boundaries[str(boundary)] += 1
    return {
        "report_count": len(reports),
        "failure_count": sum(failures.values()),
        "failures_by_code": dict(failures),
        "failures_by_owner": dict(owners),
        "retries_by_boundary": dict(boundaries),
    }
