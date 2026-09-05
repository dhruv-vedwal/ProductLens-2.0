"""Run one durable, cloud-backed URL acceptance job outside an interactive shell timeout.

This is an operations entry point, not a second generation path: it creates a
normal repository request/run and delegates every stage to ``DemoJobService``.
Its output is intentionally limited to the non-secret run ID and terminal
status so it can be used by a supervised worker or a local operator alike.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from productlens.config.settings import Settings
from productlens.contracts.models import DiscoveryBudget
from productlens.services.runtime import build_job_service


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one ProductLens live URL acceptance job")
    parser.add_argument("--request-file", type=Path, help="JSON file containing url, objective, and optional run settings")
    parser.add_argument("--run-id", help="Resume an existing persisted run instead of creating a new one")
    parser.add_argument("--url")
    parser.add_argument("--objective")
    parser.add_argument("--audience", default="product prospect")
    parser.add_argument("--target-duration-seconds", type=int, default=180)
    parser.add_argument("--max-pages", type=int, default=6)
    args = parser.parse_args()
    if args.request_file:
        request = json.loads(args.request_file.read_text(encoding="utf-8"))
        for field in ("url", "objective", "audience", "target_duration_seconds", "max_pages"):
            if field in request:
                setattr(args, field, request[field])
    if not args.url or not args.objective:
        parser.error("--url and --objective are required unless --request-file provides them")
    return args


async def run(args: argparse.Namespace) -> None:
    settings = Settings.from_environment()
    repository, service = build_job_service(settings)
    if args.run_id:
        record = repository.get_run(args.run_id)
        created = False
        if record["status"] == "RUNNING":
            # A supervised operator may have terminated a stale worker after
            # its provider lease closed. Requeue the same run so persisted
            # discovery evidence is reused rather than creating a duplicate.
            repository.update_run(args.run_id, stage="QUEUED", status="QUEUED")
    else:
        project = repository.ensure_local_project()
        request = repository.create_request(str(uuid4()), args.url, args.objective, project["id"])
        record, created = repository.create_idempotent_run(request["id"], str(settings.artifact_root))
    print(f"RUN_ID={record['id']} CREATED={created}", flush=True)
    settings_payload = {
        "allow_external_side_effects": False,
        "render": True,
        "cloud_discovery": True,
        "cloud_production": True,
        "max_pages": args.max_pages,
        "stagehand_assist": False,
        "audience": args.audience,
        "target_duration_seconds": args.target_duration_seconds,
    }
    if args.run_id:
        # Resume from the first incomplete durable checkpoint. This prevents a
        # stale worker from paying for a second exploration when discovery and
        # planning evidence are already present.
        service.repository.ensure_stage_jobs(record["id"])
        completed = {
            row["stage"]
            for row in service.repository.stage_jobs(record["id"])
            if row["status"] in {"COMPLETE", "SKIPPED"}
        }
        for stage in ("DISCOVERY", "PLANNING", "EXECUTION", "NARRATION", "RENDER", "VIDEO_QA"):
            if stage not in completed:
                await service.run_url_stage(record["id"], stage, payload=settings_payload)
        repository.update_run(record["id"], stage="COMPLETE", status="COMPLETE")
    else:
        await service.run_url(
            record["id"],
            allow_external_side_effects=False,
            render=True,
            cloud_discovery=True,
            budget=DiscoveryBudget(max_pages=args.max_pages, max_actions=24, max_model_calls=3),
            stagehand_assist=False,
            audience=args.audience,
            target_duration_seconds=args.target_duration_seconds,
        )
    print(f"COMPLETE={record['id']}", flush=True)


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
