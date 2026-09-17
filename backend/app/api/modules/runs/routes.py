from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.api import deps_state as api
from app.api.modules.runs.schemas import (
    FixtureRequest,
    GenerationRequest,
    RetentionRequest,
    RetryRequest,
)
from app.artifacts.store import RunArtifacts
from app.video.poster import write_video_poster

router = APIRouter(tags=["runs"])


@router.post("/fixture-runs")
async def create_fixture_run(
    payload: FixtureRequest,
    user: dict = Depends(api.current_user),
) -> dict[str, str]:
    try:
        project_id = payload.project_id or (
            api.repository.ensure_user_project(user["id"])["id"]
            if api.settings.auth_required
            else api.repository.ensure_local_project()["id"]
        )
        if api.settings.auth_required:
            api.repository.get_project_for_user(project_id, user["id"])
        request = api.repository.create_request(
            str(uuid4()),
            f"fixture://gate-{payload.gate}",
            payload.objective,
            project_id,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="project not found") from error
    run, created = api.repository.create_idempotent_run(
        request["id"], str(api.settings.artifact_root)
    )
    if created:
        job = api.repository.enqueue_job(run["id"], "fixture", payload.model_dump())
        api.dispatch_generation_job(job["id"])
    return {"run_id": run["id"], "status": run["status"]}


@router.post("/runs")
async def create_generation_run(
    payload: GenerationRequest,
    user: dict = Depends(api.current_user),
) -> dict[str, str]:
    if api.url_generator is None:
        raise HTTPException(
            status_code=503,
            detail="OpenRouter structured planning is not configured; configure an active LLM provider first",
        )
    try:
        project_id = payload.project_id or (
            api.repository.ensure_user_project(user["id"])["id"]
            if api.settings.auth_required
            else api.repository.ensure_local_project()["id"]
        )
        if api.settings.auth_required:
            api.repository.get_project_for_user(project_id, user["id"])
        request = api.repository.create_request(
            payload.request_id or str(uuid4()),
            str(payload.url),
            payload.objective,
            project_id,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="project not found") from error
    run, created = api.repository.create_idempotent_run(
        request["id"], str(api.settings.artifact_root)
    )
    if created:
        job_payload = payload.model_dump(mode="json")
        job_payload["cloud_discovery"] = api.resolve_cloud_discovery(
            job_payload["cloud_discovery"],
            browserbase_configured=bool(api.settings.browserbase_api_key),
        )
        job = api.repository.enqueue_job(run["id"], "url", job_payload)
        api.dispatch_generation_job(job["id"])
    return {"run_id": run["id"], "status": run["status"]}


