from datetime import UTC, datetime

from app.contracts.models import DemoTrace, InteractionEvent, OperationKind
from app.narration.script import (
    captions_from_audio_duration,
    captions_from_duration,
    captions_from_measured_segments,
    recommended_caption_duration,
    script_from_trace,
)


def test_narration_is_grounded_in_trace_events():
    trace = DemoTrace(
        run_id="r",
        objective="Demo",
        started_at=datetime.now(UTC),
        events=[
            InteractionEvent(
                operation_id="o",
                kind=OperationKind.FILL_TEXT,
                intent="Enter customer name",
                before={},
                after={"value": "Sarah"},
                success=True,
                duration_ms=1,
            )
        ],
    )
    script = script_from_trace(trace)
    assert (
        script[0]["text"]
        == "We enter the required details in Enter customer name so the next step has the right context."
    )
    assert captions_from_audio_duration(script, 3)[0]["end"] == 3


def test_collection_caption_is_a_viewer_journey_not_a_raw_scroll_command():
    trace = DemoTrace(
        run_id="collection",
        objective="Demo",
        started_at=datetime.now(UTC),
        events=[
            InteractionEvent(
                operation_id="one",
                kind=OperationKind.SCROLL_TO,
                intent="Reveal the project collection",
                before={},
                after={},
                success=True,
                duration_ms=1,
            )
        ],
    )
    assert "visible information" in script_from_trace(trace)[0]["text"]


def test_measured_audio_segments_own_caption_timing():
    script = [
        {"event_id": "first", "text": "First scene."},
        {"event_id": "second", "text": "Second scene."},
    ]
    captions = captions_from_measured_segments(script, [1.25, 2.75])

    assert captions == [
        {"start": 0.0, "end": 1.25, "text": "First scene.", "scene_id": "first"},
        {"start": 1.25, "end": 4.0, "text": "Second scene.", "scene_id": "second"},
    ]


def test_script_adapts_editorial_lens_to_audience():
    trace = DemoTrace(
        run_id="audience",
        objective="Demo",
        started_at=datetime.now(UTC),
        events=[
            InteractionEvent(
                operation_id="one",
                kind=OperationKind.CLICK,
                intent="Open details",
                before={},
                after={},
                success=True,
                duration_ms=1,
            )
        ],
    )
    assert "engineering context" in script_from_trace(trace, audience="recruiter")[0]["text"]
    assert (
        "implementation-relevant"
        in script_from_trace(trace, audience="technical developer")[0]["text"]
    )


def test_recommended_caption_duration_covers_each_scene_reading_requirement():
    script = [
        {"event_id": "one", "text": "A short line."},
        {
            "event_id": "two",
            "text": "This deliberately longer line needs enough time for silent reading.",
        },
    ]
    duration = recommended_caption_duration(script)
    captions = captions_from_duration(script, duration)
    requirements = [max(2.4, len(item["text"].split()) / 3.2 + 0.25) for item in script]
    assert all(
        caption["end"] - caption["start"] + 0.02 >= requirement
        for caption, requirement in zip(captions, requirements, strict=True)
    )
