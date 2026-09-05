"""Bounded, DOM-first product discovery for supported web applications."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from productlens.contracts.models import (
    CandidateDemoFlow,
    DiscoveryBudget,
    FeatureKnowledge,
    ObjectiveSpec,
    ObservedElement,
    PageKnowledge,
    ProductContext,
)


def _tokens(value: str) -> set[str]:
    return {item for item in re.findall(r"[a-z0-9]{3,}", value.lower())}


def _canonical_route(value: str) -> str:
    """Compare application routes without treating cosmetic URL variants as pages.

    Discovery must not spend its limited page budget on both ``/`` and an
    equivalent trailing-slash/query variant.  Fragments are presentation state,
    not independent pages for the purpose of product knowledge.
    """
    parsed = urlparse(value)
    # HTTP-to-HTTPS redirects are transport normalization, not distinct pages.
    scheme = "https" if parsed.scheme in {"http", "https"} else parsed.scheme
    path = parsed.path.rstrip("/") or "/"
    return parsed._replace(
        scheme=scheme, netloc=parsed.netloc.lower(), path=path, params="", query="", fragment=""
    ).geturl()


def _classify(title: str, text: str, elements: list[ObservedElement]) -> str:
    words = _tokens(f"{title} {text}")
    if {"lead", "contact", "pipeline"} & words:
        return "crm"
    if {"booking", "calendar", "schedule"} & words:
        return "scheduler"
    if any(item.tag == "form" for item in elements):
        return "form_application"
    return "web_application"


def _route_score(item: ObservedElement, objective_words: set[str]) -> int:
    candidate_words = _tokens(f"{item.name} {item.text or ''} {item.href or ''}")
    score = len(objective_words & candidate_words)
    # The synonyms are only ranking assistance; the actual route remains DOM evidence.
    if {"invite", "teammate", "member", "team"} & objective_words and {
        "user",
        "users",
    } & candidate_words:
        score += 3
    if {"report", "export", "csv"} & objective_words and "report" in candidate_words:
        score += 3
    if {"lead", "contact"} & objective_words and {"lead", "contact"} & candidate_words:
        score += 3
    if {"booking", "schedule", "calendar"} & objective_words and {
        "booking",
        "schedule",
        "calendar",
    } & candidate_words:
        score += 3
    return score


def _objective_spec(objective: str) -> ObjectiveSpec:
    lower = objective.lower()
    # Full-tour intent is often expressed without the literal phrase
    # ``full walkthrough`` (for example, "complete ... walkthrough of every
    # safe primary section").  Discovery uses this classification to order
    # its page budget, so under-classifying it silently replaces primary-page
    # coverage with objective-ranked detail routes.  Keep the signal generic
    # and objective-derived; no product labels belong here.
    full = bool(
        # Natural requests commonly place punctuation and duration words
        # between the completeness signal and ``walkthrough`` (for example,
        # "complete, evidence-backed 2 to 3 minute walkthrough").  Treat
        # those separators as prose, not as a reason to downgrade the entire
        # production envelope to a short feature demo.
        re.search(r"\bfull\b(?:[\s,;:\-]+[\w-]+){0,6}[\s,;:\-]+walkthrough\b", lower)
        or re.search(r"\bcomplete\b(?:[\s,;:\-]+[\w-]+){0,10}[\s,;:\-]+walkthrough\b", lower)
        or re.search(r"\b(?:every|all)\s+(?:safe\s+)?(?:primary\s+)?(?:section|page|tab)s?\b", lower)
        or "each tab" in lower
        or "entire" in lower
        or "whole product" in lower
    )
    words = [word for word in re.findall(r"[a-z0-9]{3,}", lower) if word not in {"show", "demo", "walkthrough", "full", "this", "with", "from", "that"}]
    requested = list(dict.fromkeys(words))[:12]
    return ObjectiveSpec(
        raw=objective,
        demo_type="full_walkthrough" if full else "feature_walkthrough",
        depth="thorough" if full else "standard",
        requested_features=requested,
        # Must-show items are only explicit user terms.  Discovery later
        # grounds them against actual page evidence; no supplied application
        # label is privileged at runtime.
        must_show=requested if not full else [],
        success_criteria=["requested content is visibly established", "each selected page is explored before transition"],
        minimum_duration_seconds=110 if full else 60,
        target_duration_seconds=180 if full else 120,
        # A complete walkthrough is allowed the full 2–3 minute delivery
        # envelope.  Discovery must not reject a faithful story merely because
        # Browserbase capture includes readable transition/dwell time.
        maximum_duration_seconds=300 if full else 180,
    )


def _page_knowledge(context: ProductContext) -> PageKnowledge:
    headings = [item.name for item in context.elements if item.tag in {"h1", "h2", "h3", "h4"}][:20]
    # Narrative blocks precede short control text. Otherwise a dense navigation
    # or link list can consume the fact budget before a card's contribution
    # evidence is available to the editorial writer.
    facts = list(dict.fromkeys([
        *context.content_blocks,
        *[" ".join((item.text or "").split())[:320] for item in context.elements if item.text and len(item.text.strip()) > 20],
    ]))[:30]
    controls = [item.name for item in context.elements if item.actionable and item.tag in {"a", "button", "input", "select"}][:30]
    fingerprint = hashlib.sha256((context.url + context.title + context.visible_text[:2000]).encode()).hexdigest()[:20]
    screenshot = next(
        (item.removeprefix("screenshot:") for item in context.evidence if item.startswith("screenshot:")),
        None,
    )
    return PageKnowledge(
        url=context.url, title=context.title, purpose=(headings[0] if headings else context.title),
        visible_sections=headings, scroll_landmarks=headings, actionable_controls=controls,
        visible_facts=facts, loading_behavior=["network-idle or bounded hydration wait"],
        evidence_refs=[f"page:{context.url}", *[f"section:{heading}" for heading in headings[:12]]],
        screenshot_evidence=screenshot, fingerprint=fingerprint,
    )


def _restore_missing_page_landmarks(
    elements: list[ObservedElement], pages: list[PageKnowledge]
) -> list[ObservedElement]:
    """Keep every inspected page executable when a dense DOM crowds out headings.

    Discovery intentionally caps raw controls per page.  On long tables this
    can leave a page with durable ``PageKnowledge`` but no retained local
    landmark, which made a full-tour planner navigate there and immediately
    advance.  Reconstitute only the page's observed heading evidence as
    role-grounded ScrollTo targets.  These are not guessed selectors: the
    locator resolves by the exact accessible heading name at production time.
    """
    restored = list(elements)
    existing = {
        ((item.source_url or "").rstrip("/"), " ".join(item.name.split()).casefold())
        for item in restored
    }
    for page in pages:
        page_url = page.url.rstrip("/")
        labels = list(dict.fromkeys([*page.scroll_landmarks, *page.visible_sections]))[:12]
        for label in labels:
            normalized = " ".join(label.split())
            key = (page_url, normalized.casefold())
            if len(normalized) < 3 or key in existing:
                continue
            digest = hashlib.sha256(f"{page_url}|{normalized}".encode()).hexdigest()[:12]
            restored.append(
                ObservedElement(
                    tag="h2",
                    role="heading",
                    name=normalized,
                    selector=f"observed-heading:{digest}",
                    text=normalized,
                    source_url=page.url,
                    actionable=True,
                )
            )
            existing.add(key)
    return restored


class LiveDiscovery:
    """Inspects only the current page and its visible navigation; it never crawls blindly."""

    async def inspect(
        self, page, objective: str, budget: DiscoveryBudget | None = None
    ) -> ProductContext:
        budget = budget or DiscoveryBudget()
        if budget.max_pages < 1 or budget.max_actions < 1:
            raise ValueError("Discovery budget does not permit a page inspection")
        # SPA controls often appear immediately after DOMContentLoaded.  Give the
        # hydrated accessibility tree a brief, bounded settling window.
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except PlaywrightTimeoutError:
            # Long-polling applications may never be idle; the short fallback is
            # still enough to inspect their hydrated DOM without stalling a run.
            await page.wait_for_timeout(700)
        title = await page.title()
        visible_text = (await page.locator("body").inner_text())[:8_000]
        # Cards and article bodies carry the explanation a viewer needs, but
        # are often not actionable controls. Capture their visible prose as
        # evidence for the editorial layer without turning them into click
        # targets or treating a title as a complete fact.
        raw_blocks = await page.locator("main section, main article, section, article, [data-testid]").evaluate_all(
            """nodes => nodes.slice(0, 40).map(node => (node.innerText || '').replace(/\\s+/g, ' ').trim()).filter(text => text.length >= 40).slice(0, 20)"""
        )
        # Some portfolio/timeline cards are plain divs rather than semantic
        # sections or heading containers. Select bounded *leaf-like* readable
        # cards so a company/project title retains the visible role and
        # contribution text needed for editorial narration.
        card_blocks = await page.locator("main div").evaluate_all(
            """nodes => nodes.map(node => {
                const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                const childWithSameText = Array.from(node.children).some(child =>
                    ((child.innerText || '').replace(/\\s+/g, ' ').trim()) === text
                );
                return {text, childWithSameText};
            }).filter(item => item.text.length >= 120 && item.text.length <= 1400 && !item.childWithSameText)
              .map(item => item.text).slice(0, 40)"""
        )
        # Component libraries frequently render project cards as nested divs
        # rather than semantic articles. Associate every visible heading with
        # its nearest readable container so editorial facts retain the card's
        # description, role, contribution, and outcome—not merely its title.
        heading_blocks = await page.locator("h1,h2,h3,h4").evaluate_all(
            """nodes => nodes.slice(0, 60).map(node => {
                const heading = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                let parent = node.parentElement, body = '';
                while (parent) {
                    const candidate = (parent.innerText || '').replace(/\\s+/g, ' ').trim();
                    // Rich timeline cards can legitimately exceed a small
                    // container threshold. Keep the nearest readable card and
                    // apply the artifact size limit after evidence extraction.
                    if (candidate.length >= 80) { body = candidate; break; }
                    parent = parent.parentElement;
                }
                return heading && body ? `${heading} :: ${body}` : '';
            }).filter(Boolean).slice(0, 50)"""
        )
        content_blocks = list(dict.fromkeys(str(block)[:700] for block in [*heading_blocks, *raw_blocks, *card_blocks]))
        # Headings are also valid presentation landmarks.  They are not
        # necessarily clickable, but retaining them lets a director create a
        # natural scroll tour of a project collection rather than jumping to
        # whichever CTA happens to be actionable.
        raw = await page.locator("a,button,input,select,textarea,[role='button'],h1,h2,h3,h4").evaluate_all(
            """nodes => nodes.slice(0, 120).map((node, index) => ({
                tag: node.tagName.toLowerCase(),
                // Headings inside a card-link are still excellent reading
                // landmarks, but they are also a discoverable, semantic way
                // to enter the representative detail. Retain the ancestor's
                // route instead of losing it just because the card's label is
                // rendered by a nested heading.
                role: node.getAttribute('role') || node.closest('a,button,[role="button"]')?.getAttribute('role'),
                name: node.getAttribute('aria-label') || node.getAttribute('name') ||
                    node.getAttribute('placeholder') ||
                    (/^h[1-4]$/i.test(node.tagName) ? (node.innerText || '').split(/\\n/)[0] : '') ||
                    node.innerText || node.value || `element-${index}`,
                selector: node.getAttribute('data-testid') ? `[data-testid="${node.getAttribute('data-testid')}"]` :
                    node.id ? `#${CSS.escape(node.id)}` :
                    node.tagName.toLowerCase() + (node.getAttribute('name') ? `[name="${node.getAttribute('name')}"]` : ''),
                href: node.getAttribute('href') || node.closest('a[href]')?.getAttribute('href'), type: node.getAttribute('type'),
                required: node.required === true, autocomplete: node.getAttribute('autocomplete'),
                options: node.tagName.toLowerCase() === 'select' ? Array.from(node.options).map(option => option.value || option.text).filter(Boolean).slice(0, 40) : [],
                text: node.innerText || null,
                navigation_scope: (() => { const container=node.closest('header,nav,footer,[role="navigation"]'); if (!container) return 'unknown'; if (container.tagName.toLowerCase()==='footer') return 'footer'; return 'primary'; })(),
                visible: !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length)
            }))"""
        )
        elements = [
            ObservedElement(
                tag=item["tag"],
                role=item.get("role"),
                name=str(item["name"]).strip()[:300],
                selector=item["selector"],
                href=item.get("href"),
                element_type=item.get("type"),
                required=bool(item.get("required")),
                options=[str(option)[:200] for option in item.get("options", [])],
                autocomplete=item.get("autocomplete"),
                text=item.get("text"),
                source_url=page.url,
                actionable=item["visible"],
                navigation_scope=item.get("navigation_scope", "unknown"),
            )
            for item in raw
            if item["visible"] and str(item["name"]).strip()
        ]
        objective_words = _tokens(objective)
        origin = urlparse(page.url)
        navigation = [item for item in elements if item.href]
        ranked = sorted(
            navigation,
            key=lambda item: _route_score(item, objective_words),
            reverse=True,
        )
        routes: list[str] = []
        canonical_routes: set[str] = set()
        for item in ranked:
            absolute = urljoin(page.url, item.href or "")
            parsed = urlparse(absolute)
            if parsed.scheme not in {"http", "https", "file"} or parsed.netloc not in {
                "",
                origin.netloc,
            }:
                continue
            canonical = _canonical_route(absolute)
            if canonical not in canonical_routes:
                routes.append(absolute)
                canonical_routes.add(canonical)
            if len(routes) >= max(1, budget.max_pages - 1):
                break
        login = any("password" in (item.element_type or "").lower() for item in elements)
        return ProductContext(
            url=page.url,
            title=title[:500],
            application_type=_classify(title, visible_text, elements),
            authentication_state="login_required" if login else "unknown",
            visible_text=visible_text,
            content_blocks=content_blocks,
            relevant_routes=routes,
            navigation=navigation[:40],
            elements=elements[:80],
            blockers=["authentication required"] if login else [],
            evidence=[
                "current DOM",
                "visible text",
                "accessible names",
                "same-origin navigation only",
            ],
            confidence=0.8 if elements else 0.2,
        )

    async def discover(
        self,
        page,
        objective: str,
        budget: DiscoveryBudget | None = None,
        known_routes: list[str] | None = None,
        known_actions: list[dict] | None = None,
        explore_visible_routes: bool = False,
        screenshot_directory: Path | None = None,
    ) -> ProductContext:
        """Explore a bounded set of objective-ranked, same-origin routes and restore the entry URL."""
        budget = budget or DiscoveryBudget()
        entry_url = page.url
        async def inspect_with_scroll_evidence() -> ProductContext:
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
            initial = await self.inspect(page, objective, budget)
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
                        await page.evaluate("y => window.scrollTo({top: y, behavior: 'instant'})", max(0, height * fraction - viewport * 0.4))
                        await page.wait_for_timeout(1_000)
                        samples.append(await self.inspect(page, objective, budget))
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
            blocks = list(dict.fromkeys(block for sample in samples for block in sample.content_blocks))
            return initial.model_copy(update={
                "elements": elements,
                "content_blocks": blocks,
                "visible_text": max((sample.visible_text for sample in samples), key=len),
                "evidence": [*initial.evidence, "bounded scroll-state evidence"],
            })

        primary = await inspect_with_scroll_evidence()
        if explore_visible_routes:
            origin = urlparse(entry_url)
            visible_routes: list[str] = []
            for item in primary.navigation:
                absolute = urljoin(entry_url, item.href or "")
                parsed = urlparse(absolute)
                if parsed.scheme in {"http", "https", "file"} and parsed.netloc in {"", origin.netloc}:
                    visible_routes.append(absolute)
            # A thorough walkthrough must give every safe primary tab a chance
            # to contribute page-local knowledge.  Keep the visible navigation
            # controls ahead of deep cards such as Week 1, while retaining the
            # objective-ranked candidates afterwards for feature requests.
            primary_controls = [
                route for route in visible_routes
                if urlparse(route).path.count("/") <= 1
            ]
            ordered_routes = [
                *primary_controls,
                *primary.relevant_routes,
                *visible_routes,
            ] if _objective_spec(objective).demo_type == "full_walkthrough" else [
                *primary.relevant_routes,
                *visible_routes,
            ]
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
                update={
                    "relevant_routes": deduped_routes
                }
            )
        same_origin_known = [
            route
            for route in known_routes or []
            if urlparse(route).netloc in {"", urlparse(entry_url).netloc}
        ]
        # Fresh visible primary controls outrank cached/deep routes for a full
        # walkthrough.  Cached knowledge still participates, but must not turn
        # a whole-product tour into a sweep of repeated detail URLs.
        route_sources = (
            [*primary.relevant_routes, *same_origin_known]
            if _objective_spec(objective).demo_type == "full_walkthrough"
            else [*same_origin_known, *primary.relevant_routes]
        )
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
            if _objective_spec(objective).demo_type == "full_walkthrough":
                primary_routes = [
                    urljoin(entry_url, item.href or "")
                    for item in primary.navigation
                    if item.href
                    and urlparse(urljoin(entry_url, item.href)).netloc in {"", urlparse(entry_url).netloc}
                    and urlparse(urljoin(entry_url, item.href)).path.count("/") <= 1
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
            for route in routes_to_inspect:
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
                        anchors = page.locator("a")
                        for index in range(await anchors.count()):
                            candidate = anchors.nth(index)
                            href = await candidate.get_attribute("href")
                            if not href or _canonical_route(urljoin(page.url, href)) != canonical:
                                continue
                            if not await candidate.is_visible():
                                continue
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
                        await page.goto(route, wait_until="domcontentloaded")
                    except PlaywrightError:
                        rejected_routes.append(f"navigation_failed:{route}")
                        continue
                    navigation_probes.append(f"direct_navigation_fallback:{route}")
                collected.append(await inspect_with_scroll_evidence())
        finally:
            if _canonical_route(page.url) != _canonical_route(entry_url):
                await page.goto(entry_url, wait_until="domcontentloaded")
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
        observed_targets = {
            (item.selector, item.name.lower())
            for item in elements
        }
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
        objective_spec = _objective_spec(objective)
        objective_words = _tokens(objective)
        features = []
        for page_info in pages:
            terms = _tokens(" ".join([page_info.title, page_info.purpose, *page_info.visible_sections, *page_info.visible_facts[:6]]))
            score = min(1.0, 0.25 + 0.15 * len(terms & objective_words) + (0.35 if objective_spec.demo_type == "full_walkthrough" else 0))
            # Semantic relevance is still grounded in the observed page terms;
            # these synonym bridges prevent an "invite teammate" objective
            # from ranking a generic settings page above an observed Users
            # page merely because neither label repeats the request verbatim.
            if {"invite", "teammate", "member", "team"} & objective_words and {"user", "users", "member", "members"} & terms:
                score = max(score, 0.82)
            if {"report", "export", "csv"} & objective_words and {"report", "reports", "analytics"} & terms:
                score = max(score, 0.82)
            features.append(FeatureKnowledge(name=page_info.purpose, purpose=page_info.purpose, entry_urls=[page_info.url], related_urls=[], evidence=[f"page:{page_info.url}", *[f"section:{section}" for section in page_info.visible_sections[:4]]], relevance_score=score))
        ranked_pages = sorted(pages, key=lambda page_info: next((feature.relevance_score for feature in features if feature.name == page_info.purpose), 0), reverse=True)
        flow_pages = [page_info.url for page_info in (pages if objective_spec.demo_type == "full_walkthrough" else ranked_pages[: min(3, len(ranked_pages))])]
        flow = CandidateDemoFlow(
            name="evidence-backed walkthrough", page_urls=flow_pages,
            rationale=["objective-relevant page knowledge", "visible sections and controls inspected"],
            expected_outcomes=[page_info.purpose for page_info in ranked_pages[:6]],
            risks=["external links and side effects excluded"],
            estimated_duration_seconds=objective_spec.target_duration_seconds,
            evidence_coverage=[evidence for page_info in ranked_pages for evidence in page_info.evidence_refs[:3]],
            score=0.85 if pages else 0.0,
        )
        candidates = [flow]
        # Retain alternatives as explicit, scored evidence. Planning can then
        # reject a merely navigable page instead of silently broadening a
        # narrow request into a route sweep.
        if objective_spec.demo_type != "full_walkthrough":
            for page_info in ranked_pages[: min(4, len(ranked_pages))]:
                # The opening page already provides context.  A duplicate
                # "focused" candidate contains no feature outcome and can win
                # by being artificially short, which would make scope
                # validation reject the genuinely relevant observed page.
                if _canonical_route(page_info.url) == _canonical_route(primary.url):
                    continue
                feature = next((item for item in features if item.name == page_info.purpose), None)
                relevance = feature.relevance_score if feature else 0.25
                candidates.append(CandidateDemoFlow(
                    name=f"focused: {page_info.purpose}", page_urls=[primary.url, page_info.url],
                    rationale=["direct objective relevance", "supporting opening context"],
                    expected_outcomes=[page_info.purpose], risks=["unrelated primary pages excluded"],
                    estimated_duration_seconds=90,
                    evidence_coverage=page_info.evidence_refs[:8], score=round(relevance, 3),
                ))
        return primary.model_copy(
            update={
                # Keep enough evidence for every inspected page.  A single
                # dense DSA table can legitimately contain more than 120
                # controls; a global first-N truncation used to erase Builds
                # and Progress entirely after they had been successfully
                # explored.  The bounded route/page budget keeps this payload
                # finite, while planner prompts apply their own compact view.
                "elements": elements[:360],
                "navigation": [item for item in elements if item.href][:50],
                "relevant_routes": list(
                    dict.fromkeys(
                        route for context in collected for route in context.relevant_routes
                    )
                )[: budget.max_pages],
                "evidence": [*primary.evidence, f"bounded routes inspected: {len(collected)}"],
                "successful_action_hints": action_hints[:30],
                "confidence": min(1.0, primary.confidence + 0.05 * (len(collected) - 1)),
                "objective": objective_spec,
                "page_knowledge": pages,
                "feature_knowledge": features,
                "candidate_demo_flows": sorted(candidates, key=lambda candidate: candidate.score, reverse=True),
                "exploration_actions": navigation_probes,
                "rejected_routes": rejected_routes,
            }
        )

    async def enrich_with_stagehand(self, page, context: ProductContext, observation) -> ProductContext:
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
                    tag=item["tag"], role=item.get("role"), name=name[:300],
                    selector=candidate.selector, href=item.get("href"),
                    element_type=item.get("type"), required=bool(item.get("required")),
                    options=[str(option)[:200] for option in item.get("options", [])],
                    autocomplete=item.get("autocomplete"), text=item.get("text"),
                    source_url=page.url, actionable=True, navigation_scope="unknown",
                )
            )
        if not additions:
            return context
        return context.model_copy(update={
            # ``context.elements`` already has a bounded discovery budget.
            # Do not reapply a lower Stagehand-specific cap here: it used to
            # remove the synthesized page-local landmarks at the tail of the
            # evidence list, leaving later primary pages unexecutable.
            "elements": [*context.elements, *additions],
            "evidence": [*context.evidence, f"stagehand suggestions re-grounded: {len(additions)}"],
        })