@router.post("/runs/{run_id}/retry")
async def retry_generation_run(
    run_id: str,
    payload: RetryRequest,
    user: dict = Depends(api.current_user),
) -> dict[str, str]:
    """Retry only a failed run, retaining its original evidence and lineage."""
    try:
        previous = (
            api.repository.get_run_for_user(run_id, user["id"])
            if api.settings.auth_required
            else api.repository.get_run(run_id)
        )
        request = api.repository.get_request(previous["request_id"])
        previous_job = api.repository.job_for_run(run_id)
        retry = api.repository.create_retry_run(run_id, str(api.settings.artifact_root))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="run not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    retry_from_stage = payload.retry_from_stage
    if retry_from_stage is None:
        decision_path = (
            api.settings.artifact_root / "runs" / run_id / "qa" / "repair-decision.json"
        )
        if decision_path.exists():
            try:
                decision = json.loads(decision_path.read_text(encoding="utf-8"))
                retry_from_stage = {
                    "PLANNING": "PLANNING",
                    "PRODUCTION_EXECUTION": "EXECUTION",
                    "NARRATION": "NARRATION",
                    "PRESENTATION_PLANNED": "NARRATION",
                    "RENDERING": "RENDER",
                    "VIDEO_QA": "VIDEO_QA",
                }.get(decision.get("retry_from_stage"))
            except (OSError, ValueError):
                # A malformed diagnostic must never silently select a stage;
                # fall back to the explicit full retry path instead.
                retry_from_stage = None
    if retry_from_stage and request["url"].startswith("fixture://gate-"):
        raise HTTPException(
            status_code=422, detail="targeted retry is currently available for URL runs only"
        )
    if request["url"].startswith("fixture://gate-"):
        gate = int(request["url"].rsplit("-", maxsplit=1)[1])
        job = api.repository.enqueue_job(
            retry["id"],
            "fixture",
            {
                "gate": gate,
                "objective": request["objective"],
                "render": payload.render
                if payload.render is not None
                else bool(((previous_job or {}).get("payload", {})).get("render", True)),
            },
        )
    else:
        if api.url_generator is None:
            raise HTTPException(
                status_code=503, detail="OpenRouter structured planning is not configured"
            )
        original = (previous_job or {}).get("payload", {})
        # Retain the prior run's non-secret intent/settings so the retry remains
        # comparable. Side effects are intentionally *not* inherited: a user
        # must opt in again for every replayable external mutation.
        job_payload = {
            "allow_external_side_effects": payload.allow_external_side_effects,
            "allow_isolated_record_creation": payload.allow_isolated_record_creation,
            "cloud_discovery": api.resolve_cloud_discovery(
                payload.cloud_discovery
                if payload.cloud_discovery is not None
                else original.get("cloud_discovery"),
                browserbase_configured=bool(api.settings.browserbase_api_key),
            ),
            "max_pages": payload.max_pages
            if payload.max_pages is not None
            else int(original.get("max_pages", 6)),
            "render": payload.render
            if payload.render is not None
            else bool(original.get("render", True)),
            "credential_reference": payload.credential_reference
            if payload.credential_reference is not None
            else original.get("credential_reference"),
            "audience": payload.audience or original.get("audience", "product prospect"),
            "target_duration_seconds": payload.target_duration_seconds
            or int(original.get("target_duration_seconds", 120)),
            "presentation": (
                payload.presentation.model_dump(mode="json")
                if payload.presentation is not None
                else original.get("presentation", {})
            ),
            # Editorial/narration repairs regenerate only the script and its
            # evidence-bound storyboard from the existing trace. Other retry
            # boundaries retain their approved presentation artifact.
            "refresh_editorial": retry_from_stage == "NARRATION",
        }
        job = api.repository.enqueue_job(retry["id"], "url", job_payload)
    if retry_from_stage:
        try:
            RunArtifacts.clone_for_targeted_retry(
                api.settings.artifact_root, run_id, retry["id"], start_stage=retry_from_stage
            )
            api.repository.prepare_targeted_retry(retry["id"], retry_from_stage)
            api.repository.copy_run_evidence(run_id, retry["id"], through_stage=retry_from_stage)
        except (FileNotFoundError, KeyError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
    api.dispatch_generation_job(job["id"])
    return {"run_id": retry["id"], "status": retry["status"]}


@router.get("/runs/{run_id}/stages")
def get_run_stages(run_id: str, user: dict = Depends(api.current_user)) -> dict:
    """Inspect durable checkpoints before deciding whether retry or resume is safe."""
    run = api.owned_run_or_404(run_id, user)
    return {
        "run": run,
        "job": api.repository.job_for_run(run_id),
        "stages": api.repository.stage_jobs(run_id),
    }


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str, request: Request, user: dict = Depends(api.current_user_bearer_or_query)
) -> StreamingResponse:
    """Stream durable run/stage status without client polling.

    The stream is a read-only projection of the database ledger. It contains
    no provider payloads, credentials, or artifact contents, and terminates as
    soon as the run reaches a terminal state or the client disconnects.
    """
    api.owned_run_or_404(run_id, user)

    async def events():
        terminal = {"COMPLETE", "FAILED", "CANCELLED"}
        last_payload: str | None = None
        while True:
            if await request.is_disconnected():
                return
            try:
                run = api.owned_run_or_404(run_id, user)
            except HTTPException:
                return
            payload = {
                "run_id": run_id,
                "status": run.get("status"),
                "stage": run.get("stage"),
                "error_code": run.get("error_code"),
                "updated_at": run.get("updated_at"),
                "stages": api.repository.stage_jobs(run_id),
            }
            encoded = json.dumps(payload, separators=(",", ":"), default=str)
            if encoded != last_payload:
                yield f"event: status\ndata: {encoded}\n\n"
                last_payload = encoded
            if str(run.get("status")) in terminal:
                return
            await asyncio.sleep(1.0)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/resume")
def resume_generation_run(run_id: str, user: dict = Depends(api.current_user)) -> dict:
    api.owned_run_or_404(run_id, user)
    try:
        job = api.repository.resume_run(run_id)
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    api.dispatch_generation_job(job["id"])
    return {"run_id": run_id, "job_id": job["id"], "status": "QUEUED"}


