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

from app.config.settings import Settings
from app.contracts.models import DiscoveryBudget
from app.orchestration.lifecycle import RunStage
from app.services.runtime import build_job_service


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one ProductLens live URL acceptance job")
    parser.add_argument(
        "--request-file",
        type=Path,
        help="JSON file containing url, objective, and optional run settings",
    )
    parser.add_argument(
        "--run-id", help="Resume an existing persisted run instead of creating a new one"
    )
    parser.add_argument("--url")
    parser.add_argument("--objective")
    parser.add_argument("--audience", default="product prospect")
    parser.add_argument("--target-duration-seconds", type=int, default=180)
    parser.add_argument("--max-pages", type=int, default=6)
    parser.add_argument(
        "--allow-isolated-record-creation",
        action="store_true",
        help="Authorise one verified isolated demo record rehearsal; external actions remain blocked.",
    )
    parser.add_argument(
        "--credential-reference",
        default=None,
        help="Opaque environment-backed credential reference for an authenticated target.",
    )
    parser.add_argument(
        "--stop-after",
        choices=("DISCOVERY", "PLANNING", "EXECUTION", "NARRATION", "RENDER", "VIDEO_QA"),
        help=(
            "Run through one durable stage then requeue the run. Useful for "
            "reviewing discovery or rehearsal evidence before a production capture."
        ),
    )
    args = parser.parse_args()
    if args.request_file:
        request = json.loads(args.request_file.read_text(encoding="utf-8"))
        for field in (
            "url",
            "objective",
            "audience",
            "target_duration_seconds",
            "max_pages",
            "credential_reference",
            "allow_isolated_record_creation",
        ):
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
            trace_path = (
                Path(record["artifact_root"]) / "runs" / record["id"] / "execution" / "trace.json"
            )
            dispatched_mutation = False
            if trace_path.exists():
                try:
                    prior_trace = json.loads(trace_path.read_text(encoding="utf-8"))
                    dispatched_mutation = any(
                        event.get("kind") == "Submit"
                        and event.get("action_at")
                        and not event.get("success", False)
                        for event in prior_trace.get("events", [])
                    )
                except (OSError, json.JSONDecodeError):
                    # A corrupt/incomplete trace is not safe evidence for an
                    # automatic mutation replay either.
                    dispatched_mutation = True
            if dispatched_mutation:
                raise RuntimeError(
                    "REPAIR_REQUIRED: an unverified submit was already dispatched; "
                    "perform read-only outcome verification before resuming"
                )
            # A supervised operator may have terminated a stale worker after
            # its provider lease closed. Requeue the same run so persisted
            # discovery evidence is reused rather than creating a duplicate.
            repository.update_run(args.run_id, stage="QUEUED", status="QUEUED")
    else:
        project = repository.ensure_local_project()
        request = repository.create_request(str(uuid4()), args.url, args.objective, project["id"])
        record, created = repository.create_idempotent_run(
            request["id"], str(settings.artifact_root)
        )
    print(f"RUN_ID={record['id']} CREATED={created}", flush=True)
    settings_payload = {
        "allow_external_side_effects": False,
        "allow_isolated_record_creation": bool(args.allow_isolated_record_creation),
        "render": True,
        "cloud_discovery": True,
        "cloud_production": True,
        "max_pages": args.max_pages,
        "credential_reference": args.credential_reference,
        "audience": args.audience,
        "target_duration_seconds": args.target_duration_seconds,
    }
    stages = ("DISCOVERY", "PLANNING", "EXECUTION", "NARRATION", "RENDER", "VIDEO_QA")
    if args.stop_after:
        # A bounded pre-production check is deliberately the same durable job
        # path as a full run. It does not create a second exploratory flow or
        # leave an apparently active worker behind; the unfinished stages can
        # later resume from their persisted checkpoints.
        completed = (
            {
                row["stage"]
                for row in repository.stage_jobs(record["id"])
                if row["status"] in {"COMPLETE", "SKIPPED"}
            }
            if args.run_id
            else set()
        )
        for stage in stages:
            if stage in completed:
                if stage == args.stop_after:
                    print(f"PAUSED_AFTER={stage} RUN_ID={record['id']}", flush=True)
                    return
                continue
            await service.run_url_stage(record["id"], stage, payload=settings_payload)
            if stage == args.stop_after:
                paused_stage = {
                    "DISCOVERY": RunStage.PLAN_READY,
                    "PLANNING": RunStage.PLAN_VALIDATED,
                    "EXECUTION": RunStage.TRACE_READY,
                    "NARRATION": RunStage.NARRATION_READY,
                    "RENDER": RunStage.RENDERED,
                    "VIDEO_QA": RunStage.QA_PASSED,
                }[stage]
                # A paused run is resumable from the next incomplete durable
                # checkpoint. Video QA is terminal and has already set its
                # own accepted completion state.
                if stage != "VIDEO_QA":
                    repository.update_run(record["id"], stage=paused_stage, status="QUEUED")
                print(f"PAUSED_AFTER={stage} RUN_ID={record['id']}", flush=True)
                return
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
        for stage in stages:
            if stage not in completed:
                await service.run_url_stage(record["id"], stage, payload=settings_payload)
        # Never infer completion from the supervisor loop itself.  A failed
        # checkpoint must remain visible and repairable; only the durable
        # stage ledger can authorize terminal completion.
        final_stages = repository.stage_jobs(record["id"])
        if all(item["status"] in {"COMPLETE", "SKIPPED"} for item in final_stages):
            repository.update_run(record["id"], stage="COMPLETE", status="COMPLETE")
        else:
            failed = [item for item in final_stages if item["status"] == "FAILED"]
            if failed:
                repository.update_run(
                    record["id"],
                    stage=failed[0]["stage"],
                    status="FAILED",
                    error_code=failed[0].get("error_code"),
                )
            raise RuntimeError("run did not reach terminal completion; inspect stage ledger")
    else:
        await service.run_url(
            record["id"],
            allow_external_side_effects=False,
            render=True,
            cloud_discovery=True,
            budget=DiscoveryBudget(max_pages=args.max_pages, max_actions=24, max_model_calls=3),
            credential_reference=args.credential_reference,
            audience=args.audience,
            target_duration_seconds=args.target_duration_seconds,
            allow_isolated_record_creation=bool(args.allow_isolated_record_creation),
        )
    print(f"COMPLETE={record['id']}", flush=True)


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
