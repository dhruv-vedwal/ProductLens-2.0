from datetime import UTC, datetime

from productlens.contracts.models import DemoTrace, InteractionEvent, OperationKind, Rect, Viewport
from productlens.presentation.scenes import build_scene_plan, inspect_scene_plan


def test_scene_plan_gives_scroll_a_human_reveal_dwell():
    trace = DemoTrace(
        run_id="scene",
        objective="Demo",
        started_at=datetime.now(UTC),
        events=[
            InteractionEvent(
                operation_id="one",
                kind=OperationKind.SCROLL_TO,
                intent="Reveal projects",
                before={},
                after={},
                success=True,
                duration_ms=1,
            )
        ],
    )
    scenes = build_scene_plan(trace)
    assert scenes[0]["phase"] == "reveal"
    assert scenes[0]["dwell_seconds"] >= 1.6
    assert inspect_scene_plan(trace, scenes)["hard_failures"] == []


def test_scene_plan_attaches_a_page_completion_contract_to_visible_evidence():
    trace = DemoTrace(
        run_id="page",
        objective="Demo",
        started_at=datetime.now(UTC),
        events=[
            InteractionEvent(
                operation_id="projects",
                kind=OperationKind.SCROLL_TO,
                intent="Read featured projects",
                before={},
                after={},
                success=True,
                duration_ms=1,
                page_url="https://example.test/projects/",
            )
        ],
    )
    scene = build_scene_plan(trace)[0]
    assert scene["page_stage"] == "explore"
    assert scene["page_key"] == "https://example.test/projects"
    assert scene["page_contract"]["complete_only_after"].startswith("visible evidence")
    assert scene["scroll"]["continuity"] == "intermediate-landmarks-and-settle"


def test_scene_plan_moves_caption_above_a_lower_active_target():
    trace = DemoTrace(
        run_id="caption-zone",
        objective="Demo",
        started_at=datetime.now(UTC),
        events=[
            InteractionEvent(
                operation_id="form",
                kind=OperationKind.FILL_TEXT,
                intent="Enter name",
                target_rect=Rect(x=280, y=650, width=360, height=48),
                viewport=Viewport(width=1440, height=900),
                before={},
                after={},
                success=True,
                duration_ms=1,
            )
        ],
    )

    assert build_scene_plan(trace)[0]["caption_safe_zone"] == "top"


def test_scene_plan_moves_scroll_caption_above_a_long_content_region():
    trace = DemoTrace(
        run_id="caption-scroll",
        objective="Demo",
        started_at=datetime.now(UTC),
        events=[
            InteractionEvent(
                operation_id="section",
                kind=OperationKind.SCROLL_TO,
                intent="Reveal section details",
                target_rect=Rect(x=320, y=345, width=500, height=40),
                viewport=Viewport(width=1920, height=1080),
                before={},
                after={},
                success=True,
                duration_ms=1,
            )
        ],
    )

    assert build_scene_plan(trace)[0]["caption_safe_zone"] == "top"


def test_dense_upper_page_heading_keeps_caption_away_from_lower_rows():
    trace = DemoTrace(
        run_id="dense-upper",
        objective="Demo",
        started_at=datetime.now(UTC),
        events=[
            InteractionEvent(
                operation_id="open",
                kind=OperationKind.OPEN_NAVIGATION_ITEM,
                intent="Open results",
                target_rect=Rect(x=1100, y=20, width=90, height=36),
                viewport=Viewport(width=1920, height=1080),
                before={},
                after={},
                success=True,
                duration_ms=1,
            ),
            InteractionEvent(
                operation_id="heading",
                kind=OperationKind.READ_VALUE,
                intent="Explain the results list",
                target_rect=Rect(x=312, y=122, width=570, height=60),
                viewport=Viewport(width=1920, height=1080),
                before={},
                after={},
                success=True,
                duration_ms=1,
            ),
        ],
    )

    assert build_scene_plan(trace)[1]["caption_safe_zone"] == "top"


