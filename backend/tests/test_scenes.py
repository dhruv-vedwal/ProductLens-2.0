from datetime import UTC, datetime

from productlens.contracts.models import DemoTrace, InteractionEvent, OperationKind, Rect, Viewport
from productlens.presentation.scenes import build_scene_plan, inspect_scene_plan


def test_scene_plan_gives_scroll_a_human_reveal_dwell():
    trace = DemoTrace(run_id="scene", objective="Demo", started_at=datetime.now(UTC), events=[InteractionEvent(operation_id="one", kind=OperationKind.SCROLL_TO, intent="Reveal projects", before={}, after={}, success=True, duration_ms=1)])
    scenes = build_scene_plan(trace)
    assert scenes[0]["phase"] == "reveal"
    assert scenes[0]["dwell_seconds"] >= 1.6
    assert inspect_scene_plan(trace, scenes)["hard_failures"] == []


def test_scene_plan_attaches_a_page_completion_contract_to_visible_evidence():
    trace = DemoTrace(run_id="page", objective="Demo", started_at=datetime.now(UTC), events=[InteractionEvent(
        operation_id="projects", kind=OperationKind.SCROLL_TO, intent="Read featured projects",
        before={}, after={}, success=True, duration_ms=1, page_url="https://example.test/projects/",
    )])
    scene = build_scene_plan(trace)[0]
    assert scene["page_stage"] == "explore"
    assert scene["page_key"] == "https://example.test/projects"
    assert scene["page_contract"]["complete_only_after"].startswith("visible evidence")
    assert scene["scroll"]["continuity"] == "intermediate-landmarks-and-settle"


def test_scene_plan_moves_caption_above_a_lower_active_target():
    trace = DemoTrace(run_id="caption-zone", objective="Demo", started_at=datetime.now(UTC), events=[InteractionEvent(
        operation_id="form", kind=OperationKind.FILL_TEXT, intent="Enter name",
        target_rect=Rect(x=280, y=650, width=360, height=48), viewport=Viewport(width=1440, height=900),
        before={}, after={}, success=True, duration_ms=1,
    )])

    assert build_scene_plan(trace)[0]["caption_safe_zone"] == "top"
