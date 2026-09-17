from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from pydantic import ValidationError

from app.artifacts.store import RunArtifacts
from app.contracts.models import (
    DiscoveryBudget,
    ExplorationReport,
    ObjectiveSpec,
    ProductContext,
    ViewportDecision,
)
from app.discovery.live import (
    _objective_spec,
    _page_knowledge,
    adaptive_exploration_budget,
)
from app.observability.logging import redact_prompt_text
from app.presentation.viewport import choose_viewport, probe_viewport_candidates
from app.providers.errors import ProviderError
from app.services.generation_policy import (
    canonical_url as _canonical_url,
)
from app.services.knowledge import product_knowledge_payload

from .render import GenerationPreconditionError


def _relevance_graph(context: ProductContext) -> dict[str, object]:
    """Build a compact evidence graph from observed pages and controls."""
    nodes: list[dict[str, object]] = []
    edges: list[dict[str, str]] = []
    page_ids: dict[str, str] = {}
    for index, page in enumerate(context.page_knowledge):
        page_id = f"page:{index}:{page.fingerprint}"
        page_ids[page.url] = page_id
        nodes.append(
            {
                "id": page_id,
                "kind": "page",
                "label": page.title,
                "url": page.url,
                "purpose": page.purpose,
                "relevance": next(
                    (
                        feature.relevance_score
                        for feature in context.feature_knowledge
                        if page.url in feature.entry_urls
                    ),
                    0.0,
                ),
                "evidence": list(page.evidence_refs),
            }
        )
    for index, feature in enumerate(context.feature_knowledge):
        feature_id = f"feature:{index}:{feature.name}"
        nodes.append(
            {
                "id": feature_id,
                "kind": "feature",
                "label": feature.name,
                "purpose": feature.purpose,
                "relevance": feature.relevance_score,
                "evidence": list(feature.evidence),
            }
        )
        for url in feature.entry_urls:
            page_id = page_ids.get(url)
            if page_id:
                edges.append({"source": page_id, "target": feature_id, "relation": "exposes"})
        for url in feature.related_urls:
            page_id = page_ids.get(url)
            if page_id:
                edges.append({"source": feature_id, "target": page_id, "relation": "supports"})
    for page in context.page_knowledge:
        source = page_ids.get(page.url)
        if not source:
            continue
        for control in page.actionable_controls[:30]:
            control_id = f"control:{page.fingerprint}:{control}"
            nodes.append({"id": control_id, "kind": "control", "label": control, "url": page.url})
            edges.append({"source": source, "target": control_id, "relation": "contains"})
    # Relationship edges are compiled from page evidence during discovery.
    # Keep them separate from navigation edges so downstream planning can use
    # the graph for explanatory context without treating it as a route list.
    for index, relationship in enumerate(getattr(context, "relationships", [])):
        source_id = f"concept:source:{index}:{relationship.source}"
        target_id = f"concept:target:{index}:{relationship.target}"
        nodes.extend(
            [
                {"id": source_id, "kind": "concept", "label": relationship.source},
                {"id": target_id, "kind": "concept", "label": relationship.target},
            ]
        )
        edges.append({"source": source_id, "target": target_id, "relation": relationship.relation})
    # Keep the artifact deterministic and compact for review/caching.
    unique_edges = list(
        {(item["source"], item["target"], item["relation"]): item for item in edges}.values()
    )
    return {"schema_version": 1, "nodes": nodes, "edges": unique_edges}

def _product_knowledge_payload(
    context: ProductContext, *, project_id: str | None = None
) -> dict[str, object]:
    """Compatibility alias for the canonical knowledge materializer."""
    return product_knowledge_payload(context, project_id=project_id)

