"""Run the configured public-target acceptance set without API polling.

The worker owns stage execution; this command only creates durable runs and
awaits each run in-process.  It is intentionally generic and reads targets
from ``validation/public-targets.json`` rather than embedding site behavior.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from productlens.config.settings import Settings
from productlens.services.runtime import build_job_service
from productlens.urls import canonical_product_url


def _target_sets(path: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    primary = [*payload.get("supplied_targets", []), *payload.get("targets", [])]
    fallbacks = list(payload.get("fallback_targets", []))
    return primary, fallbacks


def _target_key(value: object) -> str:
    """Return the canonical product identity used by discovery and caching."""
    try:
        return canonical_product_url(str(value or ""))
    except (TypeError, ValueError):
        return str(value or "").strip().rstrip("/").casefold()


def _historical_targets(path: Path) -> list[dict[str, str]]:
    """Return prior target URLs as data-only retry candidates."""
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for bucket in ("runs", "attempts", "rejected"):
        items = payload.get(bucket, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            canonical = _target_key(url)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            result.append({"name": str(item.get("name") or url), "url": url})
    return result


def _accepted_record(record: dict[str, object]) -> bool:
    """Return whether an attempt can enter the final acceptance set."""
    return (
        record.get("status") == "COMPLETE"
        and record.get("deliverable") is True
        and bool(record.get("final_video"))
        and not record.get("hard_failures")
    )


def _process_alive(pid: object) -> bool:
    """Return whether a recorded supervisor process is still alive."""
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    if os.name == "nt":
        # ``os.kill(pid, 0)`` is not a harmless existence probe on every
        # Windows/Python combination and can destabilize an embedded test
        # host. Query the process handle instead; this is also safe for the
        # long-lived acceptance supervisor.
        try:
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, value)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except (AttributeError, OSError):
            return False
    try:
        os.kill(value, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _refresh_record_from_artifacts(
    record: dict[str, object],
    repository: object | None = None,
) -> dict[str, object]:
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
    # Failed attempts historically retained only ``status=FAILED`` in the
    # acceptance manifest. Recover the durable stage/error classification from
    # the run repository so blocked targets are transparent and can be
    # retried at the correct layer instead of appearing as anonymous failures.
    run_id = str(refreshed.get("run_id") or "")
    if repository is not None and run_id:
        try:
            durable = repository.get_run(run_id)
        except Exception:  # noqa: BLE001 - an old/missing row must not block refresh
            durable = None
        if isinstance(durable, dict):
            if durable.get("stage"):
                refreshed["failed_stage"] = durable["stage"]
            if durable.get("error_code"):
                refreshed["error_code"] = durable["error_code"]
        if durable is not None and hasattr(repository, "stage_jobs"):
            try:
                failed_jobs = [
                    item
                    for item in repository.stage_jobs(run_id)
                    if isinstance(item, dict) and item.get("status") == "FAILED"
                ]
            except Exception:  # noqa: BLE001 - preserve refresh of legacy rows
                failed_jobs = []
            if failed_jobs:
                failed_job = failed_jobs[-1]
                refreshed["failed_stage"] = failed_job.get("stage") or refreshed.get("failed_stage")
                refreshed["error_code"] = failed_job.get("error_code") or refreshed.get(
                    "error_code"
                )
    final_video = root / "final" / "demo.mp4"
    delivery_path = root / "qa" / "delivery-report.json"
    if final_video.is_file():
        refreshed["final_video"] = str(final_video)
    if delivery_path.is_file():
        try:
            delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return refreshed
        refreshed["status"] = (
            "COMPLETE" if bool(delivery.get("deliverable")) else refreshed.get("status", "FAILED")
        )
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
        "--retry-url",
        action="append",
        default=[],
        help="Target a previously rejected URL after a generic code/config fix (repeatable).",
    )
    parser.add_argument(
        "--retry-rejected",
        action="store_true",
        help="Allow the configured target slice to be retried after a generic fix.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Force independent new attempts even when a URL already has an accepted run.",
    )
    parser.add_argument(
        "--all-targets",
        action="store_true",
        help="Include configured and historical target URLs in the fresh retry matrix.",
    )
    parser.add_argument(
        "--refresh-only",
        action="store_true",
        help="Refresh accepted/rejected records from existing artifacts without starting browser work.",
    )
    args = parser.parse_args()
    settings = Settings.from_environment()
    repository, jobs = build_job_service(settings)
    target_config = args.targets.resolve()
    primary, fallbacks = _target_sets(target_config)
    output = (args.artifact_root or settings.artifact_root) / "acceptance" / "public-runs.json"
    if args.all_targets:
        configured_urls = {_target_key(item.get("url", "")) for item in [*primary, *fallbacks]}
        for historical in _historical_targets(output):
            url = str(historical.get("url", "")).rstrip("/")
            if url and _target_key(url) not in configured_urls:
                fallbacks.append(historical)
                configured_urls.add(_target_key(url))
    retry_urls = {_target_key(url) for url in args.retry_url if str(url).strip()}
    target_limit = max(1, args.limit)
    if args.all_targets:
        targets = [*primary, *fallbacks]
        # ``--all-targets`` is also used for bounded batches.  Respect the
        # same start/limit semantics as the normal matrix so an operator can
        # resume a large historical sweep without accidentally launching the
        # entire catalog in one invocation.
        targets = targets[args.start_at : args.start_at + target_limit]
    else:
        targets = primary[args.start_at : args.start_at + target_limit]
    if retry_urls:
        configured = {_target_key(item.get("url")): item for item in [*primary, *fallbacks]}
        requested_targets: list[dict[str, str]] = []
        for url in retry_urls:
            if url in configured:
                requested_targets.append(configured[url])
        # An explicit retry URL is an operator-selected scope.  Do not also
        # run the default first slice (which previously caused ``--retry-url``
        # to create an unrelated fresh Portfolio attempt).
        if requested_targets:
            targets = requested_targets
    output.parent.mkdir(parents=True, exist_ok=True)
    sweep_status_path = output.parent / "public-sweep-status.json"
    sweep_lock_path = output.parent / "public-sweep.lock.json"
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
                item = _refresh_record_from_artifacts(item, repository)
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
                item = _refresh_record_from_artifacts(raw_item, repository)
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
                item = _refresh_record_from_artifacts(raw_item, repository)
                final_video = Path(str(item.get("final_video", "")))
                if _accepted_record(item) and final_video.is_file():
                    if item not in records:
                        records.append(item)
                elif item not in rejected:
                    rejected.append(item)
    accepted_urls = {_target_key(item.get("url")) for item in records}
    rejected_urls = {
        _target_key(item.get("url"))
        for item in [*attempts, *rejected]
        if isinstance(item, dict) and item.get("status") == "FAILED" and item.get("url")
    }
    # ``--start-at`` is a targeted retry/resume selector.  Its limit denotes
    # how many additional primary targets to attempt, not an absolute count
    # that would be satisfied immediately by previously accepted runs.
    if args.start_at:
        target_limit = len(records) + target_limit
    elif args.fresh:
        # ``limit`` describes new fresh attempts, not the number of retained
        # historical accepted records already present in the manifest.
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
    if args.refresh_only:
        # A watcher may run while the owning supervisor is still processing a
        # target.  Never convert an in-flight status into COMPLETE merely
        # because the previous manifest happens to contain terminal records.
        # The lock is advisory and stale locks are harmless: a live PID is
        # required to suppress the checkpoint.
        try:
            active_lock = json.loads(sweep_lock_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            active_lock = {}
        if isinstance(active_lock, dict) and _process_alive(active_lock.get("pid")):
            print(json.dumps({"status": "RUNNING", "active_pid": active_lock.get("pid")}))
            return
        # A refresh-only invocation is also a durable completion checkpoint for
        # a supervisor that was restarted after its process output was lost.
        # It is safe to mark the sweep complete only when every configured and
        # historical target has a terminal accepted/rejected record; the
        # completion audit performs the stricter artifact/error validation.
        expected_urls = {
            _target_key(item.get("url"))
            for item in targets
            if isinstance(item, dict) and item.get("url")
        }
        completed_urls = {
            _target_key(item.get("url"))
            for item in [*records, *rejected]
            if isinstance(item, dict) and item.get("url")
        }
        if expected_urls.issubset(completed_urls):
            (output.parent / "public-sweep-status.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "COMPLETE",
                        "finished_at": datetime.now(UTC).isoformat(),
                        "target_count": len(expected_urls),
                        "target_urls": sorted(expected_urls),
                        "completed_urls": sorted(completed_urls),
                        "runs": len(records),
                        "attempts": len(attempts),
                        "rejected": len(rejected),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        print(
            json.dumps({"runs": len(records), "attempts": len(attempts), "rejected": len(rejected)})
        )
        return
    sweep_started_at = datetime.now(UTC).isoformat()
    sweep_completed_urls: set[str] = set()

    sweep_lock_path.write_text(
        json.dumps(
            {"pid": os.getpid(), "started_at": sweep_started_at, "targets": len(targets)},
            indent=2,
        ),
        encoding="utf-8",
    )

    def write_sweep_status(status: str) -> None:
        """Persist supervisor progress independently of the run manifest."""
        sweep_status_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": status,
                    "started_at": sweep_started_at,
                    "finished_at": datetime.now(UTC).isoformat() if status == "COMPLETE" else None,
                    "target_count": len(targets),
                    "target_urls": sorted(
                        {_target_key(item.get("url")) for item in targets if item.get("url")}
                    ),
                    "completed_urls": sorted(sweep_completed_urls),
                    "runs": len(records),
                    "attempts": len(attempts),
                    "rejected": len(rejected),
                    "cloud": bool(args.cloud),
                    "render": bool(args.render),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    write_sweep_status("RUNNING")
    print(
        json.dumps(
            {
                "event": "acceptance_started",
                "targets": len(targets),
                "cloud": args.cloud,
                "render": args.render,
            }
        ),
        flush=True,
    )
    # Cloud capture has its own bounded deadline, but a complete delivery also
    # includes local Remotion rendering and layered QA.  Do not reuse the
    # provider lease timeout as the end-to-end job timeout: a healthy 2–3
    # minute 1080p render can legitimately outlive the Browserbase session.
    # The worker remains bounded, while this supervisor allows enough time for
    # two resumable render chunks plus QA before classifying a target as hung.
    # A target must be bounded independently: a provider/browser hang must
    # not hold the complete acceptance matrix forever.  The bound includes
    # the configured Browserbase lease plus a generous local render/QA
    # allowance, while remaining below the previous two-hour watchdog that
    # could leave a supervisor apparently alive with no progress.
    overall_timeout = max(3_600, settings.cloud_capture_timeout_seconds + 2_400)
    fallback_index = 0
    target_index = 0
    while target_index < len(targets) and (args.all_targets or len(records) < target_limit):
        target = targets[target_index]
        if (
            _target_key(target.get("url")) in accepted_urls
            and not args.fresh
            and _target_key(target.get("url")) not in retry_urls
        ):
            target_index += 1
            continue
        # A prior bounded timeout or planning rejection is already durable
        # evidence that this URL should be replaced for the acceptance set.
        # Do not spend another Browserbase/OpenRouter run retrying it unless an
        # operator explicitly removes the attempt from the manifest.
        target_url = str(target.get("url"))
        if (
            _target_key(target_url) in rejected_urls
            and _target_key(target_url) not in retry_urls
            and not args.retry_rejected
        ):
            target_index += 1
            existing_urls = {str(item.get("url")) for item in [*targets, *attempts]}
            while fallback_index < len(fallbacks):
                candidate = fallbacks[fallback_index]
                fallback_index += 1
                candidate_url = str(candidate.get("url"))
                if (
                    _target_key(candidate_url) not in {_target_key(item) for item in existing_urls}
                    and _target_key(candidate_url) not in rejected_urls
                ):
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
            "name": target.get("name", target["url"]),
            "url": target["url"],
            "run_id": run["id"],
            "started_at": datetime.now(UTC).isoformat(),
        }
        print(
            json.dumps(
                {
                    "event": "target_started",
                    "name": record["name"],
                    "url": record["url"],
                    "run_id": record["run_id"],
                }
            ),
            flush=True,
        )
        try:
            await asyncio.wait_for(
                jobs.run_url(
                    run["id"],
                    allow_external_side_effects=False,
                    render=args.render,
                    cloud_discovery=args.cloud,
                    audience="product prospect",
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
        # Reconcile the supervisor result with durable run artifacts before
        # classifying acceptance. A worker can finish its render/QA checkpoint
        # just as the coroutine deadline fires; the immutable delivery report
        # and repository stage are the authoritative verdict in that race.
        record = _refresh_record_from_artifacts(record, repository)
        attempts.append(record)
        # A blocked/auth-required/infrastructure-failed public target must not
        # silently reduce the eight-project acceptance set. Replace it with a
        # manifest-declared, independently chosen public target, preserving the
        # same generic objective and evidence gates. Never synthesize a
        # site-specific fallback at runtime.
        accepted = _accepted_record(record)
        if accepted:
            records.append(record)
            accepted_urls.add(_target_key(record.get("url")))
        else:
            rejected.append(record)
            rejected_urls.add(_target_key(record.get("url")))
        print(
            json.dumps(
                {
                    "event": "target_finished",
                    "name": record["name"],
                    "run_id": record["run_id"],
                    "status": record["status"],
                    "deliverable": bool(record.get("deliverable", False)),
                    "error_type": record.get("error_type"),
                }
            ),
            flush=True,
        )
        output.write_text(
            json.dumps(
                {"schema_version": 1, "runs": records, "attempts": attempts, "rejected": rejected},
                indent=2,
            ),
            encoding="utf-8",
        )
        sweep_completed_urls.add(_target_key(record.get("url")))
        write_sweep_status("RUNNING")
        if (
            not args.all_targets
            and not accepted
            and fallback_index < len(fallbacks)
            and len(records) < target_limit
        ):
            candidate = fallbacks[fallback_index]
            fallback_index += 1
            existing_urls = {str(item.get("url")) for item in [*targets, *attempts]}
            if _target_key(candidate.get("url")) not in {
                _target_key(item) for item in existing_urls
            }:
                targets.append(candidate)
        target_index += 1

    print(
        json.dumps(
            {
                "event": "acceptance_finished",
                "runs": len(records),
                "attempts": len(attempts),
                "rejected": len(rejected),
            }
        ),
        flush=True,
    )
    write_sweep_status("COMPLETE")
    try:
        sweep_lock_path.unlink()
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
