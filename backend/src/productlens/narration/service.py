from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from productlens.contracts.models import DemoTrace
from productlens.narration.audio import audio_duration_seconds
from productlens.narration.script import captions_from_measured_segments, script_from_trace


class SpeechProvider(Protocol):
    async def synthesize(self, text: str, voice: str | None = None) -> bytes: ...


class NarrationTimingError(RuntimeError):
    """Measured speech cannot be placed truthfully on the captured scene trace."""


class NarrationService:
    @staticmethod
    def _scene_offsets(
        trace: DemoTrace, script: list[dict[str, object]], durations: list[float]
    ) -> list[float]:
        events = {event.id: event for event in trace.events if event.success}
        offsets: list[float] = []
        previous_end = 0.0
        for line, duration in zip(script, durations, strict=True):
            event = events.get(str(line["event_id"]))
            if event is not None and trace.recording_started_at is not None:
                desired = max(
                    0.0,
                    (
                        (event.action_at or event.occurred_at) - trace.recording_started_at
                    ).total_seconds()
                    - 0.12,
                )
            else:
                desired = previous_end
            # Speech may not begin before the visual state it describes, nor
            # overlap a previous scene's voice. Such a conflict must repair
            # narration/capture timing rather than silently compressing audio.
            if desired + 0.02 < previous_end:
                raise NarrationTimingError(
                    "measured narration scenes overlap their verified browser events"
                )
            offsets.append(desired)
            previous_end = desired + duration
        return offsets

    async def create(
        self,
        trace: DemoTrace,
        provider: SpeechProvider,
        output: Path,
        voice: str | None = None,
        script: list[dict[str, object]] | None = None,
    ) -> dict:
        script = script or script_from_trace(trace)
        if not script:
            raise ValueError("narration requires at least one approved script line")
        output.parent.mkdir(parents=True, exist_ok=True)
        measured_segments: list[float] = []
        # Synthesize each stable scene separately. This makes the returned
        # durations factual timing evidence instead of a proportional guess.
        with TemporaryDirectory(prefix="productlens-narration-") as temporary:
            temporary_root = Path(temporary)
            segment_paths: list[Path] = []
            for index, line in enumerate(script):
                segment = temporary_root / f"{index:04d}.mp3"
                segment.write_bytes(await provider.synthesize(str(line["text"]), voice))
                duration = audio_duration_seconds(segment)
                if duration <= 0:
                    raise RuntimeError("speech provider returned an unreadable narration segment")
                measured_segments.append(duration)
                segment_paths.append(segment)
            offsets = self._scene_offsets(trace, script, measured_segments)
            if len(segment_paths) == 1 and offsets[0] <= 0.02:
                output.write_bytes(segment_paths[0].read_bytes())
            else:
                command = ["ffmpeg", "-y", "-loglevel", "error"]
                for segment in segment_paths:
                    command.extend(("-i", str(segment)))
                filters = [
                    f"[{index}:a]adelay={round(offset * 1000)}:all=1[scene{index}]"
                    for index, offset in enumerate(offsets)
                ]
                filters.append(
                    "".join(f"[scene{index}]" for index in range(len(segment_paths)))
                    + f"amix=inputs={len(segment_paths)}:duration=longest:dropout_transition=0[mix]"
                )
                command.extend(
                    (
                        "-filter_complex",
                        ";".join(filters),
                        "-map",
                        "[mix]",
                        "-c:a",
                        "libmp3lame",
                        str(output),
                    )
                )
                completed = await asyncio.to_thread(
                    subprocess.run, command, capture_output=True, text=True, check=False
                )
                if completed.returncode != 0 or not output.exists() or output.stat().st_size == 0:
                    raise RuntimeError("unable to compose event-aligned narration segments")
        duration = audio_duration_seconds(output)
        return {
            "script": script,
            "duration_seconds": duration,
            "segment_durations_seconds": measured_segments,
            "segment_offsets_seconds": offsets,
            "captions": captions_from_measured_segments(
                script, measured_segments, starts_seconds=offsets
            ),
            "audio_path": str(output),
        }