class DiscoverMixin:
    async def _understand_objective(
        self, objective: str
    ) -> tuple[ObjectiveSpec, dict[str, object]]:
        """Parse request intent with a bounded model pass and safe constraints.

        The model may improve audience/scope vocabulary, but deterministic
        parsing remains authoritative for safety, duration, and completeness.
        No model-inferred entity or relationship is accepted unless its terms
        are grounded in the user's request.
        """
        base = _objective_spec(objective)
        structured = getattr(getattr(self.planner, "provider", None), "structured", None)
        if structured is None:
            return base, {"status": "fallback", "reason": "no_structured_provider"}
        schema = json.dumps(ObjectiveSpec.model_json_schema(), ensure_ascii=False)
        prompt = (
            "You are the objective-understanding stage of a URL demo engine. Interpret "
            "this demo request and return only "
            "a JSON object that conforms to the ObjectiveSpec schema below. The raw field "
            "must exactly equal the request. Extract intent; never invent a product, URL, "
            "feature, relationship, outcome, credential, or safety permission. Keep the "
            "requested scope: a focused workflow selects the requested area plus only the "
            "supporting context needed to explain it; a full/thorough walkthrough covers "
            "safe primary sections and meaningful local content. Do not turn a list of "
            "verbs into must-show evidence. ProductLens will validate all claims against "
            "observed DOM/accessibility/screenshot evidence later.\n\n"
            "Field guidance:\n"
            "- demo_type: choose the request's explicit mode; do not upgrade a focused "
            "request to full_walkthrough.\n"
            "- requested_features/must_show/exclusions/constraints/success_criteria: copy only items "
            "actually stated by the user.\n"
            "- supporting_relationships: include only relationships explicitly stated or "
            "unambiguously described in the request; never infer product-specific links.\n"
            "- safe_action_policy/permitted_mutations: preserve the conservative defaults; "
            "never grant mutation permission from prose alone.\n\n"
            "Acceptable examples:\n"
            "Request: 'Show the invoice export workflow for finance managers, including "
            "the resulting CSV.' -> demo_type=feature_walkthrough, requested_features="
            "['invoice export'], audience='finance managers', must_show includes the "
            "resulting CSV.\n"
            "Request: 'Give a complete walkthrough of the product.' -> demo_type="
            "full_walkthrough, depth=thorough, with no invented module names.\n\n"
            "Unacceptable: adding a dashboard module not named by the request, claiming "
            "a save succeeded, or enabling create/send/delete actions.\n\n"
            f"ObjectiveSpec JSON schema:\n{schema}\n\nRequest:\n{redact_prompt_text(objective)}"
        )
        try:
            candidate = await structured(prompt, ObjectiveSpec)
            raw_terms = set(re.findall(r"[a-z0-9]{4,}", objective.casefold()))

            def grounded_phrase(value: str | None) -> bool:
                terms = set(re.findall(r"[a-z0-9]{4,}", (value or "").casefold()))
                return bool(terms & raw_terms)

            # A model can turn ordinary prose such as "Home identity and
            # capabilities" into a list of apparent relationships.  Only keep
            # a relationship when the request itself contains an explicit
            # connector; otherwise it is editorial wording, not a planning
            # dependency.  Explicit relationships parsed deterministically in
            # ``base`` remain authoritative.
            def explicitly_related(source: str, target: str) -> bool:
                source_words = [w for w in re.findall(r"[a-z0-9]{3,}", source.casefold())]
                target_words = [w for w in re.findall(r"[a-z0-9]{3,}", target.casefold())]
                if not source_words or not target_words:
                    return False
                source_pattern = r"\s+".join(map(re.escape, source_words))
                target_pattern = r"\s+".join(map(re.escape, target_words))
                connector = r"(?:->|→|configures|explains|supports|in the context of|configured by|with context from|using)"
                return bool(
                    re.search(
                        rf"{source_pattern}\s*{connector}\s*{target_pattern}", objective.casefold()
                    )
                    or re.search(
                        rf"{target_pattern}\s*{connector}\s*{source_pattern}", objective.casefold()
                    )
                )

            relationships = [
                relation
                for relation in candidate.supporting_relationships
                if grounded_phrase(relation.source)
                and grounded_phrase(relation.target)
                and explicitly_related(relation.source, relation.target)
            ]
            model_generic_entities = {
                "thorough",
                "complete",
                "full",
                "detailed",
                "walkthrough",
                "tour",
                "demo",
                "workflow",
                "flow",
                "experience",
                "application",
                "product",
                "focused",
                "feature",
                "observed",
                "public",
                "meaningful",
                "safe",
                "most",
                "information",
                "browsing",
                "discovery",
                "detail",
                "resource",
            }
            candidate_entity_words = set(
                re.findall(r"[a-z0-9]{3,}", (candidate.primary_entity or "").casefold())
            )
            primary = (
                candidate.primary_entity
                if grounded_phrase(candidate.primary_entity)
                and candidate_entity_words
                and not candidate_entity_words.issubset(model_generic_entities)
                else base.primary_entity
            )
            depth_rank = {"overview": 0, "standard": 1, "thorough": 2}
            depth = max((base.depth, candidate.depth), key=lambda item: depth_rank[item])
            demo_type = (
                "full_walkthrough"
                if base.demo_type == "full_walkthrough"
                # Model output may not broaden a focused request merely
                # because the user asked for thorough depth; that would turn
                # a lead/module tour into an unrelated whole-product crawl.
                else candidate.demo_type
                if candidate.demo_type != "full_walkthrough"
                else base.demo_type
            )
            # An explicitly complete/full objective owns the long walkthrough
            # duration envelope. A request can still ask for a thorough
            # *feature* walkthrough without silently broadening exploration to
            # the entire product. These are generic editorial envelopes, not
            # product-specific timing or route rules.
            duration_update: dict[str, int] = {}
            if demo_type == "full_walkthrough":
                duration_update = {
                    "minimum_duration_seconds": max(base.minimum_duration_seconds, 110),
                    "target_duration_seconds": max(base.target_duration_seconds, 180),
                    "maximum_duration_seconds": max(base.maximum_duration_seconds, 240),
                }
            merged_video_type = (
                "full_tour"
                if base.demo_type == "full_walkthrough"
                and candidate.video_type == "feature_walkthrough"
                else candidate.video_type or base.video_type
            )
            merged = base.model_copy(
                update={
                    "video_type": merged_video_type,
                    "demo_type": demo_type,
                    "audience": candidate.audience.strip()[:160] or base.audience,
                    # AudienceProfile is editorial metadata, not permission to
                    # broaden scope or authorize mutations. Preserve the bounded
                    # enum/list contract from the objective-understanding pass so
                    # planning, narration, and QA share one viewer profile.
                    "audience_profile": candidate.audience_profile,
                    "purpose": candidate.purpose.strip()[:240] or base.purpose,
                    "tone": candidate.tone or base.tone,
                    "depth": depth,
                    "requested_features": list(
                        dict.fromkeys([*base.requested_features, *candidate.requested_features])
                    )[:24],
                    "primary_entity": primary,
                    "supporting_relationships": relationships or base.supporting_relationships,
                    # For action-led visual objectives, model-extracted
                    # nouns such as "client component" describe content to
                    # create on the observed surface, not labels that must
                    # already exist in the opening DOM.  Requiring those
                    # nouns as pre-existing evidence rejects valid editors
                    # before planning can compile their semantic gestures.
                    # Keep only deterministic requirements for that class;
                    # ordinary feature/page objectives remain strict.
                    "must_show": list(
                        dict.fromkeys(
                            [
                                *base.must_show,
                                *(
                                    []
                                    if (
                                        re.search(
                                            r"\b(?:create|build|draw|design|make|edit|sketch)\b",
                                            objective.casefold(),
                                        )
                                        and re.search(
                                            r"\b(?:diagram|architecture|whiteboard|canvas|drawing|flowchart)\b",
                                            objective.casefold(),
                                        )
                                    )
                                    else [
                                        item
                                        for item in candidate.must_show
                                        if grounded_phrase(item)
                                    ]
                                ),
                            ]
                        )
                    )[:24],
                    "exclusions": list(
                        dict.fromkeys(
                            [
                                *base.exclusions,
                                *[item for item in candidate.exclusions if grounded_phrase(item)],
                            ]
                        )
                    )[:24],
                    "constraints": list(
                        dict.fromkeys(
                            [
                                *base.constraints,
                                *[item for item in candidate.constraints if grounded_phrase(item)],
                            ]
                        )
                    )[:24],
                    "success_criteria": list(
                        dict.fromkeys(
                            [
                                *base.success_criteria,
                                *[
                                    item
                                    for item in candidate.success_criteria
                                    if grounded_phrase(item)
                                ],
                            ]
                        )
                    )[:24],
                    **duration_update,
                }
            )
            return merged, {
                "status": "model_grounded",
                "candidate_relationships": len(relationships),
            }
        except (
            ProviderError,
            ValidationError,
            TypeError,
            ValueError,
            KeyError,
            AttributeError,
        ) as error:
            return base, {"status": "fallback", "reason": type(error).__name__}

    async def _choose_production_viewport(
        self, page, *, url: str, context: ProductContext, objective: str, artifacts: RunArtifacts
    ) -> ViewportDecision:
        """Probe the clean opening layout; fall back only when a provider cannot resize."""
        try:
            # Discovery may finish on a supporting page. Viewport choice is a
            # presentation decision for the opening product context, so probe
            # the canonical requested entry route rather than the last crawl
            # location. This navigation belongs to exploration, never capture.
            if _canonical_url(page.url) != _canonical_url(url):
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_timeout(350)
            decision, probes = await probe_viewport_candidates(
                page, context.elements, objective, context.page_knowledge
            )
            artifacts.write_json(
                "discovery/viewport-probe.json",
                {"selected": decision.model_dump(mode="json"), "candidates": probes},
            )
            return decision
        except (PlaywrightError, PlaywrightTimeoutError, TypeError, ValueError) as error:
            decision = choose_viewport(context.elements, objective, context.page_knowledge)
            artifacts.write_json(
                "discovery/viewport-probe.json",
                {
                    "selected": decision.model_dump(mode="json"),
                    "candidates": [],
                    "fallback_reason": type(error).__name__,
                },
            )
            return decision

    async def discover_stage(
        self,
        *,
        run_id: str,
        url: str,
        objective: str,
        artifact_root: Path,
        budget: DiscoveryBudget | None = None,
        cloud_discovery: bool = False,
        known_routes: list[str] | None = None,
        known_actions: list[dict] | None = None,
        known_product_fingerprint: str | None = None,
        explore_visible_routes: bool = False,
        stagehand_assist: bool = False,
        credential_reference: str | None = None,
        allow_isolated_record_creation: bool = False,
    ) -> ProductContext:
        """Discover product evidence and persist only non-secret, reloadable state."""
        artifacts = RunArtifacts(artifact_root, run_id)
        budget = budget or DiscoveryBudget()
        objective_spec, objective_understanding = await self._understand_objective(objective)
        artifacts.write_json(
            "discovery/objective-understanding.json",
            {
                "request": objective,
                "objective": objective_spec.model_dump(mode="json"),
                **objective_understanding,
            },
        )
        # Track authentication at the browser boundary. Discovery runs after
        # login, so a post-login DOM cannot be used to infer that the login
        # chapter happened; the boolean is persisted onto ProductContext and
        # consumed by planning/editorial validation.
        authenticated = False
        async with async_playwright() as pw:
            # Cloud discovery owns a Browserbase browser. Launching an extra
            # local Chromium here is wasteful and can hang before the cloud
            # timeout is even entered; only local discovery needs this browser.
            browser = None if cloud_discovery else await pw.chromium.launch()
            try:
                stagehand_evidence: dict | None = None
                if cloud_discovery:
                    if self.browserbase_provider is None:
                        raise GenerationPreconditionError(
                            "BROWSERBASE_REQUIRED: cloud discovery is not configured"
                        )
                    try:
                        session = await asyncio.wait_for(
                            self.browserbase_provider.create_session_info(
                                viewport={"width": 1440, "height": 900},
                                user_metadata={"productlens_run_id": run_id, "stage": "discovery"},
                            ),
                            timeout=60,
                        )
                    except ProviderError as error:
                        artifacts.write_json(
                            "qa/execution-report.json",
                            {
                                "outcome_verified": False,
                                "event_count": 0,
                                "hard_failures": [error.failure_code],
                                "provider": "browserbase",
                                "provider_status": error.status_code,
                            },
                        )
                        raise
                    artifacts.write_json(
                        "discovery/browserbase-session.json",
                        {
                            "provider": "browserbase",
                            "session_id": session.session_id,
                            # Extension IDs are non-secret correlation data;
                            # the CDP signing URL is intentionally excluded.
                            "stagehand_extension_id": session.stagehand_extension_id,
                        },
                    )
                    # CDP transports occasionally acknowledge cancellation
                    # only after their remote session is released. This guard
                    # runs independently of the discovery coroutine, so a
                    # stuck websocket cannot consume the full Browserbase
                    # session timeout or leave an apparently-running job.
                    # The lease guard must outlive the bounded discovery or
                    # capture stage. A fixed five-minute ceiling evicted
                    # legitimate thorough explorations (especially once
                    # Stagehand and page-local scroll evidence were enabled)
                    # and surfaced as TargetClosedError mid-page. Keep a
                    # finite safety cap, but derive it from the configured
                    # stage deadline and leave teardown headroom.
                    lease_guard_seconds = min(
                        1_500, max(300, self.cloud_capture_timeout_seconds + 120)
                    )
                    lease_guard = asyncio.create_task(
                        self._release_cloud_session_after(
                            session.session_id,
                            seconds=lease_guard_seconds,
                            reason="discovery_stage_deadline",
                            run_id=run_id,
                        )
                    )
                    remote = None
                    try:
                        artifacts.write_json(
                            "discovery/browserbase-connection.json",
                            {
                                "provider": "browserbase",
                                "session_id": session.session_id,
                                "state": "connecting",
                                "native_timeout_ms": 60_000,
                                "outer_timeout_seconds": 65,
                            },
                        )
                        remote = await asyncio.wait_for(
                            pw.chromium.connect_over_cdp(session.connect_url, timeout=60_000),
                            timeout=65,
                        )
                        artifacts.write_json(
                            "discovery/browserbase-connection.json",
                            {
                                "provider": "browserbase",
                                "session_id": session.session_id,
                                "state": "connected",
                                "native_timeout_ms": 60_000,
                                "outer_timeout_seconds": 65,
                            },
                        )
                        context = remote.contexts[0]
                        page = context.pages[0] if context.pages else await context.new_page()
                        # Browserbase's provisioned viewport is not a stable
                        # editorial input. Lock discovery to the same desktop
                        # baseline used for local evidence collection so a
                        # responsive/mobile-only duplicate does not hide the
                        # primary navigation and force a direct-route probe.
                        # CDP operations can stall independently of
                        # Playwright's navigation timeout when a remote
                        # Browserbase page has been reclaimed or is still
                        # provisioning. Bound every pre-discovery action so a
                        # connected session cannot consume its full account
                        # lease before the normal discovery timeout begins.
                        await asyncio.wait_for(
                            page.set_viewport_size({"width": 1440, "height": 900}),
                            timeout=30,
                        )
                        await asyncio.wait_for(
                            page.goto(url, wait_until="domcontentloaded", timeout=30_000),
                            timeout=35,
                        )
                        authenticated = await asyncio.wait_for(
                            self.credential_service.authenticate_if_required(
                                page, credential_reference
                            ),
                            # Authentication deliberately includes sequential
                            # typing, CAPTCHA enablement (up to 45 seconds),
                            # and a post-submit authenticated-state check.
                            # Keep the provider boundary finite, but leave
                            # enough room for those intentional phases and
                            # remote CDP latency so an otherwise valid login
                            # is not cancelled into an opaque TimeoutError.
                            timeout=120,
                        )
                        # The exploration budget is a hard product-owned
                        # deadline; the Browserbase lease is only an outer
                        # provider limit.  Leave bounded teardown headroom,
                        # but do not let a stalled route/bridge consume the
                        # entire paid session lease.
                        # The outer watchdog must cover the same adaptive
                        # envelope as LiveDiscovery.  Previously it used only
                        # the cheap 60-second default, so an interactive
                        # objective could be cancelled while its bounded
                        # reversible probe was still running.  Estimate the
                        # page-independent floor here; discovery will refine
                        # it after observing the primary navigation.
                        stage_budget = adaptive_exploration_budget(
                            budget, objective_spec, primary_route_count=0
                        )
                        discovery_deadline = (
                            min(
                                self.cloud_capture_timeout_seconds,
                                max(240, int(stage_budget.max_time_seconds) + 180),
                            )
                            if cloud_discovery
                            else 900
                        )
                        async with asyncio.timeout(discovery_deadline):
                            product = await self.discovery.discover(
                                page,
                                objective,
                                budget,
                                known_routes=known_routes,
                                known_actions=known_actions,
                                explore_visible_routes=explore_visible_routes,
                                objective_spec=objective_spec,
                                known_product_fingerprint=known_product_fingerprint,
                                # Authenticated screens can contain customer data.
                                # Do not persist their pixels until redaction is a
                                # deliberate, provider-independent capability.
                                screenshot_directory=None
                                if credential_reference
                                else artifacts.root / "discovery" / "screenshots",
                            )
                        artifacts.write_json(
                            "discovery/adaptive-budget.json",
                            {
                                "requested": budget.model_dump(mode="json"),
                                "effective": (
                                    product.effective_discovery_budget.model_dump(mode="json")
                                    if product.effective_discovery_budget is not None
                                    else budget.model_dump(mode="json")
                                ),
                                "reason": (
                                    "visible primary navigation required expansion"
                                    if product.effective_discovery_budget is not None
                                    and product.effective_discovery_budget.max_pages
                                    != budget.max_pages
                                    else "default bounded budget"
                                ),
                            },
                        )
                        # Cloud discovery always obtains an advisory semantic
                        # observation when Stagehand is configured.  The
                        # historical request flag made AI understanding an
                        # accidental opt-in, while every returned datum is
                        # still re-grounded against this Playwright page.
                        # Always execute the Stagehand boundary for cloud
                        # discovery.  The provider may be unavailable, but
                        # that fact must be persisted and considered by the
                        # planner instead of silently falling back to a
                        # route/template tour.  Mutation/interaction
                        # objectives require a behavior witness; a missing
                        # witness is a classified pre-production blocker.
                        product, stagehand_evidence = await self._stagehand_enrich(
                            page,
                            product,
                            objective,
                            environment="BROWSERBASE",
                            browserbase_session_id=session.session_id,
                            browserbase_connect_url=session.connect_url,
                            browserbase_extension_id=session.stagehand_extension_id,
                        )
                        if (
                            isinstance(stagehand_evidence, dict)
                            and stagehand_evidence.get("status") == "UNAVAILABLE"
                            and re.search(
                                r"\b(create|add|fill|type|select|submit|draw|diagram|connect|drag|book|lead|workflow|automate)\b",
                                objective.casefold(),
                            )
                        ):
                            product = product.model_copy(
                                update={
                                    "blockers": [
                                        *product.blockers,
                                        "behavior_observation_required:stagehand_unavailable",
                                    ]
                                }
                            )
                            artifacts.write_json(
                                "discovery/stagehand-observation.json",
                                stagehand_evidence,
                            )
                            # Do not proceed to candidate selection or
                            # production capture with an interaction plan
                            # that has no observed behavior witness.  A
                            # rendered video of guessed clicks is worse than
                            # an explicit retryable blocker.
                            raise GenerationPreconditionError(
                                "BEHAVIOR_OBSERVATION_REQUIRED: Stagehand observation was unavailable for an interaction objective"
                            )
                        viewport = await self._choose_production_viewport(
                            page, url=url, context=product, objective=objective, artifacts=artifacts
                        )
                    except Exception as error:
                        artifacts.write_json(
                            "discovery/browserbase-connection.json",
                            {
                                "provider": "browserbase",
                                "session_id": session.session_id,
                                "state": "failed",
                                "error_type": type(error).__name__,
                                # Do not retain provider exception text here:
                                # a CDP connection URL may embed a signing
                                # token. Type/state are enough for an
                                # operator to classify the retry boundary.
                            },
                        )
                        raise
                    finally:
                        lease_guard.cancel()
                        with suppress(asyncio.CancelledError):
                            await lease_guard
                        if remote is not None:
                            # CDP can itself be unresponsive when Browserbase
                            # has reclaimed a page. Teardown must not keep a
                            # worker (or paid browser lease) alive after a
                            # discovery/auth timeout.
                            with suppress(Exception):
                                await asyncio.wait_for(remote.close(), timeout=10)
                        await self._release_cloud_session(
                            session.session_id,
                            reason="discovery_complete",
                            run_id=run_id,
                        )
                else:
                    assert browser is not None
                    context = await browser.new_context(viewport={"width": 1440, "height": 900})
                    try:
                        page = await context.new_page()
                        await page.goto(url, wait_until="domcontentloaded")
                        authenticated = await self.credential_service.authenticate_if_required(
                            page, credential_reference
                        )
                        product = await self.discovery.discover(
                            page,
                            objective,
                            budget,
                            known_routes=known_routes,
                            known_actions=known_actions,
                            explore_visible_routes=explore_visible_routes,
                            objective_spec=objective_spec,
                            known_product_fingerprint=known_product_fingerprint,
                            screenshot_directory=None
                            if credential_reference
                            else artifacts.root / "discovery" / "screenshots",
                        )
                        artifacts.write_json(
                            "discovery/adaptive-budget.json",
                            {
                                "requested": budget.model_dump(mode="json"),
                                "effective": (
                                    product.effective_discovery_budget.model_dump(mode="json")
                                    if product.effective_discovery_budget is not None
                                    else budget.model_dump(mode="json")
                                ),
                                "reason": (
                                    "visible primary navigation required expansion"
                                    if product.effective_discovery_budget is not None
                                    and product.effective_discovery_budget.max_pages
                                    != budget.max_pages
                                    else "default bounded budget"
                                ),
                            },
                        )
                        # Use the same semantic observation contract locally
                        # and in Browserbase.  Stagehand remains advisory and
                        # every candidate is re-grounded against this exact
                        # Playwright page, so enabling it cannot bypass
                        # ProductLens safety or execution truth.  The legacy
                        # flag is retained for request compatibility but no
                        # longer creates an intelligence gap between runtime
                        # environments.
                        if self.stagehand_provider is not None:
                            product, stagehand_evidence = await self._stagehand_enrich(
                                page, product, objective, environment="LOCAL"
                            )
                        viewport = await self._choose_production_viewport(
                            page, url=url, context=product, objective=objective, artifacts=artifacts
                        )
                    finally:
                        await context.close()
            finally:
                if browser is not None:
                    await browser.close()
        if authenticated or (credential_reference and product.authentication_state == "unknown"):
            product = product.model_copy(update={"authentication_state": "authenticated"})
        if product.objective is not None and allow_isolated_record_creation:
            product = product.model_copy(
                update={
                    "objective": product.objective.model_copy(
                        update={
                            "safe_action_policy": "authorized_side_effects",
                            "permitted_mutations": ["create_isolated_record"],
                        }
                    )
                }
            )
        if product.authentication_state == "login_required":
            raise GenerationPreconditionError(
                "AUTH_REQUIRED: credentials must be supplied through a secret reference"
            )
        artifacts.write_json("discovery/product-context.json", product.model_dump(mode="json"))
        artifacts.write_json(
            "discovery/product-knowledge.json",
            _product_knowledge_payload(product),
        )
        artifacts.write_json(
            "objective.json",
            product.objective.model_dump(mode="json") if product.objective else {"raw": objective},
        )
        artifacts.write_json(
            "exploration-report.json",
            ExplorationReport(
                pages_inspected=[page.url for page in product.page_knowledge],
                actions_probed=product.exploration_actions,
                blockers=product.blockers,
                rejected_routes=product.rejected_routes,
                candidate_flow_names=[flow.name for flow in product.candidate_demo_flows],
                relationships=getattr(product, "relationships", []),
                stop_reason="bounded evidence sufficient",
            ).model_dump(mode="json"),
        )
        artifacts.write_json(
            "feature-graph.json",
            [feature.model_dump(mode="json") for feature in product.feature_knowledge],
        )
        artifacts.write_json("discovery/relevance-graph.json", _relevance_graph(product))
        artifacts.write_json(
            "candidate-flows.json",
            [flow.model_dump(mode="json") for flow in product.candidate_demo_flows],
        )
        artifacts.write_json("discovery/capabilities.json", product.capabilities)
        for index, page_knowledge in enumerate(product.page_knowledge):
            artifacts.write_json(
                f"page-knowledge/{index:02d}.json", page_knowledge.model_dump(mode="json")
            )
        artifacts.write_json("discovery/viewport-decision.json", viewport.model_dump(mode="json"))
        if stagehand_evidence is not None:
            artifacts.write_json("discovery/stagehand-observation.json", stagehand_evidence)
        return product

    async def _stagehand_enrich(
        self,
        page,
        context,
        objective: str,
        *,
        environment: str = "LOCAL",
        browserbase_session_id: str | None = None,
        browserbase_connect_url: str | None = None,
        browserbase_extension_id: str | None = None,
    ):
        # Stagehand is an advisory observation aid for local runs and a
        # required discovery witness for cloud runs. Its output never becomes
        # workflow evidence until Playwright re-grounds it in this exact page.
        if self.stagehand_provider is None:
            return context, {
                "status": "UNAVAILABLE",
                "reason": "Browserbase-backed Stagehand is not configured",
                "candidate_count": 0,
                "candidates": [],
                "re_grounded_evidence": 0,
            }
        # Browserbase sessions can spend materially longer establishing the
        # Stagehand extension/CDP bridge than a local page.  Keep the outer
        # guard longer than the bridge's 90-second subprocess budget so a
        # valid cloud observation is not discarded at 45 seconds.  Operators
        # may tune this per provider without changing workflow code.
        # The configured value is the cloud-safe ceiling; local observation
        # remains shorter unless the operator explicitly supplies a smaller
        # or larger bounded value through the composition root.
        stagehand_timeout = self.stagehand_observe_timeout_seconds
        if environment != "BROWSERBASE":
            stagehand_timeout = min(stagehand_timeout, 60.0)
        try:
            observation = await asyncio.wait_for(
                self.stagehand_provider.observe(
                    url=page.url,
                    instruction=(
                        "Observe only visible, safe, same-product navigation and primary controls "
                        f"that may help explain this objective: {redact_prompt_text(objective)}. Do not act, submit, or navigate."
                    ),
                    analysis_instruction=(
                        "Extract only labels or phrases visibly present on the current page. "
                        "List meaningful page sections, controls worth inspecting, and safe next "
                        "read-only actions. Do not infer hidden behavior, submit forms, or navigate."
                    ),
                    cache_dir=Path(".stagehand-cache"),
                    environment=environment,
                    browserbase_session_id=browserbase_session_id,
                    browserbase_connect_url=browserbase_connect_url,
                    browserbase_extension_id=browserbase_extension_id,
                ),
                timeout=stagehand_timeout,
            )
        except TimeoutError:
            return context, {
                "status": "UNAVAILABLE",
                "reason": "Stagehand observation exceeded the bounded discovery timeout",
                "candidate_count": 0,
                "candidates": [],
                "re_grounded_evidence": 0,
            }
        except ProviderError as error:
            return context, {
                "status": "UNAVAILABLE",
                "reason": str(error),
                "candidate_count": 0,
                "candidates": [],
                "re_grounded_evidence": 0,
            }
        enriched = await self.discovery.enrich_with_stagehand(page, context, observation)
        # A Stagehand observation gives us affordances; a bounded rehearsal
        # gives the planner evidence about how a real UI responds to one
        # read-only exploration instruction.  It is deliberately performed
        # before production in the exploration context and its action log is
        # advisory only.  ProductLens still re-observes the live Playwright
        # page and validates every production operation itself.
        rehearsal_report: dict[str, object] = {"status": "not_requested"}
        try:
            rehearsal = await asyncio.wait_for(
                self.stagehand_provider.rehearse_agent(
                    url=page.url,
                    instruction=(
                        "Explore this product only for understanding. Inspect the visible page and "
                        "one or two relevant same-origin controls for the objective below. Do not "
                        "submit, create, delete, send, publish, purchase, upload, or change account "
                        f"data. Stop after observing the resulting state. Objective: {redact_prompt_text(objective)}"
                    ),
                    environment=environment,
                    browserbase_session_id=browserbase_session_id,
                    browserbase_connect_url=browserbase_connect_url,
                    browserbase_extension_id=browserbase_extension_id,
                    cache_dir=Path(".stagehand-cache"),
                    max_steps=8,
                    agent_mode="hybrid",
                ),
                timeout=min(stagehand_timeout, 95.0),
            )
            rehearsal_report = {
                "status": "REHEARSED",
                "success": rehearsal.success,
                "message": rehearsal.message,
                "observed_url": rehearsal.observed_url,
                "environment": rehearsal.environment,
                "actions": rehearsal.actions[:24],
            }
        except TimeoutError:
            rehearsal_report = {"status": "UNAVAILABLE", "reason": "rehearsal_timeout"}
        except (ProviderError, RuntimeError, OSError) as error:
            rehearsal_report = {
                "status": "UNAVAILABLE",
                "reason": str(error)[:500],
            }
        enriched, safe_probe = await self._stagehand_safe_probe(
            page,
            enriched,
            observation,
            objective,
            environment=environment,
            browserbase_session_id=browserbase_session_id,
            browserbase_connect_url=browserbase_connect_url,
            browserbase_extension_id=browserbase_extension_id,
        )
        return enriched, {
            "status": "OBSERVED",
            "environment": observation.environment,
            "observed_url": observation.observed_url,
            # The Stagehand browser is intentionally independent of the
            # ProductLens discovery browser; this ID is correlation only.
            "correlated_browserbase_session_id": browserbase_session_id,
            "candidate_count": len(observation.candidates),
            "analysis_present": observation.analysis is not None,
            "analysis_error": observation.analysis_error,
            "analysis_candidate_counts": {
                "visible_sections": len(observation.analysis.visible_sections)
                if observation.analysis
                else 0,
                "meaningful_controls": len(observation.analysis.meaningful_controls)
                if observation.analysis
                else 0,
                "safe_next_actions": len(observation.analysis.safe_next_actions)
                if observation.analysis
                else 0,
            },
            "candidates": [
                {"selector": item.selector, "description": item.description, "method": item.method}
                for item in observation.candidates
            ],
            "metrics": observation.metrics,
            "re_grounded_evidence": len(enriched.elements) - len(context.elements),
            "safe_probe": safe_probe,
            "rehearsal": rehearsal_report,
        }

    async def _stagehand_safe_probe(
        self,
        page,
        context: ProductContext,
        observation,
        objective: str,
        *,
        environment: str,
        browserbase_session_id: str | None,
        browserbase_connect_url: str | None,
        browserbase_extension_id: str | None,
    ) -> tuple[ProductContext, dict[str, object]]:
        """Use one observed Stagehand action only when it is demonstrably harmless.

        Stagehand's ``act`` primitive is useful for complex semantic controls,
        but a generic demo system must never give it an open-ended task.  This
        method allows exactly one visible same-origin navigation/tab/disclosure
        probe during cloud discovery. ProductLens validates the candidate both
        before and after dispatch and retains the resulting DOM as evidence.
        """
        if environment != "BROWSERBASE" or self.stagehand_provider is None:
            return context, {"status": "not_applicable"}
        dangerous = re.compile(
            r"\b(delete|remove|archive|send|email|message|pay|charge|publish|invite|create|add|save|submit|confirm)\b",
            re.IGNORECASE,
        )
        candidate = None
        for item in observation.candidates:
            if item.method.casefold() != "click" or dangerous.search(item.description):
                continue
            try:
                locator = page.locator(item.selector)
                if await locator.count() != 1 or not await locator.is_visible():
                    continue
                semantics = await locator.evaluate(
                    """node => ({
                        href: node.getAttribute('href'), role: node.getAttribute('role'),
                        ariaControls: node.getAttribute('aria-controls'), tag: node.tagName.toLowerCase(),
                        type: node.getAttribute('type')
                    })"""
                )
            except PlaywrightError:
                continue
            href = str(semantics.get("href") or "")
            same_origin_link = (
                bool(href) and urlsplit(urljoin(page.url, href)).netloc == urlsplit(page.url).netloc
            )
            is_disclosure = (
                bool(semantics.get("ariaControls"))
                or semantics.get("role") == "tab"
                or semantics.get("tag") == "summary"
            )
            # Do not use generic buttons: their side effect cannot be inferred
            # safely across arbitrary products. Links, tabs and disclosures
            # have a bounded read-only navigation meaning.
            if same_origin_link or is_disclosure:
                candidate = item
                break
        if candidate is None:
            return context, {"status": "no_safe_candidate"}
        before_url = page.url
        try:
            before_text = " ".join((await page.locator("body").inner_text()).split())[:8_000]
            result = await asyncio.wait_for(
                self.stagehand_provider.act_observed(
                    url=before_url,
                    candidate=candidate,
                    environment=environment,
                    browserbase_session_id=browserbase_session_id,
                    browserbase_connect_url=browserbase_connect_url,
                    browserbase_extension_id=browserbase_extension_id,
                    cache_dir=Path(".stagehand-cache"),
                ),
                timeout=30,
            )
            await page.wait_for_timeout(650)
            after_text = " ".join((await page.locator("body").inner_text()).split())[:8_000]
        except ProviderError as error:
            return context, {"status": "provider_unavailable", "error_type": type(error).__name__}
        except TimeoutError:
            return context, {"status": "provider_timeout", "error_type": "TimeoutError"}
        except PlaywrightError as error:
            return context, {
                "status": "post_action_unavailable",
                "error_type": type(error).__name__,
            }
        if not result.success or (page.url == before_url and after_text == before_text):
            return context, {
                "status": "no_verified_state_change",
                "candidate": candidate.description,
                "stagehand_success": result.success,
            }
        inspected = await self.discovery.inspect(page, objective)
        inspected_page = _page_knowledge(inspected)
        pages = [
            item
            for item in context.page_knowledge
            if _canonical_url(item.url) != _canonical_url(inspected_page.url)
        ]
        pages.append(inspected_page)
        existing = {(item.source_url, item.selector, item.name) for item in context.elements}
        additions = [
            item
            for item in inspected.elements
            if (item.source_url, item.selector, item.name) not in existing
        ]
        enriched = context.model_copy(
            update={
                "elements": [*context.elements, *additions][:360],
                "page_knowledge": pages,
                "relevant_routes": list(
                    dict.fromkeys([*context.relevant_routes, *inspected.relevant_routes])
                )[:30],
                "successful_action_hints": [
                    *context.successful_action_hints,
                    {
                        "source": "stagehand_safe_probe",
                        "target": {"selector": candidate.selector, "name": candidate.description},
                        "method": candidate.method,
                        "before_url": before_url,
                        "after_url": page.url,
                        "verified": True,
                    },
                ][:30],
                "evidence": [
                    *context.evidence,
                    f"stagehand safe probe verified:{candidate.description}",
                    f"stagehand safe probe page:{inspected_page.url}",
                ],
            }
        )
        return enriched, {
            "status": "verified",
            "candidate": candidate.description,
            "before_url": before_url,
            "after_url": page.url,
            "result": result.action,
        }

