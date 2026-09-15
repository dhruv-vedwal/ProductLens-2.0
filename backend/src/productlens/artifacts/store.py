from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from productlens.contracts.models import (
    ActionAttempt,
    ActionIntent,
    DemoTrace,
    OperationKind,
    StateSnapshot,
    VerificationResult,
)


def _sha256_file(path: Path) -> str:
    """Hash video and trace evidence without materialising it in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def materialize_trace_lifecycle(trace: DemoTrace) -> DemoTrace:
    """Derive explicit lifecycle records from the canonical interaction trace.

    Older executors only emitted ``InteractionEvent`` records.  Materialising
    these typed views keeps retries/backward compatibility while giving QA and
    operators durable state, attempt, and verification boundaries.
    """
    if trace.state_snapshots or trace.action_attempts or trace.verification_results:
        return trace
    snapshots: list[StateSnapshot] = []
    attempts: list[ActionAttempt] = []
    verifications: list[VerificationResult] = []
    gesture_for = {
        OperationKind.NAVIGATE: "click",
        OperationKind.OPEN_NAVIGATION_ITEM: "click",
        OperationKind.CLICK: "click",
        OperationKind.FILL_TEXT: "type",
        OperationKind.FILL_EMAIL: "type",
        OperationKind.FILL_PHONE: "type",
        OperationKind.SELECT_OPTION: "click",
        OperationKind.SELECT_DATE: "click",
        OperationKind.SCROLL_TO: "scroll",
        OperationKind.HOVER: "hover",
        OperationKind.KEY_PRESS: "key",
        OperationKind.WAIT_FOR_STATE: "wait",
        OperationKind.READ_VALUE: "observe",
        OperationKind.VERIFY_STATE: "verify",
        OperationKind.POINTER_SEQUENCE: "pointer_sequence",
        OperationKind.DRAG: "drag",
        OperationKind.UPLOAD: "upload",
        OperationKind.SUBMIT: "submit",
        OperationKind.CREATE_RECORD: "submit",
    }
    for event in trace.events:
        before_payload = event.before or {}
        after_payload = event.after or {}
        before_id = f"state-before:{event.id}"
        after_id = f"state-after:{event.id}"
        for state_id, payload in ((before_id, before_payload), (after_id, after_payload)):
            encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
            snapshots.append(
                StateSnapshot(
                    id=state_id,
                    url=event.page_url or "",
                    title=str(payload.get("title") or ""),
                    visible_text_hash=hashlib.sha256(encoded).hexdigest(),
                    screenshot_ref=event.screenshot_path,
                    viewport=event.viewport,
                    scroll=dict(event.scroll),
                    evidence_refs=[f"trace:event:{event.id}"],
                )
            )
        gesture = gesture_for.get(event.kind)
        target = event.target
        if gesture and (gesture in {"observe", "wait", "verify"} or target is not None):
            parameters: dict[str, Any] = {}
            if gesture == "pointer_sequence":
                parameters["points"] = event.scroll_path or [{"x": 0, "y": 0}]
            if gesture == "drag":
                # A legacy event may not carry a destination; skip it rather
                # than inventing one in the lifecycle projection.
                continue
            intent_kwargs: dict[str, Any] = {
                "id": event.operation_id,
                "goal": event.intent[:240] or "Observed browser action",
                "gesture": gesture,
                "target": target,
                "parameters": parameters,
            }
            if gesture == "submit":
                # Submit lifecycle records retain the exact expected witness
                # from the operation; no lifecycle projection may invent a
                # success target after the fact.
                if not event.postconditions:
                    continue
                intent_kwargs.update(
                    {
                        "expected_state": list(event.postconditions),
                        "side_effect_policy": "authorized_mutation",
                    }
                )
            intent = ActionIntent(**intent_kwargs)
            verification = VerificationResult(
                intent_id=intent.id,
                status="passed" if event.success else "failed",
                observed_state_id=after_id,
                evidence_refs=[f"trace:event:{event.id}"],
                confidence=1.0 if event.success else 0.0,
                reason="interaction event completed"
                if event.success
                else "interaction event failed",
            )
            attempts.append(
                ActionAttempt(
                    id=f"attempt:{event.id}",
                    intent=intent,
                    dispatched=event.action_at is not None or event.success,
                    before_state_id=before_id,
                    after_state_id=after_id,
                    verification=verification,
                    error=None if event.success else "interaction event failed",
                )
            )
            verifications.append(verification)
    return trace.model_copy(
        update={
            "state_snapshots": snapshots,
            "action_attempts": attempts,
            "verification_results": verifications,
        }
    )


class RunArtifacts:
    """Creates the plan-defined evidence layout without overwriting another run."""

    def __init__(self, root: Path, run_id: str):
        self.root = root / "runs" / run_id
        self.execution = self.root / "execution"
        self.screenshots = self.execution / "screenshots"
        self.presentation = self.root / "presentation"
        self.qa = self.root / "qa"
        for directory in (self.screenshots, self.presentation, self.qa, self.root / "final"):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def remove_empty_run_directory(root: Path, run_id: str) -> bool:
        """Remove only an artifact directory that contains no evidence files.

        Retention must never use filesystem emptiness as a broad deletion
        signal. Resolve the exact run directory below the configured artifact
        root, reject any file (including an unregistered crash artifact), then
        remove its now-empty directory tree. The database retention boundary
        calls this before deleting its matching run record.
        """
        base = (root / "runs").resolve()
        candidate = (base / run_id).resolve()
        if candidate.parent != base:
            raise ValueError("invalid run artifact directory")
        if not candidate.exists():
            return False
        if any(path.is_file() for path in candidate.rglob("*")):
            raise ValueError("run artifact directory contains retained evidence")
        shutil.rmtree(candidate)
        return True

    def screenshot_path(self, event_index: int) -> Path:
        return self.screenshots / f"{event_index:03d}.png"

    def write_json(self, relative_path: str, value: Any) -> Path:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # A worker crash must leave either the prior complete artifact or the
        # new complete artifact, never a truncated JSON file that a retry
        # mistakes for valid evidence.
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")
        # Windows can briefly retain a read handle on a just-written artifact
        # (notably from a renderer, virus scanner, or test process). Keep the
        # all-or-nothing replacement guarantee, but tolerate that short race
        # rather than failing a durable generation stage spuriously.
        for attempt in range(4):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.05 * (attempt + 1))
        return path

    def save_trace(self, trace: DemoTrace) -> Path:
        trace = materialize_trace_lifecycle(trace)
        self.write_json(
            "execution/state-snapshots.json",
            [item.model_dump(mode="json") for item in trace.state_snapshots],
        )
        self.write_json(
            "execution/action-attempts.json",
            [item.model_dump(mode="json") for item in trace.action_attempts],
        )
        self.write_json(
            "execution/verification-results.json",
            [item.model_dump(mode="json") for item in trace.verification_results],
        )
        return self.write_json("execution/trace.json", trace.model_dump(mode="json"))

    def preserve_browser_video(self, source: Path | None) -> Path | None:
        if source is None or not source.exists():
            return None
        destination = self.execution / "browser-recording.webm"
        shutil.copy2(source, destination)
        return destination

    def browser_video_path(self) -> Path:
        """Return the owned source recording, preferring provider-native MP4."""
        native = self.execution / "browser-recording.mp4"
        return native if native.exists() else self.execution / "browser-recording.webm"

    def required_delivery_artifacts(self) -> dict[str, bool]:
        paths = {
            "trace": self.execution / "trace.json",
            "browser_video": self.browser_video_path(),
            "playwright_trace": self.execution / "playwright-trace.zip",
            "presentation": self.presentation / "presentation-plan.json",
            "execution_qa": self.qa / "execution-report.json",
            "story_qa": self.qa / "story-report.json",
            "editorial_qa": self.qa / "editorial-report.json",
            "video_qa": self.qa / "video-report.json",
            "presentation_qa": self.qa / "presentation-report.json",
            "final_video": self.root / "final" / "demo.mp4",
        }
        return {name: path.exists() and path.stat().st_size > 0 for name, path in paths.items()}

    def write_manifest(self) -> Path:
        """Persist a deterministic checksum manifest for the completed run."""
        entries: list[dict[str, Any]] = []
        # ``run-status.json`` is a mutable operational checkpoint updated by
        # the worker after each lifecycle transition.  It is deliberately not
        # part of the immutable evidence manifest; hashing it would make a
        # valid delivery appear tampered merely because the job reached its
        # terminal state after publication.
        for source in sorted(
            path
            for path in self.root.rglob("*")
            if path.is_file() and path.name not in {"artifact-manifest.json", "run-status.json"}
        ):
            entries.append(
                {
                    "path": source.relative_to(self.root).as_posix(),
                    "bytes": source.stat().st_size,
                    "sha256": _sha256_file(source),
                }
            )
        return self.write_json(
            "artifact-manifest.json", {"run_id": self.root.name, "artifacts": entries}
        )

    def verify_manifest(self) -> dict[str, Any]:
        """Verify every manifest entry and report missing or mutated artifacts."""
        manifest_path = self.root / "artifact-manifest.json"
        if not manifest_path.is_file():
            return {"valid": False, "missing_manifest": True, "missing": [], "mutated": []}
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries = payload.get("artifacts", [])
        except (OSError, ValueError, TypeError):
            return {
                "valid": False,
                "missing_manifest": False,
                "invalid_manifest": True,
                "missing": [],
                "mutated": [],
            }
        missing: list[str] = []
        mutated: list[str] = []
        for entry in entries:
            relative = str(entry.get("path", ""))
            path = self.root / relative
            if not path.is_file():
                missing.append(relative)
                continue
            digest = _sha256_file(path)
            if digest != entry.get("sha256") or path.stat().st_size != entry.get("bytes"):
                mutated.append(relative)
        return {
            "valid": not missing and not mutated,
            "missing_manifest": False,
            "missing": missing,
            "mutated": mutated,
        }

    def required_url_delivery_artifacts(self) -> dict[str, bool]:
        """Require the complete evidence chain for a live product delivery.

        Fixture gates intentionally test isolated mechanics. A URL demo must
        additionally prove discovery, selection, direction, and editorial
        artifacts before it is accepted as a real walkthrough.
        """
        paths = {
            "objective": self.root / "objective.json",
            "objective_understanding": self.root / "discovery" / "objective-understanding.json",
            "exploration_report": self.root / "exploration-report.json",
            "product_knowledge": self.root / "discovery" / "product-knowledge.json",
            "page_knowledge": self.root / "page-knowledge",
            "feature_graph": self.root / "feature-graph.json",
            "candidate_flows": self.root / "candidate-flows.json",
            "relevance_graph": self.root / "discovery" / "relevance-graph.json",
            "demo_brief": self.root / "planning" / "demo-brief.json",
            "capability_resolutions": self.root / "planning" / "capability-resolutions.json",
            "validated_state_graph": self.root / "planning" / "validated-state-graph.json",
            "plan": self.root / "plan.json",
            "plan_consistency": self.qa / "plan-consistency-report.json",
            "editorial_brief": self.presentation / "editorial-brief.json",
            "storyboard": self.presentation / "storyboard.json",
            "scene_plan": self.presentation / "scene-plan.json",
            "validated_scene_plan": self.presentation / "validated-scene-plan.json",
            "actual_flow_storyboard": self.presentation / "actual-flow-storyboard.json",
            "narration_script": self.presentation / "narration-script.json",
            "fact_extraction": self.root / "narration" / "fact-extraction.json",
            "captions": self.presentation / "rendered-captions.json",
            "coverage_qa": self.qa / "coverage-report.json",
            "journey_qa": self.root / "quality" / "journey-report.json",
            "artifact_manifest": self.root / "artifact-manifest.json",
            "state_snapshots": self.execution / "state-snapshots.json",
            "action_attempts": self.execution / "action-attempts.json",
            "verification_results": self.execution / "verification-results.json",
        }
        required = {
            **self.required_delivery_artifacts(),
            **{
                name: (
                    any(path.glob("*.json"))
                    if name == "page_knowledge"
                    else path.exists() and path.stat().st_size > 0
                )
                for name, path in paths.items()
            },
        }
        # An explicitly authorised creation demo must prove the separate
        # rehearsal boundary. Without this artifact a final render could show
        # a submit while silently lacking the independent outcome witness that
        # made the production workflow safe to replay.
        objective_path = self.root / "objective.json"
        try:
            objective = (
                json.loads(objective_path.read_text(encoding="utf-8"))
                if objective_path.is_file()
                else {}
            )
        except (OSError, ValueError, TypeError):
            objective = {}
        if "create_isolated_record" in objective.get("permitted_mutations", []):
            rehearsal = self.root / "discovery" / "rehearsal-report.json"
            required["rehearsal_outcome"] = rehearsal.is_file() and rehearsal.stat().st_size > 0
        # Browserbase production uses Session Replay plus the ProductLens
        # DemoTrace instead of starting a local Playwright trace. Accept that
        # verified native pair as equivalent trace evidence for live delivery.
        native_meta = self.execution / "browserbase-recording.json"
        if (
            not required.get("playwright_trace", False)
            and required.get("browser_video", False)
            and native_meta.is_file()
        ):
            try:
                payload = json.loads(native_meta.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                payload = {}
            native_status = payload.get("native_recording")
            if (
                payload.get("provider") == "browserbase"
                and native_status not in {"unavailable", "failed"}
            ) or native_status not in {None, "unavailable", "failed"}:
                required["playwright_trace"] = True
        return required

    @classmethod
    def clone_for_targeted_retry(
        cls, root: Path, parent_run_id: str, retry_run_id: str, *, start_stage: str
    ) -> None:
        """Copy only immutable predecessor evidence into an auditable child run."""
        parent = root / "runs" / parent_run_id
        retry = root / "runs" / retry_run_id
        if not parent.is_dir():
            raise FileNotFoundError(f"parent run artifacts are unavailable: {parent_run_id}")
        retry.mkdir(parents=True, exist_ok=True)
        # A rerender needs trace/presentation/narration, while an execution
        # retry needs only discovery/plan. Never copy a prior final video or its
        # delivery verdict into a child run.
        allowed = {
            "DISCOVERY": ("discovery",),
            "PLANNING": ("discovery",),
            "EXECUTION": ("discovery", "plan.json"),
            "NARRATION": (
                "discovery",
                "plan.json",
                "execution",
                "presentation",
                "qa/story-report.json",
                "qa/execution-report.json",
            ),
            # Preserve every predecessor artifact required to evaluate the
            # child delivery.  A render retry must not fail merely because it
            # intentionally skipped discovery/execution and therefore lost
            # the inherited coverage or journey evidence.
            "RENDER": (
                "discovery",
                "plan.json",
                "execution",
                "presentation",
                "audio",
                "quality",
                "qa/story-report.json",
                "qa/execution-report.json",
                "qa/coverage-report.json",
                "qa/editorial-report.json",
            ),
            "VIDEO_QA": (
                "discovery",
                "plan.json",
                "execution",
                "presentation",
                "audio",
                "quality",
                "render",
                "final",
                "qa/story-report.json",
                "qa/execution-report.json",
                "qa/coverage-report.json",
                "qa/editorial-report.json",
            ),
        }
        # Discovery writes both its browser evidence directory and root-level
        # architectural artifacts.  Any retry after discovery must inherit
        # both; otherwise a perfectly valid planning/execution/render child is
        # rejected by delivery QA merely because it intentionally did not run
        # discovery again.
        relatives = list(allowed[start_stage])
        if start_stage != "DISCOVERY":
            relatives.extend(
                (
                    "objective.json",
                    "exploration-report.json",
                    "page-knowledge",
                    "feature-graph.json",
                    "candidate-flows.json",
                )
            )
        for relative in relatives:
            source = parent / relative
            destination = retry / relative
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
            elif source.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
