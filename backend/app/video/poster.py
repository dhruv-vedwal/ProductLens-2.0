"""Extract a library poster frame from a finished demo video."""

from __future__ import annotations

import subprocess
from pathlib import Path


def write_video_poster(video: Path, poster: Path, *, second: float = 1.0) -> Path | None:
    """Best-effort ffmpeg poster; returns None when ffmpeg/video is unavailable."""
    if not video.is_file():
        return None
    poster.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, second):.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            "scale=960:-2",
            str(poster),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0 and poster.is_file() and poster.stat().st_size:
        return poster
    return None