@router.post("/runs/{run_id}/cancel")
def cancel_generation_run(run_id: str, user: dict = Depends(api.current_user)) -> dict:
    api.owned_run_or_404(run_id, user)
    try:
        run = api.repository.cancel_run(run_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"run_id": run_id, "status": run["status"]}


@router.delete("/runs/{run_id}", status_code=204)
def delete_empty_run(run_id: str, user: dict = Depends(api.current_user)) -> None:
    api.owned_run_or_404(run_id, user)
    try:
        # Refuse to remove a database row if an unregistered filesystem
        # artifact remains. That file may be the only crash evidence left by a
        # terminated browser/render process.
        RunArtifacts.remove_empty_run_directory(api.settings.artifact_root, run_id)
        api.repository.delete_empty_terminal_run(run_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/runs/retention/purge-empty")
def purge_empty_runs(payload: RetentionRequest, user: dict = Depends(api.current_user)) -> dict:
    # User-specific retention avoids deleting another workspace's diagnostics.
    candidates = (
        api.repository.list_runs_for_user(user["id"], limit=100, offset=0)
        if api.settings.auth_required
        else api.repository.list_runs(limit=100, offset=0)
    )
    cutoff = datetime.now(UTC) - timedelta(seconds=payload.older_than_seconds)
    deleted: list[str] = []
    for candidate in candidates:
        try:
            if datetime.fromisoformat(candidate["updated_at"]) <= cutoff:
                RunArtifacts.remove_empty_run_directory(
                    api.settings.artifact_root, candidate["id"]
                )
                api.repository.delete_empty_terminal_run(candidate["id"])
                deleted.append(candidate["id"])
        except ValueError:
            continue
    return {"deleted_run_ids": deleted}


@router.get("/runs")
def list_runs(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: dict = Depends(api.current_user),
) -> dict:
    return {
        "items": api.repository.list_runs_for_user(user["id"], limit=limit, offset=offset)
        if api.settings.auth_required
        else api.repository.list_runs(limit=limit, offset=offset),
        "limit": limit,
        "offset": offset,
    }


@router.get("/runs/{run_id}")
def get_run(run_id: str, user: dict = Depends(api.current_user)) -> dict:
    try:
        return (
            api.repository.get_run_for_user(run_id, user["id"])
            if api.settings.auth_required
            else api.repository.get_run(run_id)
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="run not found") from error


@router.get("/runs/{run_id}/details")
def get_run_details(run_id: str, user: dict = Depends(api.current_user)) -> dict:
    try:
        if api.settings.auth_required:
            api.repository.get_run_for_user(run_id, user["id"])
        return api.repository.run_details(run_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="run not found") from error


@router.get("/runs/{run_id}/artifacts")
def get_artifacts(run_id: str, user: dict = Depends(api.current_user)) -> list[dict[str, str]]:
    try:
        (
            api.repository.get_run_for_user(run_id, user["id"])
            if api.settings.auth_required
            else api.repository.get_run(run_id)
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="run not found") from error
    return api.repository.locations(run_id)


@router.get("/runs/{run_id}/video")
def get_final_video(
    run_id: str, user: dict = Depends(api.current_user_bearer_or_query)
) -> FileResponse:
    path = api.owned_artifact_path(run_id, user, kind="final_video")
    return FileResponse(path, media_type="video/mp4", filename=f"productlens-{run_id}.mp4")


@router.get("/runs/{run_id}/stream")
def stream_final_video(
    run_id: str, user: dict = Depends(api.current_user_bearer_or_query)
) -> FileResponse:
    path = api.owned_artifact_path(run_id, user, kind="final_video")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"productlens-{run_id}.mp4",
        content_disposition_type="inline",
    )


@router.get("/runs/{run_id}/poster")
def get_run_poster(
    run_id: str, user: dict = Depends(api.current_user_bearer_or_query)
) -> FileResponse:
    try:
        path = api.owned_artifact_path(run_id, user, kind="poster")
    except HTTPException:
        video = api.owned_artifact_path(run_id, user, kind="final_video")
        poster = write_video_poster(video, video.parent / "poster.jpg")
        if poster is None:
            raise HTTPException(status_code=404, detail="poster is not available") from None
        api.repository.save_location(run_id, "poster", str(poster))
        path = poster
    return FileResponse(path, media_type="image/jpeg", filename=f"productlens-{run_id}.jpg")
