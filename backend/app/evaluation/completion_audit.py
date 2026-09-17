"""Evidence matrix for deciding whether a run is actually deliverable."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.quality.consistency import validate_selected_candidate_consistency

REQUIRED_ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("objective", "objective.json"),
    ("discovery", "discovery/product-context.json"),
    ("exploration_report", "exploration-report.json"),
    ("product_knowledge", "discovery/product-knowledge.json"),
    ("knowledge", "page-knowledge"),
    ("feature_graph", "feature-graph.json"),
    ("candidate_flows", "candidate-flows.json"),
    ("relevance_graph", "discovery/relevance-graph.json"),
    ("plan", "plan.json"),
    ("capability_resolution", "planning/capability-resolutions.json"),
    ("plan_consistency", "qa/plan-consistency-report.json"),
    ("trace", "execution/trace.json"),
    ("state_snapshots", "execution/state-snapshots.json"),
    ("action_attempts", "execution/action-attempts.json"),
    ("verification_results", "execution/verification-results.json"),
    ("presentation", "presentation/presentation-plan.json"),
    ("editorial_brief", "presentation/editorial-brief.json"),
    ("storyboard", "presentation/storyboard.json"),
    ("scene_plan", "presentation/scene-plan.json"),
    ("validated_scene_plan", "presentation/validated-scene-plan.json"),
    ("narration", "presentation/narration-script.json"),
    ("fact_extraction", "narration/fact-extraction.json"),
    ("captions", "presentation/captions.json"),
    ("execution_qa", "qa/execution-report.json"),
    ("editorial_qa", "qa/story-report.json"),
    ("editorial_validation_qa", "qa/editorial-report.json"),
    ("journey_qa", "quality/journey-report.json"),
    ("visual_qa", "qa/video-report.json"),
    ("presentation_qa", "qa/presentation-report.json"),
    ("synchronization_qa", "qa/synchronization-report.json"),
    ("multimodal_qa", "qa/multimodal-report.json"),
    ("delivery", "qa/delivery-report.json"),
    ("manifest", "artifact-manifest.json"),
    ("video", "final/demo.mp4"),
)


def _sha256_file(path: Path) -> str:
    """Hash retained evidence without loading long recordings into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
            delivery_declared = bool(
                json.loads(delivery.read_text(encoding="utf-8")).get("deliverable")
            )
        except (OSError, ValueError, TypeError):
            delivery_declared = False
    manifest_valid = False
    manifest_path = root / "artifact-manifest.json"
    if manifest_path.is_file():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries = payload.get("artifacts")
            if not isinstance(entries, list) or not entries:
                raise ValueError("artifact manifest must contain a non-empty artifacts list")
            root_resolved = root.resolve()
            manifest_paths: set[str] = set()
            checks_valid = True
            for entry in entries:
                if not isinstance(entry, dict):
                    checks_valid = False
                    break
                relative = str(entry.get("path") or "")
                candidate = (root / relative).resolve()
                # A manifest is an integrity boundary, not a way to hash an
                # arbitrary absolute path or a parent-directory escape.
                if (
                    not relative
                    or Path(relative).is_absolute()
                    or root_resolved not in candidate.parents
                ):
                    checks_valid = False
                    break
                manifest_paths.add(Path(relative).as_posix())
                checks_valid = checks_valid and (
                    candidate.is_file()
                    and candidate.stat().st_size == int(entry["bytes"])
                    and _sha256_file(candidate) == entry["sha256"]
                )
            # Hash-valid entries are still insufficient if a required layer is
            # absent from the manifest.  Otherwise an empty/minimal manifest
            # could make a run appear complete even though delivery evidence
            # was never immutable or registered.
            required_paths = {
                Path(relative).as_posix()
                for _layer, relative in REQUIRED_ARTIFACTS
                if not relative.endswith("page-knowledge") and relative != "artifact-manifest.json"
            }
            required_paths_present = required_paths.issubset(manifest_paths)
            knowledge_present = any(path.startswith("page-knowledge/") for path in manifest_paths)
            manifest_valid = checks_valid and required_paths_present and knowledge_present
        except (OSError, ValueError, TypeError, KeyError):
            manifest_valid = False
    if not manifest_valid:
        missing.append("manifest_integrity")
    # Discovery summaries and executable workflow steps are separate layers.
    # Reject stale candidate metadata instead of allowing an old route or
    # outcome description to accompany a newer recording.
    plan_path = root / "plan.json"
    try:
        plan_payload = (
            json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.is_file() else {}
        )
    except (OSError, ValueError, TypeError):
        plan_payload = {}
    consistency_failures = validate_selected_candidate_consistency(plan_payload)
    missing.extend(failure.casefold() for failure in consistency_failures)
    # Delivery must be supported by the individual layer reports. A report
    # file is not proof of success when it persists hard failures for a
    # targeted repair; inspect the durable verdicts rather than relying on an
    # MP4 or a delivery boolean alone.
    report_layers = {
        "plan_consistency": "qa/plan-consistency-report.json",
        "execution_qa": "qa/execution-report.json",
        "story_qa": "qa/story-report.json",
        "editorial_qa": "qa/editorial-report.json",
        "journey_qa": "quality/journey-report.json",
        "visual_qa": "qa/video-report.json",
        "presentation_qa": "qa/presentation-report.json",
        "synchronization_qa": "qa/synchronization-report.json",
        "multimodal_qa": "qa/multimodal-report.json",
    }
    for layer, relative in report_layers.items():
        path = root / relative
        if not path.is_file():
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            missing.append(f"invalid_{layer}")
            continue
        if report.get("hard_failures"):
            missing.append(f"{layer}_failed")
    # Creation is a special causal claim. Its rehearsal proof and the clean
    # production trace must agree on a real outcome witness; a directory full
    # of artifacts is not sufficient evidence that the video demonstrated it.
    objective_path = root / "objective.json"
    try:
        objective = (
            json.loads(objective_path.read_text(encoding="utf-8"))
            if objective_path.is_file()
            else {}
        )
    except (OSError, ValueError, TypeError):
        objective = {}
    if "create_isolated_record" in objective.get("permitted_mutations", []):
        rehearsal_path = root / "discovery" / "rehearsal-report.json"
        trace_path = root / "execution" / "trace.json"
        try:
            rehearsal = json.loads(rehearsal_path.read_text(encoding="utf-8"))
            target = rehearsal.get("outcome_target") or {}
            witness = str(target.get("name", "")).casefold()
            witness_url = str(target.get("source_url", "")).casefold()
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            submitted = [
                event
                for event in trace.get("events", [])
                if event.get("kind") == "Submit" and event.get("success")
            ]
            def _event_proves_creation(event: dict[str, object]) -> bool:
                after = event.get("after") or {}
                if not isinstance(after, dict):
                    return False
                serialized = json.dumps(after).casefold()
                if witness and witness in serialized:
                    return True
                if witness_url and witness_url in str(after.get("url", "")).casefold():
                    return True
                # Creation pages commonly receive a server-generated identifier,
                # so the rehearsal URL is intentionally not stable across runs.
                # The execution kernel records the independent, visible values
                # matched on the resulting state; those values plus a changed
                # route are the authoritative production witness in that case.
                verified_outcome = after.get("verified_outcome")
                if not isinstance(verified_outcome, dict):
                    return False
                matched_values = verified_outcome.get("matched_form_values")
                return bool(
                    after.get("url_changed")
                    or event.get("state_delta", {}).get("url_changed")
                ) and isinstance(matched_values, list) and bool(matched_values)

            proved = any(_event_proves_creation(event) for event in submitted)
            if (not witness and not witness_url) or not submitted or not proved:
                missing.append("production_creation_outcome_proof")
        except (OSError, ValueError, TypeError):
            missing.append("rehearsal_or_production_creation_evidence")
    return {
        "run_root": str(root),
        "complete_evidence": not missing and delivery_declared,
        "delivery_declared": delivery_declared,
        "missing_layers": missing,
        "plan_consistency_failures": consistency_failures,
        "checks": checks,
    }
