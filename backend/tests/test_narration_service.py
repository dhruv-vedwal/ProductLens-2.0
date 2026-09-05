from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from productlens.contracts.models import DemoTrace, InteractionEvent, OperationKind
from productlens.narration.service import NarrationService


class StubSpeech:
    def __init__(self):
        self.calls: list[str] = []

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        self.calls.append(text)
        return b"audio"


@pytest.mark.asyncio
async def test_narration_uses_actual_audio_duration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr("productlens.narration.service.audio_duration_seconds", lambda _: 4.0)
    trace = DemoTrace(
        run_id="run",
        objective="Demo",
        started_at=datetime.now(UTC),
        events=[
            InteractionEvent(
                operation_id="op",
                kind=OperationKind.CLICK,
                intent="Open modal",
                before={},
                after={},
                success=True,
                duration_ms=1,
            )
        ],
    )
    result = await NarrationService().create(trace, StubSpeech(), tmp_path / "voice.mp3")
    assert result["captions"][0]["end"] == 4.0
    assert (tmp_path / "voice.mp3").read_bytes() == b"audio"


@pytest.mark.asyncio
async def test_narration_synthesizes_each_approved_scene_separately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    durations = iter([1.0, 2.5, 3.5])
    monkeypatch.setattr("productlens.narration.service.audio_duration_seconds", lambda _: next(durations))
    def concatenate(command, **_kwargs):
        Path(command[-1]).write_bytes(b"combined-audio")
        return __import__("subprocess").CompletedProcess(command, 0, "", "")
    monkeypatch.setattr("productlens.narration.service.subprocess.run", concatenate)
    trace = DemoTrace(
        run_id="run", objective="Demo", started_at=datetime.now(UTC),
        events=[
            InteractionEvent(operation_id="one", kind=OperationKind.CLICK, intent="Open one", before={}, after={}, success=True, duration_ms=1),
            InteractionEvent(operation_id="two", kind=OperationKind.CLICK, intent="Open two", before={}, after={}, success=True, duration_ms=1),
        ],
    )
    speech = StubSpeech()
    result = await NarrationService().create(trace, speech, tmp_path / "voice.mp3")

    assert len(speech.calls) == 2
    assert result["segment_durations_seconds"] == [1.0, 2.5]
    assert [caption["end"] for caption in result["captions"]] == [1.0, 3.5]


@pytest.mark.asyncio
async def test_narration_aligns_measured_segments_to_trace_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr("productlens.narration.service.audio_duration_seconds", lambda _: 1.5)
    def compose(command, **_kwargs):
        Path(command[-1]).write_bytes(b"aligned-audio")
        return __import__("subprocess").CompletedProcess(command, 0, "", "")
    monkeypatch.setattr("productlens.narration.service.subprocess.run", compose)
    started = datetime.now(UTC)
    trace = DemoTrace(
        run_id="run", objective="Demo", started_at=started, recording_started_at=started,
        events=[InteractionEvent(operation_id="one", kind=OperationKind.CLICK, intent="Open one", action_at=started + timedelta(seconds=3), before={}, after={}, success=True, duration_ms=1)],
    )

    result = await NarrationService().create(trace, StubSpeech(), tmp_path / "voice.mp3")

    assert result["segment_offsets_seconds"] == [2.88]
    assert result["captions"][0]["start"] == 2.88
