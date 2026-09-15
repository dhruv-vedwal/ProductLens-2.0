from productlens.execution.state_diff import requires_observable_change, state_delta


def test_state_delta_ignores_provider_noise_and_reports_content_change():
    before = {
        "url": "https://example.test",
        "text": "Before",
        "grounding_strategy": "role",
        "screenshot_ref": "a.png",
        "event_log": "click",
    }
    after = {
        "url": "https://example.test",
        "text": "After",
        "grounding_strategy": "selector",
        "screenshot_ref": "b.png",
        "event_log": "click,change",
    }
    delta = state_delta(before, after)
    assert delta["meaningful"] is True
    assert delta["content_changed"] is True
    assert delta["changed_fields"] == ["text"]


def test_state_delta_records_scroll_without_treating_it_as_content_change():
    delta = state_delta({"scroll": {"x": 0, "y": 0}}, {"scroll": {"x": 0, "y": 480}})
    assert delta == {
        "changed_fields": ["scroll"],
        "meaningful": True,
        "url_changed": False,
        "content_changed": False,
        "scroll_changed": True,
    }


def test_mutating_gestures_require_observable_witnesses():
    assert requires_observable_change("Drag")
    assert requires_observable_change("Upload")
    assert not requires_observable_change("ReadValue")


def test_state_delta_treats_accessibility_changes_as_content_evidence():
    delta = state_delta(
        {"accessibility_hash": "closed", "dom_hash": "same"},
        {"accessibility_hash": "open", "dom_hash": "same"},
    )
    assert delta["meaningful"] is True
    assert delta["content_changed"] is True
