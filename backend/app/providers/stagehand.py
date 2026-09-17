"""Stagehand v4 semantic/visual bridge.

Stagehand supplies observations and bounded read-only rehearsal during
exploration. It never owns ProductLens production workflow truth: every hint
is re-grounded against the live Playwright DOM and production dispatch remains
inside the ProductLens interaction kernel.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from app.providers.errors import ProviderError
from app.providers.limits import provider_limit


@dataclass(frozen=True)
class StagehandCandidate:
    selector: str
    description: str
    method: str
    arguments: list[object]


@dataclass(frozen=True)
class StagehandPageAnalysis:
    """Bounded, advisory page semantics returned by Stagehand.

    This is deliberately not product knowledge.  The discovery layer must
    re-ground every phrase against the Playwright page before it can reach a
    plan, prompt, or persisted evidence artifact.
    """

    visible_sections: list[str]
    meaningful_controls: list[str]
    safe_next_actions: list[str]


@dataclass(frozen=True)
class StagehandActionResult:
    """Result of one pre-observed, bounded Stagehand action.

    Only discovery may use this.  A successful result is still not execution
    evidence until ProductLens' connected Playwright page re-observes the
    expected state.
    """

    success: bool
    message: str
    action: str
    observed_url: str | None = None
    environment: str = "LOCAL"


@dataclass(frozen=True)
class StagehandRehearsalResult:
    """Bounded agent rehearsal result; never production success evidence."""

    success: bool
    message: str
    actions: list[dict[str, object]]
    observed_url: str | None = None
    environment: str = "LOCAL"


@dataclass(frozen=True)
class StagehandObservation:
    candidates: list[StagehandCandidate]
    metrics: dict[str, object]
    observed_url: str | None = None
    environment: str = "LOCAL"
    analysis: StagehandPageAnalysis | None = None
    analysis_error: str | None = None


class StagehandProvider:
    """Invokes the separately installed Node Stagehand v4 bridge on demand."""

    def __init__(
        self,
        *,
        model: str | None = None,
        node: str = "node",
        bridge: Path | None = None,
        browserbase_api_key: str | None = None,
        browserbase_project_id: str | None = None,
        openrouter_api_key: str | None = None,
        openrouter_model: str | None = None,
    ):
        self.model, self.node = model, node
        self.browserbase_api_key = browserbase_api_key
        self.browserbase_project_id = browserbase_project_id
        self.openrouter_api_key = openrouter_api_key
        self.openrouter_model = openrouter_model
        self.bridge = bridge or Path(__file__).resolve().parents[2] / "stagehand" / "observe.mjs"
        self._invoke_limit = asyncio.Semaphore(provider_limit("stagehand"))

    async def observe(
        self,
        *,
        url: str,
        instruction: str,
        analysis_instruction: str | None = None,
        environment: str = "LOCAL",
        browserbase_session_id: str | None = None,
        browserbase_connect_url: str | None = None,
        browserbase_extension_id: str | None = None,
        cache_dir: Path | None = None,
    ) -> StagehandObservation:
        if environment not in {"LOCAL", "BROWSERBASE"}:
            raise ValueError("Stagehand environment must be LOCAL or BROWSERBASE")
        if environment == "BROWSERBASE" and not self.browserbase_api_key:
            raise ProviderError(
                "stagehand", None, "Browserbase API key is required for cloud observation"
            )
        if not self.bridge.is_file():
            raise ProviderError("stagehand", None, "Stagehand bridge is not installed")
        payload = {
            "url": url,
            "instruction": instruction,
            "analysisInstruction": analysis_instruction,
            "environment": environment,
            # A model is optional.  With Browserbase, omitting it deliberately
            # selects Model Gateway's supported default instead of guessing an
            # unsupported model id or leaking a second provider credential.
            "model": self.model or None,
            # Stagehand v4 attaches its extension-equipped driver to this
            # existing Browserbase session. That preserves the authenticated
            # page that ProductLens has already grounded while its returned
            # suggestions remain advisory and are re-grounded by Playwright.
            "browserbaseSessionID": browserbase_session_id,
            # Browserbase Functions and Stagehand's official integration use
            # the existing session's CDP URL with LOCAL mode. This preserves
            # the exact authenticated browser and avoids a second
            # connectSession handshake when Playwright already owns the
            # session. The URL is process-local and never written to an
            # artifact or log.
            "browserbaseConnectUrl": browserbase_connect_url,
            "stagehandExtensionId": browserbase_extension_id,
            "cacheDir": str(cache_dir) if cache_dir else None,
        }
        # Observation and action calls share the same bounded subprocess gate.
        # Without this path through ``_invoke``, concurrent discovery runs
        # could each start an unbounded Stagehand process and exhaust browser
        # slots before provider backpressure had a chance to apply.
        response = await self._invoke(payload)
        try:
            if response.get("version") not in {1, 2}:
                raise ValueError("unsupported bridge response")
            observed_url = response.get("observedUrl")
            if observed_url is not None and (
                not isinstance(observed_url, str) or not _same_origin(url, observed_url)
            ):
                # Preserve the non-secret URL diagnostic so a provider
                # redirect/attachment mismatch can be repaired without
                # guessing from an opaque ``origin`` failure.
                raise ValueError(
                    "observation left the requested origin: "
                    f"observed_origin={_origin_label(observed_url)} "
                    f"requested_origin={_origin_label(url)}"
                )
            raw_candidates = response.get("candidates")
            if not isinstance(raw_candidates, list):
                raise TypeError("candidates must be a list")
            candidates = [_validated_candidate(item) for item in raw_candidates]
            metrics = response.get("metrics", {})
            if not isinstance(metrics, dict):
                raise TypeError("metrics must be an object")
            analysis = _validated_analysis(response.get("analysis"))
            return StagehandObservation(
                candidates=candidates,
                metrics=metrics,
                observed_url=observed_url,
                environment=str(response.get("environment") or environment),
                analysis=analysis,
                analysis_error=(
                    str(response.get("analysisError"))[:500]
                    if response.get("analysisError")
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            # Keep diagnostics classifiable without persisting raw bridge
            # output (which may contain page text or provider details).
            message = str(error)
            category = (
                "origin"
                if "origin" in message
                else "version"
                if "version" in message or "bridge response" in message
                else "candidate"
                if "candidate" in message
                else "analysis"
                if "analysis" in message
                else "json"
            )
            raise ProviderError(
                "stagehand",
                None,
                f"Stagehand returned an invalid observation ({category})",
            ) from error

    async def act_observed(
        self,
        *,
        url: str,
        candidate: StagehandCandidate,
        environment: str = "LOCAL",
        browserbase_session_id: str | None = None,
        browserbase_connect_url: str | None = None,
        browserbase_extension_id: str | None = None,
        cache_dir: Path | None = None,
    ) -> StagehandActionResult:
        """Execute exactly one previously observed, non-secret action.

        This API intentionally accepts an existing candidate rather than a
        free-form instruction. Callers must first apply their side-effect and
        DOM-grounding policies; Stagehand receives no credentials or values.
        """
        if environment not in {"LOCAL", "BROWSERBASE"}:
            raise ValueError("Stagehand environment must be LOCAL or BROWSERBASE")
        if environment == "BROWSERBASE" and not self.browserbase_api_key:
            raise ProviderError(
                "stagehand", None, "Browserbase API key is required for cloud action"
            )
        if not self.bridge.is_file():
            raise ProviderError("stagehand", None, "Stagehand bridge is not installed")
        payload = {
            "mode": "act_observed",
            "url": url,
            "environment": environment,
            "model": self.model or None,
            "browserbaseSessionID": browserbase_session_id,
            "browserbaseConnectUrl": browserbase_connect_url,
            "stagehandExtensionId": browserbase_extension_id,
            "cacheDir": str(cache_dir) if cache_dir else None,
            "action": {
                "selector": candidate.selector,
                "description": candidate.description,
                "method": candidate.method,
                "arguments": candidate.arguments,
            },
        }
        response = await self._invoke(payload)
        try:
            if response.get("version") not in {2, 3} or response.get("mode") != "act_observed":
                raise ValueError("unsupported action bridge response")
            observed_url = response.get("observedUrl")
            if observed_url is not None and (
                not isinstance(observed_url, str) or not _same_origin(url, observed_url)
            ):
                raise ValueError("action left the requested origin")
            result = response.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
                raise TypeError("action result must be an object with success")
            message = str(result.get("message") or "")[:500]
            action = str(result.get("action") or candidate.description)[:500]
            return StagehandActionResult(
                success=result["success"],
                message=message,
                action=action,
                observed_url=observed_url,
                environment=str(response.get("environment") or environment),
            )
        except (TypeError, ValueError) as error:
            raise ProviderError(
                "stagehand", None, "Stagehand returned an invalid action result"
            ) from error

    async def rehearse_agent(
        self,
        *,
        url: str,
        instruction: str,
        environment: str = "LOCAL",
        browserbase_session_id: str | None = None,
        browserbase_connect_url: str | None = None,
        browserbase_extension_id: str | None = None,
        cache_dir: Path | None = None,
        max_steps: int = 12,
        agent_mode: str = "hybrid",
    ) -> StagehandRehearsalResult:
        """Run a bounded semantic/visual agent only in isolated rehearsal."""
        if not instruction.strip():
            raise ValueError("rehearsal instruction cannot be empty")
        if agent_mode not in {"hybrid", "dom"}:
            raise ValueError("agent_mode must be hybrid or dom")
        if not 1 <= max_steps <= 40:
            raise ValueError("max_steps must be between 1 and 40")
        if environment not in {"LOCAL", "BROWSERBASE"}:
            raise ValueError("Stagehand environment must be LOCAL or BROWSERBASE")
        if environment == "BROWSERBASE" and not self.browserbase_api_key:
            raise ProviderError(
                "stagehand", None, "Browserbase API key is required for cloud rehearsal"
            )
        if not self.bridge.is_file():
            raise ProviderError("stagehand", None, "Stagehand bridge is not installed")
        response = await self._invoke(
            {
                "mode": "rehearse_agent",
                "rehearsal": True,
                "url": url,
                "instruction": instruction.strip(),
                "environment": environment,
                "model": self.model or None,
                "browserbaseSessionID": browserbase_session_id,
                "browserbaseConnectUrl": browserbase_connect_url,
                "stagehandExtensionId": browserbase_extension_id,
                "cacheDir": str(cache_dir) if cache_dir else None,
                "maxSteps": max_steps,
                "agentMode": agent_mode,
            }
        )
        try:
            if response.get("version") != 4 or response.get("mode") != "rehearse_agent":
                raise ValueError("unsupported rehearsal bridge response")
            observed_url = response.get("observedUrl")
            if observed_url is not None and (
                not isinstance(observed_url, str) or not _same_origin(url, observed_url)
            ):
                raise ValueError("rehearsal left the requested origin")
            result = response.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
                raise TypeError("rehearsal result must be an object with success")
            raw_actions = result.get("actions", [])
            if not isinstance(raw_actions, list):
                raise TypeError("rehearsal actions must be a list")
            return StagehandRehearsalResult(
                success=result["success"],
                message=str(result.get("message") or "")[:1000],
                actions=[item for item in raw_actions if isinstance(item, dict)],
                observed_url=observed_url,
                environment=str(response.get("environment") or environment),
            )
        except (TypeError, ValueError) as error:
            raise ProviderError(
                "stagehand", None, "Stagehand returned an invalid rehearsal result"
            ) from error

    async def _invoke(self, payload: dict[str, object]) -> dict[str, object]:
        async with self._invoke_limit:
            return await self._invoke_unbounded(payload)

    async def _invoke_unbounded(self, payload: dict[str, object]) -> dict[str, object]:
        """Run the bridge with bounded lifetime and never expose its stderr."""
        process: asyncio.subprocess.Process | None = None
        try:
            environment_values = os.environ.copy()
            if self.browserbase_api_key:
                environment_values["BROWSERBASE_API_KEY"] = self.browserbase_api_key
            if self.browserbase_project_id:
                environment_values["BROWSERBASE_PROJECT_ID"] = self.browserbase_project_id
            if self.openrouter_api_key:
                environment_values["OPENROUTER_API_KEY"] = self.openrouter_api_key
            if self.openrouter_model:
                environment_values["OPENROUTER_MODEL"] = self.openrouter_model
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
            if process is not None:
                await _terminate_process_tree(process)
            raise ProviderError("stagehand", None, "Stagehand bridge failed to start") from error
        if process.returncode != 0:
            raise ProviderError("stagehand", None, stderr.decode(errors="replace")[:500])
        try:
            response = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise ProviderError("stagehand", None, "Stagehand returned invalid JSON") from error
        if not isinstance(response, dict):
            raise ProviderError("stagehand", None, "Stagehand returned an invalid bridge response")
        return response


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    """Bound the bridge lifetime without orphaning Chromium grandchildren.

    ``Process.terminate`` only signals the Node parent on Windows.  Stagehand
    starts Chromium below that parent, and inherited pipes can consequently
    keep ``communicate()`` waiting indefinitely after the provider deadline.
    Kill the complete process tree on Windows and use SIGKILL elsewhere.  This
    is cleanup only: it never retries an observation or replays a product
    action.
    """
    if process.returncode is not None:
        return
    try:
        if os.name == "nt" and getattr(process, "pid", None):
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(killer.wait(), timeout=5)
            except TimeoutError:
                killer.kill()
                await killer.wait()
        else:
            # Test doubles and platforms without a process-tree utility still
            # receive the normal parent termination signal.
            if os.name == "nt":
                process.terminate()
                await process.wait()
                return
            os.kill(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        # The bridge may have exited between the timeout and cleanup.
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()


def _same_origin(requested: str, observed: str) -> bool:
    request_origin = urlsplit(requested)
    observed_origin = urlsplit(observed)

    def host_port(value):
        hostname = (value.hostname or "").lower().rstrip(".")
        port = value.port
        if port is None:
            port = (
                443
                if value.scheme.lower() == "https"
                else 80
                if value.scheme.lower() == "http"
                else None
            )
        return hostname, port

    requested_host, requested_port = host_port(request_origin)
    observed_host, observed_port = host_port(observed_origin)
    if requested_host != observed_host:
        return False
    if request_origin.scheme.lower() == observed_origin.scheme.lower():
        return requested_port == observed_port
    # A public site commonly upgrades an HTTP entry URL to HTTPS. Treat that
    # redirect as the same product origin for advisory evidence, while never
    # permitting a secure request to be downgraded.
    return (
        request_origin.scheme.lower() == "http"
        and observed_origin.scheme.lower() == "https"
        and requested_port == 80
        and observed_port == 443
    )


def _origin_label(value: str) -> str:
    """Return a non-secret origin diagnostic (never path/query/token data)."""
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if not hostname or parsed.scheme.lower() not in {"http", "https"}:
            return "invalid"
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        return f"{parsed.scheme.lower()}://{hostname}:{port}"
    except ValueError:
        return "invalid"


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
        selector=selector.strip(),
        description=description.strip(),
        method=method.strip(),
        arguments=arguments[:20],
    )


def _validated_analysis(value: object) -> StagehandPageAnalysis | None:
    """Accept only a small, text-only advisory payload from the Node bridge."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("analysis must be an object")

    def phrases(key: str) -> list[str]:
        raw = value.get(key, [])
        if not isinstance(raw, list) or len(raw) > 16:
            raise TypeError(f"analysis.{key} must be a bounded list")
        result: list[str] = []
        for item in raw:
            if not isinstance(item, str) or not item.strip() or len(item) > 300:
                raise ValueError(f"analysis.{key} contains an invalid phrase")
            phrase = item.strip()
            if phrase not in result:
                result.append(phrase)
        return result

    return StagehandPageAnalysis(
        visible_sections=phrases("visibleSections"),
        meaningful_controls=phrases("meaningfulControls"),
        safe_next_actions=phrases("safeNextActions"),
    )
