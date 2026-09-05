import argparse
from uuid import uuid4

from productlens.config.settings import Settings
from productlens.services.runtime import build_job_service
from productlens.workers.tasks import process_generation_job


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("objective")
    parser.add_argument(
        "--trace-only",
        action="store_true",
        help="Stop after discovery, production capture, and narration validation; do not render an MP4.",
    )
    args = parser.parse_args()
    settings = Settings.from_environment()
    repository, _ = build_job_service(settings)
    project = repository.ensure_local_project()
    request = repository.create_request(str(uuid4()), args.url, args.objective, project["id"])
    run = repository.create_run(request["id"], str(settings.artifact_root))
    job = repository.enqueue_job(
        run["id"],
        "url",
        {
            "allow_external_side_effects": False,
            "cloud_discovery": True,
            "stagehand_assist": False,
            "max_pages": 6,
            "render": not args.trace_only,
            "credential_reference": None,
            "audience": "product prospect",
            "target_duration_seconds": 180,
        },
    )
    if settings.worker_mode == "dramatiq" and settings.broker_url:
        process_generation_job.send(job["id"])
    print(f"run_id={run['id']} job_id={job['id']} status=QUEUED", flush=True)


if __name__ == "__main__":
    main()
