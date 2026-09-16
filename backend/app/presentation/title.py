"""Concise, trace-safe titles for the opening demo frame."""

from __future__ import annotations

import re
from collections.abc import Sequence


def concise_demo_title(
    objective: str, max_words: int = 7, action_labels: Sequence[str] | None = None
) -> str:
    """Keep the meaningful objective phrase without inventing product claims."""
    cleaned = re.sub(r"\s+", " ", objective.strip().rstrip(".!?"))
    prefixes = ("show how to ", "show ", "demonstrate ", "create a focused product demo of ")
    lower = cleaned.lower()
    for prefix in prefixes:
        if lower.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    # Requests often add safety constraints after the actual walkthrough goal.
    # They belong in the execution policy, not in the opening title card.
    cleaned = re.split(
        r"\s*(?:[.!?]\s+|;\s+)(?:do not|don't|never|avoid)\b",
        cleaned,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    words = cleaned.split()
    if not words:
        return "Product walkthrough"
    if len(words) > max_words and action_labels:
        action_title = _action_sequence_title(action_labels, max_words=max_words)
        if action_title:
            return action_title
    title = " ".join(words[:max_words])
    return title[:1].upper() + title[1:]


def _action_sequence_title(action_labels: Sequence[str], *, max_words: int) -> str | None:
    """Turn trace-backed action labels into a compact editorial chapter title."""
    labels: list[str] = []
    for action in action_labels:
        label = re.sub(
            r"^(?:start from|open|review|view|show|go to|navigate to|select|choose)\s+",
            "",
            action.strip(),
            flags=re.IGNORECASE,
        ).strip(" .,:;")
        # Execution intents often include their verification purpose (for
        # example, "Open a detail view to inspect its contents"). Keep the visible
        # destination only when building an opening chapter title.
        label = re.split(r"\s+(?:to|so|where)\s+", label, maxsplit=1, flags=re.IGNORECASE)[0]
        label = re.sub(r"^(?:the|a|an)\s+", "", label, flags=re.IGNORECASE)
        label = " ".join(label.split()[:3])
        if label and label.lower() not in {item.lower() for item in labels}:
            labels.append(label)
    if not labels:
        return None
    while len(labels) > 1 and len(" ".join(labels).split()) > max_words:
        labels.pop()
    if len(labels) == 1:
        return labels[0][:1].upper() + labels[0][1:]
    if len(labels) == 2:
        return f"{labels[0]} & {labels[1]}"
    return f"{', '.join(labels[:-1])} & {labels[-1]}"
