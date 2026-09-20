"""Run one trace-only ProductLens interaction harness job.

This is the supervised CLI equivalent of ``POST /interaction-harness/runs``.
It executes only discovery, planning, and execution, then prints the durable
harness result. It never invokes narration, audio, Remotion, or video QA.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.config.settings import Settings
from app.contracts.harness import HarnessMode, InteractionHarnessRequest
from app.interaction import objective_fingerprint
from app.services.runtime import build_job_service


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-file", type=Path)
    parser.add_argument("--request-id")
    parser.add_argument("--url")
    parser.add_argument("--objective")
    parser.add_argument("--mode", choices=[item.value for item in HarnessMode], default="capability")
    parser.add_argument("--audience", default="product prospect")
    parser.add_argument("--auth-reference")
    parser.add_argument(
        "--cloud-browser",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Use Browserbase when true; default follows configured provider capability.",
    )
    parser.add_argument(
        "--side-effect-policy",
        choices=("read_only", "reversible", "explicitly_authorized"),
        default="read_only",
    )
    parser.add_argument("--required-outcome", action="append", default=[])
    parser.add_argument("--excluded-action", action="append", default=[])
    parser.add_argument("--max-pages", type=int, default=6)
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--max-exploration-steps", type=int, default=12)
    parser.add_argument("--max-model-calls", type=int, default=3)
    parser.add_argument("--max-duration-seconds", type=int, default=900)
    args = parser.parse_args()
    if args.request_file:
        values = json.loads(args.request_file.read_text(encoding="utf-8"))
        aliases = {
            "required_outcomes": "required_outcome",
            "excluded_actions": "excluded_action",
            "auth_reference": "auth_reference",
            "side_effect_policy": "side_effect_policy",
        }
        for key, value in values.items():
            destination = aliases.get(key, key)
            if hasattr(args, destination):
                setattr(args, destination, value)
    if not args.url or not args.objective:
        parser.error("--url and --objective are required unless --request-file provides them")
    return args


async def run(args: argparse.Namespace) -> int:
    request = InteractionHarnessRequest.model_validate(
        {
            "request_id": args.request_id,
            "url": args.url,
            "objective": args.objective,
            "mode": args.mode,
            "audience": args.audience,
            "auth_reference": args.auth_reference,
            "cloud_browser": args.cloud_browser,
            "side_effect_policy": args.side_effect_policy,
            "limits": {
                "max_pages": args.max_pages,
                "max_steps": args.max_steps,
                "max_exploration_steps": args.max_exploration_steps,
                "max_model_calls": args.max_model_calls,
                "max_duration_seconds": args.max_duration_seconds,
            },
            "required_outcomes": args.required_outcome,
            "excluded_actions": args.excluded_action,
        }
    )
    settings = Settings.from_environment()
    repository, service = build_job_service(settings)
    project = repository.ensure_local_project()
    request_id = request.request_id or f"harness:{project['id']}:{objective_fingerprint(request)[:48]}"
    stored_request = repository.create_request(
        request_id, str(request.url), request.objective, project["id"]
    )
    run_record, created = repository.create_idempotent_run(
        stored_request["id"], str(settings.artifact_root)
    )
    if created:
        payload = {
            **request.model_dump(mode="json"),
            "harness_gate": True,
            "trace_only": True,
            "harness_mode": request.mode.value,
            "cloud_discovery": bool(settings.browserbase_api_key),
            "cloud_production": False,
            "render": False,
            "credential_reference": request.auth_reference,
            "allow_external_side_effects": request.side_effect_policy
            == "explicitly_authorized",
            "allow_isolated_record_creation": request.side_effect_policy
            == "explicitly_authorized",
        }
        limits = payload.pop("limits", {})
        if isinstance(limits, dict):
            payload.update(limits)
        payload["cloud_discovery"] = (
            args.cloud_browser
            if args.cloud_browser is not None
            else bool(settings.browserbase_api_key)
        )
        repository.enqueue_job(run_record["id"], "interaction", payload)
    repository.ensure_stage_jobs(run_record["id"])
    for stage in ("DISCOVERY", "PLANNING", "EXECUTION"):
        stage_job = repository.stage_job(run_record["id"], stage)
        if stage_job["status"] in {"COMPLETE", "SKIPPED"}:
            continue
        job = repository.job_for_run(run_record["id"])
        if job is None:
            raise RuntimeError("harness run has no durable root job")
        await service.run_url_stage(run_record["id"], stage, payload=job["payload"])
        if repository.get_run(run_record["id"])["status"] in {"FAILED", "COMPLETE", "CANCELLED"}:
            break
    result_path = (
        settings.artifact_root / "runs" / run_record["id"] / "harness" / "result.json"
    )
    result = (
        json.loads(result_path.read_text(encoding="utf-8"))
        if result_path.is_file()
        else {"run_id": run_record["id"], "status": repository.get_run(run_record["id"])["status"]}
    )
    print(json.dumps({"created": created, **result}, indent=2), flush=True)
    return 0 if result.get("status") == "VERIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
