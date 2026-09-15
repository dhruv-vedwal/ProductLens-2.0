import json
from pathlib import Path

from productlens.contracts.models import DemoTrace

ROOT = Path(__file__).parents[1] / "validation" / "golden"


def test_golden_trace_is_provider_neutral_and_presentation_safe():
    payload = json.loads((ROOT / "demo-trace.json").read_text(encoding="utf-8"))
    trace = DemoTrace.model_validate(payload)

    assert trace.outcome_verified is True
    assert trace.browser_zoom_percent == 100
    assert all("smartsevak" not in event.intent.casefold() for event in trace.events)
    assert trace.events[1].scroll_path
    assert trace.events[1].covered_content_groups == ["result"]


def test_golden_presentation_baseline_keeps_camera_and_frame_invariants():
    baseline = json.loads((ROOT / "presentation-baseline.json").read_text(encoding="utf-8"))
    requirements = baseline["requirements"]

    assert requirements["native_browser_frame"] is True
    assert requirements["browser_zoom_percent"] == 100
    assert requirements["camera_scale_max"] <= 1.35
    assert requirements["no_crop"] is True
