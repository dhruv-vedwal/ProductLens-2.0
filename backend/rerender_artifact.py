"""Regenerate a delivery artifact from an already captured trace.

This is intentionally local-only: it does not open a browser or invoke an AI
provider, so editorial changes can be checked without consuming provider
credits.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from productlens.artifacts.store import RunArtifacts
from productlens.contracts.models import DemoTrace, PresentationPlan
from productlens.narration.script import captions_from_duration, script_from_trace
from productlens.presentation.scenes import build_scene_plan
from productlens.video.render import render_remotion


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--artifacts-root", default="artifacts")
    args = parser.parse_args()
    artifacts = RunArtifacts(Path(args.artifacts_root), args.run_id)
    trace = DemoTrace.model_validate_json(
        (artifacts.execution / "trace.json").read_text(encoding="utf-8")
    )
    presentation = PresentationPlan.model_validate_json(
        (artifacts.presentation / "presentation-plan.json").read_text(encoding="utf-8")
    )
    script = script_from_trace(trace)
    captions = captions_from_duration(script, max(3.0, len(trace.events) * 1.35))
    artifacts.write_json("presentation/captions.json", captions)
    artifacts.write_json(
        "presentation/narration-script.json", {"mode": "caption_only", "script": script}
    )
    output = render_remotion(
        trace,
        presentation,
        artifacts,
        captions=captions,
        scenes=build_scene_plan(trace),
        target_duration_seconds=None,
    )
    print(output)


if __name__ == "__main__":
    main()
