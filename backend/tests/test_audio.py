import json
from pathlib import Path

import pytest

from app.narration.audio import audio_duration_seconds


def test_audio_duration_uses_ffprobe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    class Result:
        stdout = json.dumps({"format": {"duration": "2.4"}})

    monkeypatch.setattr(
        "app.narration.audio.subprocess.run", lambda *args, **kwargs: Result()
    )
    assert audio_duration_seconds(tmp_path / "voice.mp3") == 2.4
