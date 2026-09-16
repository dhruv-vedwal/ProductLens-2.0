"""Resume a durable URL run without an interactive polling loop.

This is an operator/supervisor entry point over the same staged service used by
the API and Dramatiq workers. It reconstructs its payload from the persisted
request and run settings, executes only incomplete stages in order, and emits a
single terminal summary. It is intentionally generic and never infers or
prints credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

from app.config.settings import Settings
from app.services.runtime import build_job_service

STAGES = ("DISCOVERY", "PLANNING", "EXECUTION", "NARRATION", "RENDER", "VIDEO_QA")


def write_status_snapshot(settings: Settings, repository: Any, run_id: str) -> None:
    """Write a one-shot operator snapshot without requiring a polling loop."""
    root = settings.artifact_root / "runs" / run_id
    root.mkdir(parents=True, exist_ok=True)
    payload = {"run": repository.get_run(run_id), "jobs": repository.stage_jobs(run_id)}
    destination = root / "run-status.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(destination)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resume a ProductLens URL run")
    parser.add_argument("run_id")
    parser.add_argument(
        "--from-stage",
        choices=STAGES,
        help="Queue this stage and all later stages for a targeted retry.",
    )
    parser.add_argument("--credential-reference", default=None)
    parser.add_argument("--audience", default="product prospect")
    parser.add_argument("--target-duration-seconds", type=int, default=120)
    parser.add_argument("--max-pages", type=int, default=6)
    parser.add_argument("--allow-isolated-record-creation", action="store_true")
    parser.add_argument("--no-render", action="store_true")
    return parser.parse_args()


async def resume(args: argparse.Namespace) -> None:
    settings = Settings.from_environment()
    repository, service = build_job_service(settings)
    # A supervisor/worker can disappear while Remotion or Browserbase is
    # outside Python's event loop. Reconcile expired leases before deciding
    # which checkpoint is resumable; otherwise a stale RENDER row looks live
    # forever and a subsequent operator has no safe recovery boundary.
    repository.fail_orphaned_jobs()
    repository.recover_stale_jobs(
        max_running_seconds=int(os.getenv("PRODUCTLENS_WORKER_LEASE_SECONDS", "1800"))
    )
    run = repository.get_run(args.run_id)
    # Resolve the run first so older databases fail with a useful lookup error;
    # the stage payload is reconstructed from the persisted job below.
    repository.get_request(run["request_id"])
    repository.ensure_stage_jobs(args.run_id)
    write_status_snapshot(settings, repository, args.run_id)
    if args.from_stage:
        repository.prepare_targeted_retry(args.run_id, args.from_stage)

    # A root generation job is normally persisted by the API. Direct
    # supervisors may operate on an older run without one, so use only the
    # non-secret request/settings defaults needed by the stage service.
    payload: dict[str, Any] = {
        "allow_external_side_effects": False,
        "allow_isolated_record_creation": bool(args.allow_isolated_record_creation),
        "cloud_discovery": bool(settings.browserbase_api_key),
        "cloud_production": bool(settings.browserbase_api_key),
        "max_pages": max(1, args.max_pages),
        "credential_reference": args.credential_reference,
        "audience": args.audience,
        "target_duration_seconds": max(30, args.target_duration_seconds),
        "render": not args.no_render,
        "refresh_editorial": args.from_stage == "NARRATION",
    }
    existing = repository.job_for_run(args.run_id)
    if existing and isinstance(existing.get("payload"), dict):
        # Preserve prior non-secret intent for a like-for-like resume, while
        # explicitly overriding safety-sensitive mutation permission.
        payload = {**existing["payload"], **payload}

    completed = {
        item["stage"]
        for item in repository.stage_jobs(args.run_id)
        if item["status"] in {"COMPLETE", "SKIPPED"}
    }
    try:
        for stage in STAGES:
            if stage in completed:
                continue
            await service.run_url_stage(args.run_id, stage, payload=payload)
        repository.update_run(args.run_id, stage="COMPLETE", status="COMPLETE")
        print(f"COMPLETE={args.run_id}", flush=True)
    except Exception:
        # The durable stage service records the owning failure. Refresh the
        # snapshot once so detached operators see the terminal state without
        # having to spend a client polling the database.
        write_status_snapshot(settings, repository, args.run_id)
        raise
    finally:
        write_status_snapshot(settings, repository, args.run_id)


if __name__ == "__main__":
    asyncio.run(resume(parse_args()))
