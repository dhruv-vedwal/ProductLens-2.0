from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from productlens.contracts.models import DemoTrace


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
        for source in sorted(path for path in self.root.rglob("*") if path.is_file() and path.name != "artifact-manifest.json"):
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            entries.append({
                "path": source.relative_to(self.root).as_posix(),
                "bytes": source.stat().st_size,
                "sha256": digest.hexdigest(),
            })
        return self.write_json("artifact-manifest.json", {"run_id": self.root.name, "artifacts": entries})

    def verify_manifest(self) -> dict[str, Any]:
        """Verify every manifest entry and report missing or mutated artifacts."""
        manifest_path = self.root / "artifact-manifest.json"
        if not manifest_path.is_file():
            return {"valid": False, "missing_manifest": True, "missing": [], "mutated": []}
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries = payload.get("artifacts", [])
        except (OSError, ValueError, TypeError):
            return {"valid": False, "missing_manifest": False, "invalid_manifest": True, "missing": [], "mutated": []}
        missing: list[str] = []
        mutated: list[str] = []
        for entry in entries:
            relative = str(entry.get("path", ""))
            path = self.root / relative
            if not path.is_file():
                missing.append(relative)
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != entry.get("sha256") or path.stat().st_size != entry.get("bytes"):
                mutated.append(relative)
        return {"valid": not missing and not mutated, "missing_manifest": False, "missing": missing, "mutated": mutated}

    def required_url_delivery_artifacts(self) -> dict[str, bool]:
        """Require the complete evidence chain for a live product delivery.

        Fixture gates intentionally test isolated mechanics. A URL demo must
        additionally prove discovery, selection, direction, and editorial
        artifacts before it is accepted as a real walkthrough.
        """
        paths = {
            "objective": self.root / "objective.json",
            "exploration_report": self.root / "exploration-report.json",
            "page_knowledge": self.root / "page-knowledge",
            "feature_graph": self.root / "feature-graph.json",
            "candidate_flows": self.root / "candidate-flows.json",
            "plan": self.root / "plan.json",
            "editorial_brief": self.presentation / "editorial-brief.json",
            "storyboard": self.presentation / "storyboard.json",
            "scene_plan": self.presentation / "scene-plan.json",
            "validated_scene_plan": self.presentation / "validated-scene-plan.json",
            "narration_script": self.presentation / "narration-script.json",
            "captions": self.presentation / "rendered-captions.json",
            "coverage_qa": self.qa / "coverage-report.json",
            "journey_qa": self.root / "quality" / "journey-report.json",
            "artifact_manifest": self.root / "artifact-manifest.json",
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
            "NARRATION": ("discovery", "plan.json", "execution", "presentation", "qa/story-report.json", "qa/execution-report.json"),
            # Preserve every predecessor artifact required to evaluate the
            # child delivery.  A render retry must not fail merely because it
            # intentionally skipped discovery/execution and therefore lost
            # the inherited coverage or journey evidence.
            "RENDER": (
                "discovery", "plan.json", "execution", "presentation", "audio", "quality",
                "qa/story-report.json", "qa/execution-report.json", "qa/coverage-report.json",
                "qa/editorial-report.json",
            ),
            "VIDEO_QA": (
                "discovery", "plan.json", "execution", "presentation", "audio", "quality", "render", "final",
                "qa/story-report.json", "qa/execution-report.json", "qa/coverage-report.json",
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
