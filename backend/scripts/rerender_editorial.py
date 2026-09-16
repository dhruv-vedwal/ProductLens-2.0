"""Refresh evidence-grounded narration and render an existing captured run."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from app.artifacts.store import RunArtifacts
from app.config.settings import Settings
from app.persistence.repository import RunRepository
from app.services.generation import UrlGenerationService

logger = logging.getLogger(__name__)


async def _run(run_id: str, artifacts_root: Path) -> None:
    artifacts = RunArtifacts(artifacts_root, run_id)
    service = UrlGenerationService.__new__(UrlGenerationService)
    service.speech_provider = None
    service.planner = None
    service.visual_reviewer = None
    # A provider-free rerender is still a durable stage transition.  Keep the
    # database truthful while a presentation repair is running, but tolerate
    # artifact-only fixtures and older local databases that have no run row.
    repository = None
    try:
        repository = RunRepository(Settings.from_environment().database_url)
        repository.ensure_stage_jobs(run_id)
        repository.update_run(run_id, stage="NARRATION", status="RUNNING")
    except Exception:  # noqa: BLE001 - artifact-only rerenders have no DB row
        repository = None

    def mark(stage: str, status: str, error_code: str | None = None) -> None:
        if repository is None:
            return
        repository.update_stage_job(run_id, stage, status=status, error_code=error_code)
        repository.update_run(run_id, stage=stage, status=status, error_code=error_code)

    try:
        mark("NARRATION", "RUNNING")
        await service.narration_stage(
            run_id=run_id, artifact_root=artifacts_root, refresh_editorial=True
        )
        mark("NARRATION", "COMPLETE")
        mark("RENDER", "RUNNING")
        output = service.render_stage(run_id=run_id, artifact_root=artifacts_root)
        mark("RENDER", "COMPLETE")
        mark("VIDEO_QA", "RUNNING")
        # Revalidate the repaired render from the retained trace.  This is
        # provider-free and ensures a standalone presentation repair cannot
        # claim success without the same delivery/editorial/visual checks as a
        # worker.
        await asyncio.to_thread(service.qa_stage, run_id=run_id, artifact_root=artifacts_root)
        # Presentation-only repairs mutate storyboard, captions, scene policy,
        # and the final MP4. Re-publish the checksum manifest after QA so a
        # rerender cannot leave a valid-looking delivery with stale hashes.
        artifacts.write_manifest()
        mark("VIDEO_QA", "COMPLETE")
        if repository is not None:
            repository.update_run(run_id, stage="COMPLETE", status="COMPLETE")
    except Exception as error:
        if repository is not None:
            current = repository.get_run(run_id)
            failed_stage = str(current.get("stage") or "RENDER")
            if failed_stage in {"COMPLETE", "QUEUED"}:
                failed_stage = "RENDER"
            try:
                mark(failed_stage, "FAILED", type(error).__name__)
            except Exception as mark_error:  # noqa: BLE001 - preserve original failure
                logger.debug("unable to persist rerender failure: %s", mark_error)
        raise
    print(output, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--artifacts-root", default="artifacts")
    args = parser.parse_args()
    asyncio.run(_run(args.run_id, Path(args.artifacts_root)))


if __name__ == "__main__":
    main()
