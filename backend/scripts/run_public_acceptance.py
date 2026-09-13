"""Run the configured public-target acceptance set without API polling.

The worker owns stage execution; this command only creates durable runs and
awaits each run in-process.  It is intentionally generic and reads targets
from ``validation/public-targets.json`` rather than embedding site behavior.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from productlens.config.settings import Settings
from productlens.services.runtime import build_job_service


def _target_sets(path: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    primary = [*payload.get("supplied_targets", []), *payload.get("targets", [])]
    fallbacks = list(payload.get("fallback_targets", []))
    return primary, fallbacks


def _accepted_record(record: dict[str, object]) -> bool:
    """Return whether an attempt can enter the final acceptance set."""
    return (
        record.get("status") == "COMPLETE"
        and record.get("deliverable") is True
        and bool(record.get("final_video"))
        and not record.get("hard_failures")
    )


def _refresh_record_from_artifacts(record: dict[str, object]) -> dict[str, object]:
    """Re-evaluate a retained attempt after an out-of-band repair.

    A rerender or targeted QA repair can finish after the acceptance supervisor
    has written a failed attempt to the manifest. On the next invocation the
    durable delivery report is authoritative; refresh that attempt before
    deciding whether to rerun the browser workflow. This keeps repair
    resumability generic and avoids spending another cloud session.
    """
    refreshed = dict(record)
    root_value = refreshed.get("artifact_root")
    if not root_value:
        return refreshed
    root = Path(str(root_value))
    final_video = root / "final" / "demo.mp4"
    delivery_path = root / "qa" / "delivery-report.json"
    if final_video.is_file():
        refreshed["final_video"] = str(final_video)
    if delivery_path.is_file():
        try:
            delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return refreshed
        refreshed["status"] = "COMPLETE" if bool(delivery.get("deliverable")) else refreshed.get("status", "FAILED")
        refreshed["deliverable"] = bool(delivery.get("deliverable", False))
        refreshed["hard_failures"] = list(delivery.get("hard_failures", []))
    return refreshed


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", type=Path, default=Path("validation/public-targets.json"))
    parser.add_argument("--artifact-root", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--start-at", type=int, default=0)
    parser.add_argument("--cloud", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--render", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--retry-url", action="append", default=[],
        help="Target a previously rejected URL after a generic code/config fix (repeatable).",
    )
    parser.add_argument(
        "--retry-rejected", action="store_true",
        help="Allow the configured target slice to be retried after a generic fix.",
    )
    args = parser.parse_args()
    settings = Settings.from_environment()
    repository, jobs = build_job_service(settings)
    primary, fallbacks = _target_sets(args.targets.resolve())
    retry_urls = {str(url).rstrip("/") for url in args.retry_url if str(url).strip()}
    target_limit = max(1, args.limit)
    targets = primary[args.start_at : args.start_at + target_limit]
    if retry_urls:
        configured = {str(item.get("url")).rstrip("/"): item for item in [*primary, *fallbacks]}
        for url in retry_urls:
            if url in configured and all(str(item.get("url")).rstrip("/") != url for item in targets):
                targets.append(configured[url])
    output = (args.artifact_root or settings.artifact_root) / "acceptance" / "public-runs.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    # ``runs`` is the final acceptance set, while ``attempts``/``rejected``
    # retain operational evidence for targets that were replaced.  Counting a
    # blocked URL as one of the eight would make a fallback silently reduce
    # coverage and would make the audit report ambiguous.
    records: list[dict[str, object]] = []
    attempts: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    # Resume an interrupted acceptance batch without rerunning accepted
    # products or discarding their durable evidence. Older manifests stored
    # failed attempts directly in ``runs``; migrate those entries into the
    # operational attempt/rejected ledgers while retaining only verified
    # deliverables in the final acceptance set.
    if output.is_file():
        try:
            previous = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            previous = {}
        previous_runs = previous.get("runs", []) if isinstance(previous, dict) else []
        previous_attempts = previous.get("attempts", []) if isinstance(previous, dict) else []
        previous_rejected = previous.get("rejected", []) if isinstance(previous, dict) else []
        if isinstance(previous_runs, list):
            for item in previous_runs:
                if not isinstance(item, dict):
                    continue
                item = _refresh_record_from_artifacts(item)
                if _accepted_record(item):
                    final_video = Path(str(item.get("final_video", "")))
                    if final_video.is_file():
                        records.append(item)
                elif item not in previous_attempts:
                    attempts.append(item)
                    if item not in previous_rejected:
                        rejected.append(item)
        if isinstance(previous_attempts, list):
            for raw_item in previous_attempts:
                if not isinstance(raw_item, dict):
                    continue
                item = _refresh_record_from_artifacts(raw_item)
                final_video = Path(str(item.get("final_video", "")))
                if _accepted_record(item) and final_video.is_file():
                    if item not in records:
                        records.append(item)
                elif item not in attempts:
                    attempts.append(item)
        if isinstance(previous_rejected, list):
            for raw_item in previous_rejected:
                if not isinstance(raw_item, dict):
                    continue
                item = _refresh_record_from_artifacts(raw_item)
                final_video = Path(str(item.get("final_video", "")))
                if _accepted_record(item) and final_video.is_file():
                    if item not in records:
                        records.append(item)
                elif item not in rejected:
                    rejected.append(item)
    accepted_urls = {str(item.get("url")) for item in records}
    rejected_urls = {
        str(item.get("url")).rstrip("/")
        for item in [*attempts, *rejected]
        if isinstance(item, dict) and item.get("status") == "FAILED" and item.get("url")
    }
    # ``--start-at`` is a targeted retry/resume selector.  Its limit denotes
    # how many additional primary targets to attempt, not an absolute count
    # that would be satisfied immediately by previously accepted runs.
    if args.start_at:
        target_limit = len(records) + target_limit
    # Persist out-of-band repairs even when the requested slice is already
    # satisfied and no new browser work is needed in this invocation.
    output.write_text(
        json.dumps(
            {"schema_version": 1, "runs": records, "attempts": attempts, "rejected": rejected},
            indent=2,
        ),
        encoding="utf-8",
    )
    # Cloud capture has its own bounded deadline, but a complete delivery also
    # includes local Remotion rendering and layered QA.  Do not reuse the
    # provider lease timeout as the end-to-end job timeout: a healthy 2–3
    # minute 1080p render can legitimately outlive the Browserbase session.
    # The worker remains bounded, while this supervisor allows enough time for
    # two resumable render chunks plus QA before classifying a target as hung.
    overall_timeout = max(3_600, settings.cloud_capture_timeout_seconds + 7_200)
    fallback_index = 0
    target_index = 0
    while target_index < len(targets) and len(records) < target_limit:
        target = targets[target_index]
        if str(target.get("url")) in accepted_urls:
            target_index += 1
            continue
        # A prior bounded timeout or planning rejection is already durable
        # evidence that this URL should be replaced for the acceptance set.
        # Do not spend another Browserbase/OpenRouter run retrying it unless an
        # operator explicitly removes the attempt from the manifest.
        target_url = str(target.get("url"))
        if (
            target_url.rstrip("/") in rejected_urls
            and target_url.rstrip("/") not in retry_urls
            and not args.retry_rejected
        ):
            target_index += 1
            existing_urls = {str(item.get("url")) for item in [*targets, *attempts]}
            while fallback_index < len(fallbacks):
                candidate = fallbacks[fallback_index]
                fallback_index += 1
                candidate_url = str(candidate.get("url"))
                if candidate_url not in existing_urls and candidate_url not in rejected_urls:
                    targets.append(candidate)
                    break
            continue
        request_id = str(uuid4())
        project = repository.ensure_local_project()
        # Targets may opt into a narrower, still evidence-grounded editorial
        # objective when a public product is intentionally small.  The
        # objective is data/configuration, never a runtime route recipe; this
        # lets sparse products earn a truthful focused demo without padding a
        # full-tour script with artificial holds.
        objective = str(
            target.get("objective")
            or "Create a complete, evidence-grounded walkthrough of every safe primary section and meaningful visible content."
        )
        request = repository.create_request(request_id, target["url"], objective, project["id"])
        run, _created = repository.create_idempotent_run(request["id"], str(settings.artifact_root))
        record: dict[str, object] = {
            "name": target.get("name", target["url"]), "url": target["url"],
            "run_id": run["id"], "started_at": datetime.now(UTC).isoformat(),
        }
        try:
            await asyncio.wait_for(
                jobs.run_url(
                    run["id"], allow_external_side_effects=False, render=args.render,
                    cloud_discovery=args.cloud, audience="product prospect",
                    # Keep the acceptance objective inside the standard
                    # thorough-walkthrough envelope; the validated scene plan
                    # may still finish earlier when the product genuinely has
                    # fewer viable scenes, without renderer slowdown.
                    target_duration_seconds=120,
                    credential_reference=target.get("credential_reference"),
                ),
                timeout=overall_timeout,
            )
            record["status"] = "COMPLETE"
        except Exception as error:  # noqa: BLE001 - classify each target and continue
            record["status"] = "FAILED"
            record["error_type"] = type(error).__name__
        # Always retain durable evidence locations and the delivery verdict in
        # the batch manifest.  A successful coroutine alone is not proof that
        # a publishable MP4 exists; operators should be able to open the exact
        # run directory and inspect the owning QA layer without polling a
        # worker or guessing which artifact belongs to which URL.
        run_root = (settings.artifact_root / "runs" / run["id"]).resolve()
        record["artifact_root"] = str(run_root)
        final_video = run_root / "final" / "demo.mp4"
        record["final_video"] = str(final_video) if final_video.is_file() else None
        delivery_path = run_root / "qa" / "delivery-report.json"
        if delivery_path.is_file():
            try:
                delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
                record["deliverable"] = bool(delivery.get("deliverable", False))
                record["hard_failures"] = list(delivery.get("hard_failures", []))
            except (OSError, TypeError, ValueError):
                record["deliverable"] = False
                record["hard_failures"] = ["INVALID_DELIVERY_REPORT"]
        record["finished_at"] = datetime.now(UTC).isoformat()
        attempts.append(record)
        # A blocked/auth-required/infrastructure-failed public target must not
        # silently reduce the eight-project acceptance set. Replace it with a
        # manifest-declared, independently chosen public target, preserving the
        # same generic objective and evidence gates. Never synthesize a
        # site-specific fallback at runtime.
        accepted = _accepted_record(record)
        if accepted:
            records.append(record)
            accepted_urls.add(str(record.get("url")))
        else:
            rejected.append(record)
            rejected_urls.add(str(record.get("url")))
        output.write_text(json.dumps({"schema_version": 1, "runs": records, "attempts": attempts, "rejected": rejected}, indent=2), encoding="utf-8")
        if not accepted and fallback_index < len(fallbacks) and len(records) < target_limit:
            candidate = fallbacks[fallback_index]
            fallback_index += 1
            existing_urls = {str(item.get("url")) for item in [*targets, *attempts]}
            if str(candidate.get("url")) not in existing_urls:
                targets.append(candidate)
        target_index += 1


if __name__ == "__main__":
    asyncio.run(main())
