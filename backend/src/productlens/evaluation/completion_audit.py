"""Evidence matrix for deciding whether a run is actually deliverable."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

REQUIRED_ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("objective", "objective.json"),
    ("discovery", "discovery/product-context.json"),
    ("knowledge", "page-knowledge"),
    ("plan", "plan.json"),
    ("trace", "execution/trace.json"),
    ("presentation", "presentation/presentation-plan.json"),
    ("narration", "presentation/narration-script.json"),
    ("execution_qa", "qa/execution-report.json"),
    ("editorial_qa", "qa/story-report.json"),
    ("visual_qa", "qa/video-report.json"),
    ("synchronization_qa", "qa/synchronization-report.json"),
    ("delivery", "qa/delivery-report.json"),
    ("manifest", "artifact-manifest.json"),
    ("video", "final/demo.mp4"),
)


def audit_run(root: Path) -> dict[str, object]:
    """Return an explicit evidence matrix without declaring success implicitly."""
    checks: list[dict[str, object]] = []
    for layer, relative in REQUIRED_ARTIFACTS:
        path = root / relative
        present = path.is_dir() if relative.endswith("knowledge") else path.is_file()
        checks.append({"layer": layer, "path": relative, "present": present})
    missing = [item["layer"] for item in checks if not item["present"]]
    delivery = root / "qa" / "delivery-report.json"
    delivery_declared = False
    if delivery.is_file():
        try:
            delivery_declared = bool(json.loads(delivery.read_text(encoding="utf-8")).get("deliverable"))
        except (OSError, ValueError, TypeError):
            delivery_declared = False
    manifest_valid = False
    manifest_path = root / "artifact-manifest.json"
    if manifest_path.is_file():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_valid = all(
                (root / str(entry["path"])).is_file()
                and (root / str(entry["path"])).stat().st_size == int(entry["bytes"])
                and hashlib.sha256((root / str(entry["path"])).read_bytes()).hexdigest() == entry["sha256"]
                for entry in payload.get("artifacts", [])
            )
        except (OSError, ValueError, TypeError, KeyError):
            manifest_valid = False
    if not manifest_valid:
        missing.append("manifest_integrity")
    return {
        "run_root": str(root),
        "complete_evidence": not missing and delivery_declared,
        "delivery_declared": delivery_declared,
        "missing_layers": missing,
        "checks": checks,
    }
