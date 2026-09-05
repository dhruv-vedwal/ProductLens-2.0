from productlens.narration.script import captions_from_duration


def test_caption_only_timing_is_even_and_includes_all_evidence():
    captions = captions_from_duration(
        [{"event_id": "one", "text": "Open leads"}, {"event_id": "two", "text": "Fill form"}],
        8,
    )
    assert captions == [
        {"start": 0.0, "end": 4.0, "text": "Open leads", "scene_id": "one"},
        {"start": 4.0, "end": 8.0, "text": "Fill form", "scene_id": "two"},
    ]
