"""Optional Stagehand v4 observation bridge.

Stagehand may suggest selectors, but it never executes ProductLens workflow
operations. Every candidate is re-grounded against the live Playwright DOM by
the ProductLens discovery/execution layers before it can influence a DemoPlan.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from productlens.providers.errors import ProviderError


@dataclass(frozen=True)
class StagehandCandidate:
    selector: str
    description: str
    method: str
    arguments: list[object]


@dataclass(frozen=True)
class StagehandObservation:
    candidates: list[StagehandCandidate]
    metrics: dict[str, object]
    observed_url: str | None = None
    environment: str = "LOCAL"


class StagehandProvider:
    """Invokes the separately installed Node Stagehand v4 bridge on demand."""

    def __init__(
        self,
        *,
        model: str | None = None,
        node: str = "node",
        bridge: Path | None = None,
        browserbase_api_key: str | None = None,
    ):
        self.model, self.node = model, node
        self.browserbase_api_key = browserbase_api_key
        self.bridge = bridge or Path(__file__).resolve().parents[3] / "stagehand" / "observe.mjs"

    async def observe(
        self,
        *,
        url: str,
        instruction: str,
        environment: str = "LOCAL",
        browserbase_session_id: str | None = None,
        cache_dir: Path | None = None,
    ) -> StagehandObservation:
        if environment not in {"LOCAL", "BROWSERBASE"}:
            raise ValueError("Stagehand environment must be LOCAL or BROWSERBASE")
        if environment == "BROWSERBASE" and not self.browserbase_api_key:
            raise ProviderError("stagehand", None, "Browserbase API key is required for cloud observation")
        if not self.bridge.is_file():
            raise ProviderError("stagehand", None, "Stagehand bridge is not installed")
        payload = {
            "url": url,
            "instruction": instruction,
            "environment": environment,
            # A model is optional.  With Browserbase, omitting it deliberately
            # selects Model Gateway's supported default instead of guessing an
            # unsupported model id or leaking a second provider credential.
            "model": self.model or None,
            # v4 creates an extension-equipped short-lived observation
            # session. Keep this parameter for call compatibility and audit
            # correlation only; connecting it to a raw Playwright CDP session
            # is unsupported by the current Stagehand runtime.
            "browserbaseSessionID": browserbase_session_id,
            "cacheDir": str(cache_dir) if cache_dir else None,
        }
        try:
            environment_values = os.environ.copy()
            # The Browserbase API key authenticates both the cloud browser and
            # Browserbase Model Gateway. It is never serialized into artifacts.
            if self.browserbase_api_key:
                environment_values["BROWSERBASE_API_KEY"] = self.browserbase_api_key
            process = await asyncio.create_subprocess_exec(
                self.node,
                str(self.bridge),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment_values,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(payload).encode()), timeout=90
            )
        except (OSError, TimeoutError) as error:
            raise ProviderError("stagehand", None, "Stagehand observation failed to start") from error
        if process.returncode != 0:
            raise ProviderError("stagehand", None, stderr.decode(errors="replace")[:500])
        try:
            response = json.loads(stdout)
            if not isinstance(response, dict) or response.get("version") != 1:
                raise ValueError("unsupported bridge response")
            observed_url = response.get("observedUrl")
            if observed_url is not None and (
                not isinstance(observed_url, str) or not _same_origin(url, observed_url)
            ):
                raise ValueError("observation left the requested origin")
            raw_candidates = response.get("candidates")
            if not isinstance(raw_candidates, list):
                raise TypeError("candidates must be a list")
            candidates = [_validated_candidate(item) for item in raw_candidates]
            metrics = response.get("metrics", {})
            if not isinstance(metrics, dict):
                raise TypeError("metrics must be an object")
            return StagehandObservation(
                candidates=candidates,
                metrics=metrics,
                observed_url=observed_url,
                environment=str(response.get("environment") or environment),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ProviderError("stagehand", None, "Stagehand returned an invalid observation") from error


def _same_origin(requested: str, observed: str) -> bool:
    request_origin = urlsplit(requested)
    observed_origin = urlsplit(observed)
    return (
        request_origin.scheme.lower(), request_origin.netloc.lower()
    ) == (
        observed_origin.scheme.lower(), observed_origin.netloc.lower()
    )


def _validated_candidate(value: object) -> StagehandCandidate:
    if not isinstance(value, dict):
        raise TypeError("candidate must be an object")
    selector = value.get("selector")
    description = value.get("description")
    method = value.get("method")
    arguments = value.get("arguments", [])
    if not all(isinstance(item, str) and item.strip() for item in (selector, description, method)):
        raise ValueError("candidate fields must be non-empty strings")
    if len(selector) > 2_000 or len(description) > 1_000 or not isinstance(arguments, list):
        raise ValueError("candidate fields exceed safe bridge limits")
    return StagehandCandidate(
        selector=selector.strip(), description=description.strip(), method=method.strip(), arguments=arguments[:20]
    )
