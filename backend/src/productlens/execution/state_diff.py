"""Provider-neutral state-delta helpers for action verification and QA.

Snapshots are intentionally small dictionaries so the execution engine can
work with Playwright, Stagehand, or a test adapter.  This module gives every
provider the same definition of a meaningful observation change without
depending on DOM selectors or a product-specific event log.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# These fields are timestamps/implementation details, not product state.  A
# changed screenshot reference or grounding strategy must never make a no-op
# action look successful.
_IGNORED_FIELDS = {
    "captured_at",
    "grounding_strategy",
    "screenshot_ref",
    "screenshot_path",
    "event_log",
}
_OBSERVABLE_FIELDS = {
    "url",
    "title",
    "text",
    "visible_text",
    "page_text_hash",
    "value",
    "input_value",
    "attributes",
    "target_available",
    "visible",
    "exists",
    "dom_hash",
    "accessibility_hash",
    "loading",
}


def _normalise(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _normalise(item)
            for key, item in sorted(value.items(), key=lambda entry: str(entry[0]))
            if str(key) not in _IGNORED_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    return value


def state_delta(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Return a deterministic, redacted summary of observable state changes."""

    left: Mapping[str, Any] = before if before is not None else {}
    right: Mapping[str, Any] = after if after is not None else {}
    changed: list[str] = []
    for field in sorted(_OBSERVABLE_FIELDS):
        if _normalise(left.get(field)) != _normalise(right.get(field)):
            changed.append(field)
    # Scroll is useful evidence but is handled separately by the presentation
    # layer.  Include only its displacement, never arbitrary provider payloads.
    left_scroll_value = left.get("scroll")
    right_scroll_value = right.get("scroll")
    left_scroll: Mapping[str, Any] = (
        left_scroll_value if isinstance(left_scroll_value, Mapping) else {}
    )
    right_scroll: Mapping[str, Any] = (
        right_scroll_value if isinstance(right_scroll_value, Mapping) else {}
    )
    scroll_changed = any(left_scroll.get(axis) != right_scroll.get(axis) for axis in ("x", "y"))
    if scroll_changed:
        changed.append("scroll")
    return {
        "changed_fields": changed,
        "meaningful": bool(changed),
        "url_changed": "url" in changed,
        "content_changed": bool(
            set(changed)
            & {
                "title",
                "text",
                "visible_text",
                "value",
                "input_value",
                "attributes",
                "dom_hash",
                "accessibility_hash",
            }
        ),
        "scroll_changed": scroll_changed,
    }


def requires_observable_change(operation_kind: str) -> bool:
    """Whether a dispatched operation should prove a state change.

    Read-only observations and navigation are allowed to leave the target
    unchanged; mutating gestures must carry an explicit postcondition in the
    planner, while this predicate supplies a provider-independent audit hint.
    """

    return operation_kind in {
        "FillText",
        "FillEmail",
        "FillPhone",
        "SelectOption",
        "SelectDate",
        "SelectDateRange",
        "Check",
        "Uncheck",
        "ChooseRadio",
        "Search",
        "ApplyFilter",
        "Submit",
        "CreateRecord",
        "KeyPress",
        "Drag",
        "Upload",
        "PointerSequence",
    }
