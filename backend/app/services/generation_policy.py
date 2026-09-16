"""Small, pure policies shared by generation stages.

The generation service coordinates browser stages; it should not also own
route identity, media diagnostics, or duration arithmetic. These helpers have
no provider or browser lifecycle and are deliberately easy to test in
isolation.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from app.contracts.models import DemoPlan
from app.urls import canonical_product_url


def normalise_observed_selector(selector: str | None) -> str | None:
    """Repair only mechanically truncated attribute selectors from DOM probes."""
    if not selector:
        return selector
    if selector.startswith("[") and selector.count("]") < selector.count("["):
        return selector + "]" * (selector.count("[") - selector.count("]"))
    return selector


def safe_render_error(message: str, *, limit: int = 2_000) -> str:
    """Return a bounded render diagnostic with credential-like values removed."""
    text = " ".join((message or "").split())
    text = re.sub(
        r"(?i)(\bauthorization\b\s*[:=]\s*(?:bearer\s+)?)([^\s,;]+)",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)(\b(?:api[_-]?key|access[_-]?key|token|password|passcode|secret|authorization)\b\s*[:=]\s*)([^\s,;]+)",
        r"\1[REDACTED]",
        text,
    )
    return text[-limit:] if len(text) > limit else text


def recording_frame_rate(path: Path) -> float | None:
    """Read native capture frame rate without turning it into a render guess."""
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=avg_frame_rate",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        payload = json.loads(probe.stdout or "{}")
        value = (payload.get("streams") or [{}])[0].get("avg_frame_rate")
        if isinstance(value, str) and "/" in value:
            numerator, denominator = value.split("/", 1)
            rate = float(numerator) / float(denominator)
        else:
            rate = float(value)
        return rate if rate > 0 else None
    except (OSError, ValueError, TypeError, ZeroDivisionError, json.JSONDecodeError):
        return None


def production_duration_envelope(plan: DemoPlan) -> tuple[int | None, dict[str, object] | None]:
    """Return the bounded native-edit envelope for a production plan."""
    maximum = plan.maximum_duration_seconds
    if maximum is None or plan.target_duration_seconds < 180:
        return maximum, None
    allowance = 6
    return maximum + allowance, {
        "requested_maximum_seconds": maximum,
        "effective_maximum_seconds": maximum + allowance,
        "allowance_seconds": allowance,
        "reason": "bounded native-speed editorial/title-close accounting for thorough walkthrough",
    }


def canonical_url(value: str) -> str:
    """Compare browser states, not incidental redirect spelling."""
    return canonical_product_url(value)
