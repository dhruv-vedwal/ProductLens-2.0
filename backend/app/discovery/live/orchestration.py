"""Discovery orchestration and Stagehand enrichment."""

from __future__ import annotations

import asyncio
import hashlib
import re
from contextlib import suppress
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.contracts.models import (
    ActionCapability,
    CandidateDemoFlow,
    DiscoveryBudget,
    FeatureKnowledge,
    FormField,
    FormSchema,
    ObjectiveRelationship,
    ObjectiveSpec,
    ObservedElement,
    PageKnowledge,
    ProductContext,
    ProductRelationship,
    Target,
)
from app.planning.capability_resolution import resolve_capabilities

from app.discovery.live.helpers import *  # noqa: F403

class DiscoveryOrchestrationMixin:
    async def discover(
        self,
        page,
        objective: str,
        budget: DiscoveryBudget | None = None,
        known_routes: list[str] | None = None,
        known_actions: list[dict] | None = None,
        explore_visible_routes: bool = False,
        screenshot_directory: Path | None = None,
        objective_spec: ObjectiveSpec | None = None,
        known_product_fingerprint: str | None = None,
    ) -> ProductContext:
        """Explore a bounded set of objective-ranked, same-origin routes and restore the entry URL."""
        budget = budget or DiscoveryBudget()
        entry_url = page.url
        discovery_started = asyncio.get_running_loop().time()

        async def revive_page_if_closed() -> bool:
            """Reopen the authenticated entry state after a transient probe closure.

            Remote CDP browsers can close a page when a reversible control opens
            a portal/new target or when an extension probe tears down its target.
            Discovery evidence collected before that event is still valid; the
            route collector only needs a live page to continue.  Reusing the
            existing browser context preserves cookies without replaying a
            production navigation (this helper is exploration-only).
            """
            nonlocal page
            try:
                if not page.is_closed():
                    return True
            except PlaywrightError:
                pass
            try:
                browser_context = page.context
                page = await browser_context.new_page()
                await page.goto(entry_url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(1_100)
                return not page.is_closed()
            except PlaywrightError:
                return False

        async def reopen_unresponsive_page(url: str) -> bool:
            """Replace a live-but-unresponsive remote target without losing auth.

            CDP can leave ``page.is_closed()`` false while a renderer target
            stops answering (a common failure after a long SPA transition).
            Reusing the existing context preserves authenticated storage and
            avoids replaying credentials; the old target is closed only on a
            best-effort basis.
            """
            nonlocal page
            try:
                browser_context = page.context
                replacement = await asyncio.wait_for(browser_context.new_page(), timeout=10)
                await asyncio.wait_for(
                    replacement.goto(url, wait_until="domcontentloaded", timeout=20_000),
                    timeout=25,
                )
                await replacement.wait_for_timeout(700)
                old_page = page
                page = replacement
                with suppress(Exception):
                    await asyncio.wait_for(old_page.close(), timeout=5)
                return not page.is_closed()
            except (PlaywrightError, TimeoutError):
                return False

        async def inspect_with_scroll_evidence(recovery_url: str | None = None) -> ProductContext:
            """Collect bounded lower-page evidence without turning discovery into capture.

            Animated timelines and card collections often hydrate their detailed
            content only after it enters the viewport.  Discovery therefore
            samples two semantic scroll regions, merges the observed evidence,
            and restores the opening position before planning/recording.
            """
            # Route transitions in animated SPAs may report network-idle before
            # their timeline/card components have committed.  A bounded settle
            # window makes visible contribution text discoverable without
            # coupling discovery to arbitrary long sleeps.
            await page.wait_for_timeout(1_100)
            try:
                initial = await asyncio.wait_for(self.inspect(page, objective, budget), timeout=30)
            except TimeoutError:
                # A remote renderer may remain nominally open but stop
                # answering DOM calls. Recreate only the exploration target,
                # then retry once against the same route; production remains
                # isolated and never inherits this recovery.
                target_url = recovery_url or entry_url
                if not await reopen_unresponsive_page(target_url):
                    raise
                initial = await asyncio.wait_for(self.inspect(page, objective, budget), timeout=30)
            if screenshot_directory is not None:
                screenshot_directory.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256(_canonical_route(page.url).encode("utf-8")).hexdigest()[:16]
                screenshot = screenshot_directory / f"{digest}.png"
                await page.screenshot(path=str(screenshot), full_page=False)
                initial = initial.model_copy(
                    update={
                        "evidence": [
                            *initial.evidence,
                            f"screenshot:discovery/screenshots/{screenshot.name}",
                        ]
                    }
                )
            samples = [initial]
            try:
                metrics = await page.evaluate(
                    "() => ({height: document.documentElement.scrollHeight, viewport: window.innerHeight})"
                )
                height, viewport = float(metrics["height"]), float(metrics["viewport"])
                if height > viewport * 1.35:
                    for fraction in (0.45, 0.9):
                        await page.evaluate(
                            "y => window.scrollTo({top: y, behavior: 'instant'})",
                            max(0, height * fraction - viewport * 0.4),
                        )
                        await page.wait_for_timeout(1_000)
                        samples.append(
                            await asyncio.wait_for(
                                self.inspect(page, objective, budget), timeout=30
                            )
                        )
            finally:
                await page.evaluate("() => window.scrollTo({top: 0, behavior: 'instant'})")
            elements: list[ObservedElement] = []
            seen_elements: set[tuple[str | None, str, str]] = set()
            for sample in samples:
                for item in sample.elements:
                    key = (item.source_url, item.selector, item.name)
                    if key not in seen_elements:
                        seen_elements.add(key)
                        elements.append(item)
            blocks = list(
                dict.fromkeys(block for sample in samples for block in sample.content_blocks)
            )
            return initial.model_copy(
                update={
                    "elements": elements,
                    "content_blocks": blocks,
                    "visible_text": max((sample.visible_text for sample in samples), key=len),
                    "evidence": [*initial.evidence, "bounded scroll-state evidence"],
                }
            )

        primary = await inspect_with_scroll_evidence()
        objective_spec = objective_spec or _objective_spec(objective)
        primary_route_count = len(
            {
                _canonical_route(urljoin(entry_url, item.href))
                for item in primary.navigation
                if item.href
                and urlparse(urljoin(entry_url, item.href)).netloc
                in {"", urlparse(entry_url).netloc}
                and _canonical_route(urljoin(entry_url, item.href)) != _canonical_route(entry_url)
            }
        )
        original_budget = budget
        budget = adaptive_exploration_budget(
            budget, objective_spec, primary_route_count=primary_route_count
        )
        # A fresh timestamp alone does not prove the product is unchanged.
        # Reuse cached routes/actions only when the current opening page has
        # the same content fingerprint; otherwise the live DOM remains the
        # sole source of route relevance and the cache is ignored for this
        # exploration.
        current_fingerprint = _page_knowledge(primary).fingerprint
        cache_matches = bool(
            known_product_fingerprint and known_product_fingerprint == current_fingerprint
        )
        (
            capabilities,
            capability_actions,
            capability_blockers,
        ) = await self._probe_reversible_capabilities(
            page, primary, objective, remaining=max(0, min(3, budget.max_actions // 6))
        )
        # A probe must never make the rest of discovery unusable. If a remote
        # target disappeared, reopen the same authenticated entry state and
        # refresh its page-local evidence before traversing visible routes.
        if await revive_page_if_closed() and any(
            "Target page" in item for item in capability_blockers
        ):
            primary = await inspect_with_scroll_evidence()
        if explore_visible_routes:
            origin = urlparse(entry_url)
            visible_routes: list[str] = []
            for item in primary.navigation:
                absolute = urljoin(entry_url, item.href or "")
                parsed = urlparse(absolute)
                if parsed.scheme in {"http", "https", "file"} and parsed.netloc in {
                    "",
                    origin.netloc,
                }:
                    visible_routes.append(absolute)
            # A thorough walkthrough must give every safe primary tab a chance
            # to contribute page-local knowledge.  Keep the visible navigation
            # controls ahead of deep cards such as Week 1, while retaining the
            # objective-ranked candidates afterwards for feature requests.
            primary_controls = [route for route in visible_routes if _route_depth(route) <= 1]
            ordered_routes = (
                [
                    *primary_controls,
                    *primary.relevant_routes,
                    *visible_routes,
                ]
                if objective_spec.demo_type == "full_walkthrough"
                else [
                    # For a focused request, visible navigation is the strongest
                    # discovery signal.  Rank it by semantic overlap before
                    # cached/model-ranked routes; otherwise a bounded page budget
                    # can be consumed by generic dashboard links before the
                    # requested module (for example a feature route that is
                    # visible in the sidebar).  This remains product-neutral: the
                    # score uses the objective words, label, and route path only.
                    *sorted(
                        visible_routes,
                        key=lambda route: (
                            -_route_objective_score(route, primary.navigation, objective_spec)
                        ),
                    ),
                    *primary.relevant_routes,
                ]
            )
            deduped_routes: list[str] = []
            seen_routes: set[str] = set()
            for route in ordered_routes:
                canonical = _canonical_route(route)
                if canonical not in seen_routes:
                    seen_routes.add(canonical)
                    deduped_routes.append(route)
            primary = primary.model_copy(
                # Preserve objective-ranked routes first. Replacing them with
                # DOM navigation order made a small exploration budget inspect
                # generic dashboard links before the route relevant to the
                # requested workflow.
                update={"relevant_routes": deduped_routes}
            )
        same_origin_known = [
            route
            for route in (known_routes or [])
            if cache_matches
            if urlparse(route).netloc in {"", urlparse(entry_url).netloc}
        ]
        # Fresh visible primary controls outrank cached/deep routes for a full
        # walkthrough.  Cached knowledge still participates, but must not turn
        # a whole-product tour into a sweep of repeated detail URLs.
        # Fresh evidence must lead for focused requests as well as full tours.
        # Cached knowledge is useful as a fallback, but putting it first lets
        # stale/generic routes consume the bounded page budget before a newly
        # visible objective route (for example ``/bookings``) is inspected.
        # This ordering is deliberately product-neutral and is later
        # canonicalized/deduplicated below.
        route_sources = [*primary.relevant_routes, *same_origin_known]
        deduped_route_sources: list[str] = []
        seen_route_sources: set[str] = set()
        for route in route_sources:
            canonical = _canonical_route(route)
            if canonical not in seen_route_sources:
                seen_route_sources.add(canonical)
                deduped_route_sources.append(route)
        primary = primary.model_copy(
            update={
                "relevant_routes": deduped_route_sources,
                "evidence": [*primary.evidence, "fresh knowledge routes re-grounded"],
            }
        )
        collected = [primary]
        navigation_probes: list[str] = []
        rejected_routes: list[str] = []
        try:
            entry_canonical = _canonical_route(entry_url)
            # The collection loop has its own full-walkthrough ordering rather
            # than relying on a route list which may include deep links from a
            # cached knowledge record.  This guarantees that top-level visible
            # controls (Weeks, DSA, Builds, Progress) are inspected before any
            # repeated week/card routes.
            routes_to_inspect = list(primary.relevant_routes)
            if objective_spec.demo_type == "full_walkthrough":
                primary_routes = [
                    urljoin(entry_url, item.href or "")
                    for item in primary.navigation
                    if item.href
                    and urlparse(urljoin(entry_url, item.href)).netloc
                    in {"", urlparse(entry_url).netloc}
                    and _route_depth(urljoin(entry_url, item.href)) <= 1
                ]
                routes_to_inspect = [*primary_routes, *routes_to_inspect]
            seen_inspection_routes: set[str] = set()
            visible_controls_by_route: dict[str, ObservedElement] = {}
            for item in primary.navigation:
                if not item.href or not item.actionable:
                    continue
                destination = urljoin(entry_url, item.href)
                if urlparse(destination).netloc not in {"", urlparse(entry_url).netloc}:
                    continue
                visible_controls_by_route.setdefault(_canonical_route(destination), item)
            for route_index, route in enumerate(routes_to_inspect):
                if asyncio.get_running_loop().time() - discovery_started >= budget.max_time_seconds:
                    rejected_routes.append("discovery_time_budget_exhausted")
                    break
                if not await revive_page_if_closed():
                    rejected_routes.append(f"page_unavailable:{route}")
                    continue
                # The opening page is already represented by ``primary``.
                # Skipping its canonical equivalents preserves the bounded
                # budget for actual primary sections.
                canonical = _canonical_route(route)
                if canonical == entry_canonical or canonical in seen_inspection_routes:
                    continue
                seen_inspection_routes.add(canonical)
                if len(collected) >= budget.max_pages:
                    break
                control = visible_controls_by_route.get(canonical)
                used_visible_control = False
                if control is not None:
                    try:
                        # Responsive layouts legitimately duplicate a semantic
                        # navigation label.  A unique accessible name is not a
                        # prerequisite for visible navigation: choose the first
                        # *visible* same-destination anchor in DOM order, which
                        # remains a real product control rather than falling
                        # back to a direct URL merely because mobile markup is
                        # also present in the accessibility tree.
                        # Resolve route candidates in the page in one bounded
                        # operation. Per-anchor CDP ``get_attribute`` calls
                        # can starve on virtualized/re-rendering navigation
                        # lists; only the small matching index set is brought
                        # back to Playwright for the actual semantic click.
                        try:
                            anchor_inventory = await page.evaluate(
                                """() => Array.from(document.querySelectorAll('a')).slice(0, 160).map((node, index) => ({
                                    index,
                                    href: node.getAttribute('href'),
                                    visible: !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length)
                                }))"""
                            )
                        except (PlaywrightError, TimeoutError):
                            anchor_inventory = []
                        anchors = page.locator("a")
                        for item in anchor_inventory:
                            if (
                                not isinstance(item, dict)
                                or not item.get("href")
                                or not item.get("visible")
                            ):
                                continue
                            href = str(item["href"])
                            if _canonical_route(urljoin(page.url, href)) != canonical:
                                continue
                            candidate = anchors.nth(int(item["index"]))
                            try:
                                await candidate.click(timeout=2_500)
                            except PlaywrightError:
                                # Cloud browsers occasionally report a
                                # transient animation overlay over a genuine
                                # visible navigation control. This is still a
                                # semantic element click (not coordinates or
                                # direct navigation); force is limited to
                                # read-only same-origin anchor probes.
                                await candidate.click(timeout=2_500, force=True)
                            await page.wait_for_timeout(650)
                            used_visible_control = _canonical_route(page.url) == canonical
                            if used_visible_control:
                                break
                    except PlaywrightError:
                        used_visible_control = False
                if used_visible_control:
                    navigation_probes.append(f"visible_navigation:{control.name}")
                else:
                    # Navigation is direct only when no visible, uniquely
                    # resolvable same-origin link is available at this state.
                    try:
                        # A route probe is bounded discovery evidence, not a
                        # production navigation. Keep an unresponsive SPA
                        # route from consuming the entire adaptive budget.
                        await page.goto(route, wait_until="domcontentloaded", timeout=15_000)
                    except PlaywrightError:
                        rejected_routes.append(f"navigation_failed:{route}")
                        continue
                    navigation_probes.append(f"direct_navigation_fallback:{route}")
                inspected = await inspect_with_scroll_evidence(recovery_url=route)
                remaining_capabilities = max(0, min(3, budget.max_actions // 6) - len(capabilities))
                if remaining_capabilities:
                    found, actions, blockers = await self._probe_reversible_capabilities(
                        page, inspected, objective, remaining=remaining_capabilities
                    )
                    capabilities.extend(found)
                    capability_actions.extend(actions)
                    capability_blockers.extend(blockers)
                collected.append(inspected)
                for item in inspected.navigation:
                    if not item.href or not item.actionable:
                        continue
                    destination = urljoin(inspected.url, item.href)
                    if urlparse(destination).netloc not in {"", urlparse(inspected.url).netloc}:
                        continue
                    visible_controls_by_route.setdefault(_canonical_route(destination), item)
                # Relationships can be represented by an in-page button
                # rather than a route (common in SPA Settings dashboards).
                # Probe only an explicitly requested, non-mutating semantic
                # control and retain its independently observed state as page
                # knowledge. This is discovery evidence, never a production
                # shortcut or a data-changing action.
                for relationship_control in _relationship_child_controls(inspected, objective_spec):
                    if (
                        asyncio.get_running_loop().time() - discovery_started
                        >= budget.max_time_seconds
                    ):
                        rejected_routes.append("discovery_time_budget_exhausted")
                        break
                    if len(collected) >= budget.max_pages:
                        break
                    try:
                        visible = await self._visible_semantic_control(page, relationship_control)
                        if visible is None:
                            continue
                        before_text = (await page.locator("body").inner_text())[:8_000]
                        await page.wait_for_timeout(500)
                        try:
                            await visible.click(timeout=5_000)
                        except PlaywrightTimeoutError:
                            await visible.click(timeout=3_000, force=True)
                        await page.wait_for_timeout(650)
                        revealed = await inspect_with_scroll_evidence()
                        changed = revealed.visible_text != before_text
                        if not changed:
                            capability_blockers.append(
                                f"relationship_probe_no_visible_state_change:{relationship_control.name}"
                            )
                            continue
                        revealed = revealed.model_copy(
                            update={
                                "evidence": [
                                    *revealed.evidence,
                                    f"visible_relationship_control:{relationship_control.name}",
                                    f"relationship_context:{relationship_control.source_url or inspected.url}",
                                ]
                            }
                        )
                        collected.append(revealed)
                        navigation_probes.append(
                            f"visible_relationship_control:{relationship_control.name}"
                        )
                    except PlaywrightError as error:
                        capability_blockers.append(
                            f"relationship_probe_failed:{relationship_control.name}:{str(error)[:120]}"
                        )
                    finally:
                        # Restore a clean parent state before the next bounded
                        # discovery probe. Escape is semantic and harmless;
                        # the page reload is only a read-only fallback when a
                        # component does not expose a dismissible state.
                        await self._safe_escape(page)
                # A relationship context often lives one visible navigation
                # level below its generic entry surface (for example, an
                # observed Settings item followed by a named configuration).
                # Expand only those explicitly relevant child controls and do
                # it immediately while the verified visible parent remains
                # active, so discovery prefers semantic navigation over a
                # direct URL fallback.
                supporting = _relationship_supporting_routes(inspected, objective_spec)
                queued = {
                    _canonical_route(candidate)
                    for candidate in routes_to_inspect[route_index + 1 :]
                }
                additions = [
                    candidate
                    for candidate in supporting
                    if _canonical_route(candidate) not in seen_inspection_routes
                    and _canonical_route(candidate) not in queued
                ]
                if additions:
                    if objective_spec.demo_type == "full_walkthrough":
                        # A complete walkthrough must establish every visible
                        # primary section before spending the bounded page
                        # budget on deep cards/articles. Inserting a child
                        # route immediately after its parent used to evict a
                        # later top-level tab (for example Contact) and made
                        # planning fail despite that tab being visible.
                        routes_to_inspect.extend(additions)
                    else:
                        routes_to_inspect[route_index + 1 : route_index + 1] = additions
                # An explicit configuration/dependency request is complete
                # once the operational feature and a page-local supporting
                # detail state have both been grounded. Continuing through a
                # bounded route list after that point is crawler behavior and
                # only weakens the final candidate flow with unrelated pages.
                if _focused_relationship_evidence_complete(collected, objective_spec):
                    break
        finally:
            pass
        # Discovery owns evidence, not a browser state for later capture.
        # Production always uses a clean context, and viewport selection can
        # explicitly revisit the opening URL if it needs to. Replaying the
        # entry navigation here caused duplicate loads and could discard fully
        # collected knowledge when a provider lease expired during cleanup.
        # The same generic selector (for example ``h1`` or ``a``) is valid on
        # several routes.  De-duplicating without page provenance silently
        # removes later-page landmarks and makes a planner open a tab with no
        # way to explore it.  Preserve each page-local observation.
        seen: set[tuple[str | None, str, str]] = set()
        elements: list[ObservedElement] = []
        for context in collected:
            for item in context.elements:
                key = (item.source_url, item.selector, item.name)
                if key not in seen:
                    seen.add(key)
                    elements.append(item)
        observed_targets = {(item.selector, item.name.lower()) for item in elements}
        action_hints = []
        for hint in known_actions or []:
            target = hint.get("target") or {}
            selector, name = target.get("selector"), str(target.get("name", "")).lower()
            if (selector, name) in observed_targets or any(
                selector and selector == observed_selector or name and name == observed_name
                for observed_selector, observed_name in observed_targets
            ):
                action_hints.append(hint)
        pages = [_page_knowledge(context) for context in collected]
        elements = _restore_missing_page_landmarks(elements, pages)
        relationships = _derive_product_relationships(pages, objective_spec)
        objective_words = _tokens(objective)
        features = []
        for page_info in pages:
            terms = _tokens(
                " ".join(
                    [
                        page_info.title,
                        page_info.purpose,
                        *page_info.visible_sections,
                        *page_info.visible_facts[:6],
                    ]
                )
            )
            score = min(
                1.0,
                0.25
                + 0.15 * len(terms & objective_words)
                + (0.35 if objective_spec.demo_type == "full_walkthrough" else 0),
            )
            # Semantic relevance is still grounded in the observed page terms;
            # these synonym bridges prevent an "invite teammate" objective
            # from ranking a generic settings page above an observed Users
            # page merely because neither label repeats the request verbatim.
            if {"invite", "teammate", "member", "team"} & objective_words and {
                "user",
                "users",
                "member",
                "members",
            } & terms:
                score = max(score, 0.82)
            if {"report", "export", "csv"} & objective_words and {
                "report",
                "reports",
                "analytics",
            } & terms:
                score = max(score, 0.82)
            related_urls = list(
                dict.fromkeys(
                    url
                    for relationship in relationships
                    for url in (relationship.source_url, relationship.target_url)
                    if url and _canonical_route(url) != _canonical_route(page_info.url)
                )
            )
            features.append(
                FeatureKnowledge(
                    name=page_info.purpose,
                    purpose=page_info.purpose,
                    entry_urls=[page_info.url],
                    related_urls=related_urls[:12],
                    evidence=[
                        f"page:{page_info.url}",
                        *[f"section:{section}" for section in page_info.visible_sections[:4]],
                    ],
                    relevance_score=score,
                )
            )
        ranked_pages = sorted(
            pages,
            key=lambda page_info: next(
                (
                    feature.relevance_score
                    for feature in features
                    if feature.name == page_info.purpose
                ),
                0,
            ),
            reverse=True,
        )
        operational_pages, supporting_pages = _relationship_page_roles(pages, objective_spec)
        if objective_spec.demo_type == "full_walkthrough":
            flow_page_infos = pages
        elif objective_spec.supporting_relationships and operational_pages:
            # The source configuration is evidence gathered during discovery;
            # the operational feature is what a viewer asked to see.  Never
            # turn this into a Settings/configuration route tour merely
            # because lexical relevance happens to tie.
            flow_page_infos = operational_pages
        else:
            flow_page_infos = ranked_pages[: min(3, len(ranked_pages))]
        flow_pages = [page_info.url for page_info in flow_page_infos]
        flow = CandidateDemoFlow(
            name="evidence-backed walkthrough",
            page_urls=flow_pages,
            supporting_page_urls=[page_info.url for page_info in supporting_pages],
            rationale=[
                "objective-relevant operational page knowledge",
                "relationship context grounded during exploration",
                "visible sections and controls inspected",
            ]
            if supporting_pages
            else ["objective-relevant page knowledge", "visible sections and controls inspected"],
            expected_outcomes=[page_info.purpose for page_info in flow_page_infos[:6]],
            risks=["external links and side effects excluded"],
            estimated_duration_seconds=objective_spec.target_duration_seconds,
            evidence_coverage=[
                evidence for page_info in ranked_pages for evidence in page_info.evidence_refs[:3]
            ],
            score=0.85 if pages else 0.0,
        )
        candidates = [flow]
        # Retain alternatives as explicit, scored evidence. Planning can then
        # reject a merely navigable page instead of silently broadening a
        # narrow request into a route sweep.
        if objective_spec.demo_type != "full_walkthrough":
            # Relationship support is deliberately not emitted as an isolated
            # production candidate. It may be selected only alongside its
            # operational story surface through ``supporting_page_urls``.
            focused_pages = (
                flow_page_infos
                if objective_spec.supporting_relationships
                else ranked_pages[: min(4, len(ranked_pages))]
            )
            for page_info in focused_pages:
                # The opening page already provides context.  A duplicate
                # "focused" candidate contains no feature outcome and can win
                # by being artificially short, which would make scope
                # validation reject the genuinely relevant observed page.
                if _canonical_route(page_info.url) == _canonical_route(primary.url):
                    continue
                feature = next((item for item in features if item.name == page_info.purpose), None)
                relevance = feature.relevance_score if feature else 0.25
                candidates.append(
                    CandidateDemoFlow(
                        name=f"focused: {page_info.purpose}",
                        page_urls=[primary.url, page_info.url],
                        supporting_page_urls=[item.url for item in supporting_pages],
                        rationale=[
                            "direct operational relevance",
                            "supporting context verified during discovery",
                        ],
                        expected_outcomes=[page_info.purpose],
                        risks=["unrelated primary pages excluded"],
                        estimated_duration_seconds=90,
                        evidence_coverage=page_info.evidence_refs[:8],
                        score=round(relevance, 3),
                    )
                )
        # Preserve a bounded, objective-ranked inventory of every visible
        # same-origin navigation control.  The page collection above may stop
        # after ``max_pages``; losing a visible objective route from
        # ``relevant_routes`` would make the planner fall back to stale cached
        # knowledge (and, for example, miss a Booking page even though the
        # sidebar exposed it).  This is route evidence only; the collection
        # budget still controls which pages are actually inspected.
        visible_inventory: list[str] = []
        entry_origin = urlparse(entry_url)
        for item in primary.navigation:
            if not item.href:
                continue
            candidate = urljoin(entry_url, item.href)
            parsed = urlparse(candidate)
            if parsed.scheme not in {"http", "https", "file"} or parsed.netloc not in {
                "",
                entry_origin.netloc,
            }:
                continue
            visible_inventory.append(candidate)
        objective_words = _tokens(objective)
        if objective_spec.demo_type != "full_walkthrough":
            visible_inventory.sort(
                key=lambda route: -_route_objective_score(route, primary.navigation, objective_spec)
            )
        route_inventory = list(dict.fromkeys(visible_inventory))
        observed_routes = list(
            dict.fromkeys(route for context in collected for route in context.relevant_routes)
        )
        discovered = primary.model_copy(
            update={
                # Keep enough evidence for every inspected page.  A single
                # dense DSA table can legitimately contain more than 120
                # controls; a global first-N truncation used to erase Builds
                # and Progress entirely after they had been successfully
                # explored.  The bounded route/page budget keeps this payload
                # finite, while planner prompts apply their own compact view.
                "elements": elements[:360],
                # Preserve a bounded quota per inspected source page.  A
                # global first-N slice lets a dense page's footer hide the
                # visible control needed to reach a later primary section,
                # forcing planning to use an avoidable direct URL fallback.
                "navigation": _bounded_page_navigation(elements),
                "relevant_routes": list(dict.fromkeys([*route_inventory, *observed_routes]))[
                    : budget.max_pages
                ],
                "evidence": [
                    *primary.evidence,
                    f"bounded routes inspected: {len(collected)}",
                    *(
                        [
                            f"adaptive full-walkthrough budget: {original_budget.max_pages}->{budget.max_pages}"
                        ]
                        if budget.max_pages != original_budget.max_pages
                        else []
                    ),
                ],
                "successful_action_hints": action_hints[:30],
                "confidence": min(1.0, primary.confidence + 0.05 * (len(collected) - 1)),
                "objective": objective_spec,
                "page_knowledge": pages,
                "feature_knowledge": features,
                "relationships": relationships,
                "candidate_demo_flows": sorted(
                    candidates, key=lambda candidate: candidate.score, reverse=True
                ),
                "capabilities": [capability.model_dump(mode="json") for capability in capabilities],
                "exploration_actions": [*navigation_probes, *capability_actions],
                "blockers": [*primary.blockers, *capability_blockers],
                "rejected_routes": rejected_routes,
                "effective_discovery_budget": budget,
            }
        )
        # Resolve generic interaction capabilities from the complete evidence
        # snapshot before handing discovery to planning.  This is descriptive
        # only; production still re-grounds and verifies every action.
        resolution = resolve_capabilities(objective, discovered)
        return discovered.model_copy(update={"capability_resolutions": [resolution]})

    async def enrich_with_stagehand(
        self, page, context: ProductContext, observation
    ) -> ProductContext:
        """Re-ground optional Stagehand suggestions against this Playwright page.

        A Stagehand response is never trusted as execution evidence on its own.
        Invalid, hidden, or ambiguous selectors are discarded before planning.
        """
        additions: list[ObservedElement] = []
        known = {item.selector for item in context.elements}
        for candidate in observation.candidates:
            if not candidate.selector or candidate.selector in known:
                continue
            try:
                locator = page.locator(candidate.selector)
                if await locator.count() != 1 or not await locator.is_visible():
                    continue
                item = await locator.evaluate(
                    """node => ({tag:node.tagName.toLowerCase(), role:node.getAttribute('role'),
                    name:node.getAttribute('aria-label') || node.getAttribute('name') ||
                    node.getAttribute('placeholder') || node.innerText || '', href:node.getAttribute('href'),
                    type:node.getAttribute('type'), required:node.required === true,
                    autocomplete:node.getAttribute('autocomplete'),
                    options:node.tagName.toLowerCase() === 'select' ? Array.from(node.options).map(option => option.value || option.text).filter(Boolean).slice(0, 40) : [], text:node.innerText || null})"""
                )
            except PlaywrightError:
                continue
            name = str(item.get("name") or candidate.description).strip()
            if not name:
                continue
            additions.append(
                ObservedElement(
                    tag=item["tag"],
                    role=item.get("role"),
                    name=name[:300],
                    selector=candidate.selector,
                    href=item.get("href"),
                    element_type=item.get("type"),
                    required=bool(item.get("required")),
                    options=[str(option)[:200] for option in item.get("options", [])],
                    autocomplete=item.get("autocomplete"),
                    text=item.get("text"),
                    source_url=page.url,
                    actionable=True,
                    navigation_scope="unknown",
                )
            )
        # Stagehand's extraction is useful only if it is provably describing
        # text already visible on this exact page.  Do not preserve a model
        # paraphrase as product knowledge: a matching visible phrase is the
        # minimum evidence threshold for a section/control label.
        visible_text = " ".join(context.visible_text.split()).casefold()

        def grounded(phrases: list[str]) -> list[str]:
            result: list[str] = []
            for phrase in phrases:
                normalized = " ".join(phrase.split())
                if (
                    len(normalized) >= 3
                    and normalized.casefold() in visible_text
                    and normalized not in result
                ):
                    result.append(normalized)
            return result

        analysis = getattr(observation, "analysis", None)
        grounded_sections = grounded(analysis.visible_sections) if analysis else []
        grounded_controls = grounded(analysis.meaningful_controls) if analysis else []
        current_route = _canonical_route(page.url)
        page_knowledge = []
        for known_page in context.page_knowledge:
            if _canonical_route(known_page.url) != current_route:
                page_knowledge.append(known_page)
                continue
            page_knowledge.append(
                known_page.model_copy(
                    update={
                        "visible_sections": list(
                            dict.fromkeys([*known_page.visible_sections, *grounded_sections])
                        )[:30],
                        "actionable_controls": list(
                            dict.fromkeys([*known_page.actionable_controls, *grounded_controls])
                        )[:40],
                        "evidence_refs": [
                            *known_page.evidence_refs,
                            *[f"stagehand-grounded-section:{item}" for item in grounded_sections],
                            *[f"stagehand-grounded-control:{item}" for item in grounded_controls],
                        ][:60],
                    }
                )
            )
        if not additions and not grounded_sections and not grounded_controls:
            return context
        return context.model_copy(
            update={
                # ``context.elements`` already has a bounded discovery budget.
                # Do not reapply a lower Stagehand-specific cap here: it used to
                # remove the synthesized page-local landmarks at the tail of the
                # evidence list, leaving later primary pages unexecutable.
                "elements": [*context.elements, *additions],
                "content_blocks": list(
                    dict.fromkeys(
                        [
                            *context.content_blocks,
                            *grounded_sections,
                            *grounded_controls,
                        ]
                    )
                )[:60],
                "page_knowledge": page_knowledge,
                "evidence": [
                    *context.evidence,
                    f"stagehand suggestions re-grounded: {len(additions)}",
                    f"stagehand semantic labels re-grounded: {len(grounded_sections) + len(grounded_controls)}",
                ],
            }
        )

__all__ = [
    "DiscoveryOrchestrationMixin",
]