def test_scene_plan_keeps_midframe_form_captions_away_from_login_result_area():
    trace = DemoTrace(
        run_id="caption-mid",
        objective="Demo",
        started_at=datetime.now(UTC),
        events=[
            InteractionEvent(
                operation_id="email",
                kind=OperationKind.FILL_EMAIL,
                intent="Enter account email",
                target_rect=Rect(x=500, y=475, width=426, height=48),
                viewport=Viewport(width=1920, height=1080),
                before={},
                after={},
                success=True,
                duration_ms=1,
            )
        ],
    )

    assert build_scene_plan(trace)[0]["caption_safe_zone"] == "top"


def test_scene_plan_requests_meaningful_bounded_focus_for_visible_typing():
    trace = DemoTrace(
        run_id="form-focus",
        objective="Demo",
        started_at=datetime.now(UTC),
        events=[
            InteractionEvent(
                operation_id="form",
                kind=OperationKind.FILL_TEXT,
                intent="Enter customer name",
                target_rect=Rect(x=280, y=350, width=360, height=48),
                viewport=Viewport(width=1440, height=900),
                before={},
                after={},
                success=True,
                duration_ms=1,
            )
        ],
    )
    camera = build_scene_plan(trace)[0]["camera"]
    assert camera["mode"] == "target-focus"
    assert camera["zoom"] == 1.14


def test_scene_plan_requests_bounded_focus_for_a_compact_visible_scroll_target():
    trace = DemoTrace(
        run_id="focus",
        objective="Demo",
        started_at=datetime.now(UTC),
        events=[
            InteractionEvent(
                operation_id="feature",
                kind=OperationKind.SCROLL_TO,
                intent="Explain a compact feature card",
                target_rect=Rect(x=320, y=260, width=360, height=48),
                viewport=Viewport(width=1440, height=900),
                before={},
                after={},
                success=True,
                duration_ms=1,
            )
        ],
    )

    camera = build_scene_plan(trace)[0]["camera"]
    assert camera["mode"] == "target-focus"
    assert camera["zoom"] == 1.10


def test_dense_page_explanation_uses_top_caption_when_a_later_scroll_reveals_rows():
    trace = DemoTrace(
        run_id="dense-caption",
        objective="Demo",
        started_at=datetime.now(UTC),
        events=[
            InteractionEvent(
                operation_id="open",
                kind=OperationKind.OPEN_NAVIGATION_ITEM,
                intent="Open builds",
                page_url="https://example.test/builds",
                target_rect=Rect(x=1100, y=20, width=90, height=36),
                viewport=Viewport(width=1920, height=1080),
                before={},
                after={},
                success=True,
                duration_ms=1,
            ),
            InteractionEvent(
                operation_id="explain",
                kind=OperationKind.READ_VALUE,
                intent="Explain the challenge bank",
                page_url="https://example.test/builds",
                viewport=Viewport(width=1920, height=1080),
                before={},
                after={},
                success=True,
                duration_ms=1,
            ),
            InteractionEvent(
                operation_id="scroll",
                kind=OperationKind.SCROLL_TO,
                intent="Reveal challenge rows",
                page_url="https://example.test/builds",
                target_rect=Rect(x=300, y=350, width=500, height=40),
                viewport=Viewport(width=1920, height=1080),
                before={},
                after={},
                success=True,
                duration_ms=1,
            ),
        ],
    )
    scenes = build_scene_plan(trace)
    assert scenes[1]["caption_safe_zone"] == "top"


def test_navigation_explanation_stays_above_dense_destination_content():
    trace = DemoTrace(
        run_id="navigation-caption",
        objective="Demo",
        started_at=datetime.now(UTC),
        events=[
            InteractionEvent(
                operation_id="opening",
                kind=OperationKind.READ_VALUE,
                intent="Establish home",
                page_url="https://example.test/",
                viewport=Viewport(width=1920, height=1080),
                before={},
                after={},
                success=True,
                duration_ms=1,
            ),
            InteractionEvent(
                operation_id="nav",
                kind=OperationKind.OPEN_NAVIGATION_ITEM,
                intent="Open results",
                page_url="https://example.test/results",
                target_rect=Rect(x=1100, y=20, width=90, height=36),
                viewport=Viewport(width=1920, height=1080),
                before={},
                after={},
                success=True,
                duration_ms=1,
            ),
        ],
    )

    assert build_scene_plan(trace)[1]["caption_safe_zone"] == "top"
