"""Verify one run against the ProductLens delivery contract.

This is a read-only audit.  It never reruns a provider, edits a run, or treats
the existence of an MP4 as success.  The report is intentionally generic and
works for fixture, local, and Browserbase runs; provider-specific evidence is
checked only when the run declares that it was required.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REQUIRED_ARTIFACTS = (
    "objective.json",
    "discovery/objective-understanding.json",
    "discovery/product-context.json",
    "discovery/product-knowledge.json",
    "discovery/behavioral-product-model.json",
    "exploration-report.json",
    "feature-graph.json",
    "candidate-flows.json",
    "plan.json",
    "presentation/validated-scene-plan.json",
    "execution/trace.json",
    "execution/interaction-trace.json",
    "presentation/semantic-moments.json",
    "presentation/sync-edl.json",
    "presentation/storyboard.json",
    "presentation/narration-script.json",
    "narration/fact-extraction.json",
    "presentation/captions.json",
    "qa/exploration-report.json",
    "qa/coverage-report.json",
    "qa/story-report.json",
    "qa/editorial-report.json",
    "qa/video-report.json",
    "qa/presentation-report.json",
    "qa/synchronization-report.json",
    "qa/multimodal-report.json",
    "qa/completion-audit.json",
    "qa/delivery-report.json",
)

QA_REPORTS = (
    "qa/exploration-report.json",
    "qa/coverage-report.json",
    "qa/story-report.json",
    "qa/editorial-report.json",
    "qa/video-report.json",
    "qa/presentation-report.json",
    "qa/synchronization-report.json",
    "qa/multimodal-report.json",
    "qa/delivery-report.json",
)


def _read_json(path: Path) -> dict[str, Any] | list[Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, (dict, list)) else None


def _probe_video(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False, "valid": False}
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_name,width,height,avg_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    payload = _read_json_from_text(result.stdout)
    streams = payload.get("streams", []) if isinstance(payload, dict) else []
    video = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_name")), {}
    )
    try:
        duration = float(
            (payload.get("format", {}) if isinstance(payload, dict) else {}).get("duration")
        )
    except (TypeError, ValueError):
        duration = None
    return {
        "present": True,
        "valid": result.returncode == 0 and bool(video) and duration is not None and duration > 0,
        "duration_seconds": round(duration, 3) if duration is not None else None,
        "width": video.get("width"),
        "height": video.get("height"),
        "frame_rate": video.get("avg_frame_rate"),
        "codec": video.get("codec_name"),
    }


def _read_json_from_text(value: str) -> dict[str, Any] | list[Any] | None:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def audit_run(run_root: Path, *, require_manual_review: bool = False) -> dict[str, Any]:
    run_root = run_root.resolve()
    missing = [item for item in REQUIRED_ARTIFACTS if not (run_root / item).is_file()]
    malformed = [
        item
        for item in (*QA_REPORTS, "objective.json", "plan.json", "execution/trace.json")
        if (run_root / item).is_file() and _read_json(run_root / item) is None
    ]
    qa: dict[str, dict[str, Any]] = {}
    hard_failures: list[str] = []
    for relative in QA_REPORTS:
        value = _read_json(run_root / relative)
        if isinstance(value, dict):
            qa[relative] = value
            failures = value.get("hard_failures", [])
            if isinstance(failures, list):
                hard_failures.extend(f"{relative}:{item}" for item in failures)
    delivery = qa.get("qa/delivery-report.json", {})
    multimodal = qa.get("qa/multimodal-report.json", {})
    completion = _read_json(run_root / "qa" / "completion-audit.json")
    workflow_proved = (
        isinstance(completion, dict)
        and completion.get("complete_evidence") is True
        and not completion.get("missing_layers")
    )
    multimodal_passed = (
        multimodal.get("status") == "complete" and not multimodal.get("hard_failures")
    )
    video = _probe_video(run_root / "final" / "demo.mp4")
    manual_path = run_root / "quality" / "manual-review.json"
    manual = _read_json(manual_path)
    manual_passed = isinstance(manual, dict) and manual.get("status") == "PASS"
    if require_manual_review and not manual_passed:
        hard_failures.append("manual-review:required")
    if not video["valid"]:
        hard_failures.append("delivery:invalid-final-video")
    if not bool(delivery.get("deliverable")):
        hard_failures.append("delivery:verdict-not-deliverable")
    if not workflow_proved:
        hard_failures.append("workflow:completion-evidence-not-proved")
    if not multimodal_passed:
        hard_failures.append("multimodal:semantic-review-not-passed")
    hard_failures = list(dict.fromkeys(str(item) for item in hard_failures))
    report = {
        "schema_version": 2,
        "created_at": datetime.now(UTC).isoformat(),
        "run_root": str(run_root),
        "required_artifacts": list(REQUIRED_ARTIFACTS),
        "missing_artifacts": missing,
        "malformed_artifacts": malformed,
        "video": video,
        "manual_review": {
            "path": str(manual_path),
            "present": manual_path.is_file(),
            "passed": manual_passed,
            "required": require_manual_review,
        },
        "acceptance_evidence": {
            "workflow_proof": workflow_proved,
            "multimodal_verdict": {
                "passed": multimodal_passed,
                "provider": multimodal.get("provider"),
                "status": multimodal.get("status"),
            },
            "manual_review": manual_passed,
            "repeated_run_result": None,
        },
        "qa_layers": {
            relative: {
                "present": relative in qa,
                "hard_failures": qa.get(relative, {}).get("hard_failures", []),
            }
            for relative in QA_REPORTS
        },
        "hard_failures": hard_failures,
        "deliverable": not missing and not malformed and not hard_failures,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-manual-review", action="store_true")
    args = parser.parse_args()
    run_root = args.run_root
    # Operators commonly pass the durable run UUID copied from the API. Keep
    # that shorthand read-only and resolve it to the configured local artifact
    # tree; explicit paths continue to work for CI and archived runs.
    if not run_root.exists() and len(run_root.parts) == 1:
        candidate = Path.cwd() / "artifacts" / "runs" / run_root.name
        if candidate.is_dir():
            run_root = candidate
    report = audit_run(run_root, require_manual_review=args.require_manual_review)
    payload = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if report["deliverable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
