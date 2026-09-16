"""Page inspection and DOM semantic capture for live discovery."""

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

class PageCaptureMixin:
    @staticmethod
    async def _safe_escape(page) -> None:
        """Dismiss a reversible probe without letting a wedged CDP target leak.

        Browser targets can stop answering while a portal is animating.  Cleanup
        must be best-effort and bounded; otherwise a harmless Escape in a
        ``finally`` block can consume the entire discovery watchdog.
        """
        try:
            await asyncio.wait_for(page.keyboard.press("Escape"), timeout=3)
            await asyncio.wait_for(page.wait_for_timeout(250), timeout=2)
        except (PlaywrightError, TimeoutError):
            return

    async def _visible_semantic_control(self, page, item: ObservedElement):
        """Resolve one currently visible discovery control without coordinates."""
        role = item.role if item.role in {"button", "link"} else "button"
        locators = [page.get_by_role(role, name=item.name, exact=True)]
        if item.selector.startswith(("#", "[")):
            locators.append(page.locator(item.selector))
        locators.append(page.get_by_text(item.name, exact=True))
        viewport = await page.evaluate(
            "() => ({width: window.innerWidth, height: window.innerHeight})"
        )
        deferred = None
        for locator in locators:
            # Remote CDP locator queries can stall on a continuously
            # re-rendering SPA.  Discovery must skip one unresponsive semantic
            # candidate and continue with the remaining evidence rather than
            # consuming the whole run deadline.
            try:
                # Do not call ``count()`` here.  Counting a virtualized list or
                # portal subtree forces a full remote DOM query and, on a
                # target that is mid-transition, can block until the outer
                # Browserbase lease expires.  The observed semantic control is
                # already ranked and de-duplicated; the first currently
                # visible match is the safe candidate.
                candidate = locator.first
                visible = await asyncio.wait_for(candidate.is_visible(), timeout=1.5)
                box = await asyncio.wait_for(candidate.bounding_box(), timeout=1.5)
            except (PlaywrightError, TimeoutError):
                continue
            if not visible:
                continue
            if not box or box["width"] < 2 or box["height"] < 2:
                continue
            in_viewport = (
                box["x"] + box["width"] > 0
                and box["x"] < viewport["width"]
                and box["y"] + box["height"] > 0
                and box["y"] < viewport["height"]
            )
            if in_viewport:
                return candidate
            deferred = deferred or candidate
        if deferred is not None:
            # The semantic locator is still valid but currently below the
            # fold. Scroll its DOM element into the reading region, then
            # re-check geometry; do not treat Playwright's CSS visibility as
            # proof that an off-screen virtualized duplicate is actionable.
            try:
                await deferred.evaluate(
                    "element => element.scrollIntoView({block: 'center', inline: 'nearest'})"
                )
                await page.wait_for_timeout(150)
                box = await deferred.bounding_box()
                if (
                    box
                    and box["width"] >= 2
                    and box["height"] >= 2
                    and box["x"] + box["width"] > 0
                    and box["x"] < viewport["width"]
                    and box["y"] + box["height"] > 0
                    and box["y"] < viewport["height"]
                ):
                    return deferred
            except PlaywrightError:
                pass
        return None

    @staticmethod
    async def _dom_semantic_click(page, item: ObservedElement) -> bool:
        """Dispatch one evidence-grounded semantic click through the DOM.

        A remote CDP session can reject Playwright's locator click while the
        renderer is reconciling a portal.  The observed accessible name and
        visible geometry still provide a safe target, so a single DOM-native
        ``click()`` is a bounded fallback.  It never uses coordinates or a
        product-specific selector and is limited to the reversible discovery
        probe (production remains Playwright-authoritative).
        """
        try:
            result = await asyncio.wait_for(
                page.evaluate(
                    """name => {
                        const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();
                        const wanted = normalize(name);
                        const nodes = Array.from(document.querySelectorAll(
                            'button, [role="button"], input[type="button"], input[type="submit"]'
                        ));
                        const node = nodes.find(candidate => {
                            const rect = candidate.getBoundingClientRect();
                            if (!(rect.width || rect.height || candidate.getClientRects().length)) return false;
                            const label = normalize(
                                candidate.getAttribute('aria-label') || candidate.innerText || candidate.value
                            );
                            return label === wanted;
                        });
                        if (!node) return false;
                        node.click();
                        return true;
                    }""",
                    item.name,
                ),
                timeout=5,
            )
            return bool(result)
        except (PlaywrightError, TimeoutError):
            return False

    async def _form_schema_from_scope(self, scope, source_url: str) -> FormSchema:
        """Extract only stable, human-identifiable controls from an open form.

        A full-page inspection is intentionally broad for navigation and
        editorial evidence. It is unsafe as a form schema: design systems can
        expose anonymous helper inputs or repeated checkbox internals outside
        the active modal. The open dialog/form is the authoritative boundary.
        """
        raw = await scope.locator(
            "input, select, textarea, [contenteditable='true'], [role='combobox'], [role='textbox'], [role='searchbox'], [role='spinbutton'], [role='checkbox'], [role='radio']"
        ).evaluate_all(
            """nodes => nodes.map(node => {
                const text = value => (value || '').replace(/\\s+/g, ' ').trim();
                const labelled = (node.getAttribute('aria-labelledby') || '').split(/\\s+/)
                    .map(id => document.getElementById(id)?.innerText || '')
                    .map(text).filter(Boolean).join(' ');
                const labels = Array.from(node.labels || []).map(label => text(label.innerText)).filter(Boolean).join(' ');
                const enclosingLabel = text(node.closest('label')?.innerText || '');
                const idLabel = node.id ? text(document.querySelector(`label[for="${CSS.escape(node.id)}"]`)?.innerText || '') : '';
                // Component libraries commonly place the human label,
                // required marker, helper text and validation message around
                // a generated input rather than on the input itself.  Looking
                // only at `parentElement` loses that evidence for MUI/Radix
                // style comboboxes and causes an unsafe partial submit later.
                // Keep the evidence local to this field: described-by nodes
                // are explicitly associated with the control, while the
                // nearest semantic/form-control ancestor carries its label.
                const describedBy = (node.getAttribute('aria-describedby') || '').split(/\\s+/)
                    .map(id => document.getElementById(id)?.innerText || '')
                    .map(text).filter(Boolean).join(' ');
                const fieldContainer = node.closest(
                    '[role="group"], .MuiFormControl-root, [data-field], [data-slot="field"]'
                ) || node.parentElement?.parentElement || node.parentElement;
                const localContext = text([fieldContainer?.innerText || '', describedBy].filter(Boolean).join(' '));
                const validationMessages = Array.from(fieldContainer?.querySelectorAll(
                    '[role="alert"], [aria-live="assertive"], [aria-invalid="true"]'
                ) || []).map(node => text(node.innerText || node.textContent || '')).filter(Boolean).slice(0, 8);
                const selectedText = node.tagName.toLowerCase() === 'select' && node.selectedOptions?.length
                    ? text(node.selectedOptions[0].innerText || node.selectedOptions[0].textContent || '')
                    : '';
                const displayText = text(selectedText || node.innerText || node.value || '');
                const ariaLabel = text(node.getAttribute('aria-label') || '');
                // Some component libraries expose a locale/value code such
                // as ``en`` through aria-label while the visible control says
                // ``English``.  A short code is not useful narration or
                // grounding evidence when a readable selected label exists,
                // so prefer the visible label without relying on any product
                // vocabulary.
                const accessibleLabel = ariaLabel && ariaLabel.length <= 3 && displayText.length > ariaLabel.length
                    ? displayText
                    : ariaLabel;
                const semanticLabel = text(
                    accessibleLabel || labelled || labels || enclosingLabel || idLabel ||
                    node.getAttribute('placeholder') || node.getAttribute('name') || displayText || ''
                );
                // A visible asterisk is direct required-field evidence, not
                // part of the label later used for semantic grounding.
                const semanticName = text(semanticLabel.replace(/\\*+/g, ' '));
                const stableId = node.id && !/^:r[0-9a-z]+:$/i.test(node.id);
                const selector = node.getAttribute('data-testid') ? `[data-testid="${CSS.escape(node.getAttribute('data-testid'))}"]` :
                    node.getAttribute('name') ? `[name="${CSS.escape(node.getAttribute('name'))}"]` :
                    node.getAttribute('placeholder') ? `[placeholder="${CSS.escape(node.getAttribute('placeholder'))}"]` :
                    stableId ? `#${CSS.escape(node.id)}` :
                    node.getAttribute('aria-label') ? `[aria-label="${CSS.escape(node.getAttribute('aria-label'))}"]` :
                    // The execution target retains the accessible label and
                    // deliberately ignores this marker as CSS. It is safer
                    // than dropping a labelled control merely because a
                    // component did not expose a stable attribute selector.
                    `label:${semanticName}`;
                const role = node.getAttribute('role') || '';
                const type = (node.getAttribute('type') || node.tagName.toLowerCase()).toLowerCase();
                const controlType = role || (
                    node.getAttribute('aria-haspopup') === 'listbox' ||
                    node.getAttribute('aria-autocomplete') === 'list'
                        ? 'combobox'
                        : type
                );
                const dependencyHint = node.getAttribute('data-depends-on') ||
                    node.getAttribute('aria-depends-on') ||
                    node.getAttribute('data-dependent-on') || '';
                return {
                    name: semanticName, selector, controlType,
                    dependsOn: dependencyHint.split(',').map(value => value.trim()).filter(Boolean).slice(0, 8),
                    validationMessages,
                    // Browser-native validation is ideal, but form libraries
                    // often expose an asterisk only through the visible label.
                    // It is still direct UI evidence and lets a safe planner
                    // prefer the fields a person is expected to complete.
                    required: node.required === true || node.getAttribute('aria-required') === 'true' ||
                        /\\*/.test(semanticLabel) || /\\brequired\\b/i.test(localContext),
                    visible: !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length),
                    disabled: node.disabled === true || node.getAttribute('aria-disabled') === 'true',
                    options: node.tagName.toLowerCase() === 'select'
                        ? Array.from(node.options).map(option => option.value || option.text).filter(Boolean).slice(0, 40)
                        : [],
                };
            }).filter(item => item.visible && !item.disabled && item.selector && item.name &&
                !/^(?:element-\\d+|on|off|x|true|false|\\d+)$/i.test(item.name) &&
                !['hidden', 'submit', 'button', 'reset'].includes(item.controlType))"""
        )
        fields: list[FormField] = []
        seen: set[tuple[str, str]] = set()
        for item in raw:
            key = (str(item["selector"]), str(item["name"]).casefold())
            if key in seen:
                continue
            seen.add(key)
            fields.append(
                FormField(
                    name=str(item["name"])[:200],
                    selector=str(item["selector"]),
                    control_type=str(item["controlType"]),
                    required=bool(item["required"]),
                    options=[str(value)[:200] for value in item.get("options", [])],
                    confidence=0.95,
                    depends_on=[str(value)[:200] for value in item.get("dependsOn", [])],
                    validation_messages=[
                        str(value)[:300] for value in item.get("validationMessages", [])
                    ],
                )
            )
        scope_text = (await scope.inner_text())[:8_000]
        observed_required = any(field.required for field in fields)
        # "Required" is an explicit visible form-state signal. If the scoped
        # DOM exposes it but no required editable control could be grounded,
        # preserve that uncertainty so the workflow cannot submit guessed or
        # partial data. This remains generic across design systems.
        unresolved_required = (
            bool(re.search(r"\brequired\b", scope_text, flags=re.IGNORECASE))
            and not observed_required
        )
        evidence = [
            "active form scope",
            "accessible label/placeholder/name",
            "stable semantic selector",
        ]
        if unresolved_required:
            evidence.append("unresolved visible required controls")
        return FormSchema(
            source_url=source_url,
            fields=fields,
            evidence=evidence,
            unresolved_required_fields=unresolved_required,
        )

    async def _enrich_choice_options(self, page, schema: FormSchema) -> FormSchema:
        """Record observed choices for native and accessible select controls.

        Opening a choice list is reversible and happens only inside the
        already-open form probe. A production plan can therefore select a
        real option rather than inventing a value for a custom combobox.
        """
        enriched = []
        for field in schema.fields:
            if field.control_type.casefold() not in {"select", "combobox"} or field.options:
                enriched.append(field)
                continue
            locators = [page.get_by_label(field.name, exact=True)]
            if field.control_type.casefold() == "combobox":
                locators.append(page.get_by_role("combobox", name=field.name, exact=True))
            if field.selector.startswith(("#", "[")):
                locators.append(page.locator(field.selector))
            control = None
            for locator in locators:
                for index in range(await locator.count()):
                    candidate = locator.nth(index)
                    if await candidate.is_visible():
                        control = candidate
                        break
                if control is not None:
                    break
            if control is None and field.control_type.casefold() == "combobox":
                # A number of design systems render a visually labelled
                # combobox without a `for`, aria-label, or aria-labelledby
                # relationship.  Do not fall back to a coordinate or a broad
                # first-combobox click. Instead, find the unique visible
                # combobox whose *nearest* readable ancestor contains this
                # field's observed label. This is DOM evidence and remains
                # safe across component libraries.
                semantic_candidates = await page.locator("[role='combobox']").evaluate_all(
                    """(nodes, fieldName) => {
                        const normal = value => (value || '').replace(/\\s+/g, ' ').trim().toLocaleLowerCase();
                        const sought = normal(fieldName);
                        return nodes.map((node, index) => {
                            let parent = node.parentElement;
                            for (let depth = 1; parent && depth <= 6; depth += 1, parent = parent.parentElement) {
                                const label = normal(parent.innerText);
                                if (label.includes(sought) && label.length <= 420) return {index, depth};
                            }
                            return null;
                        }).filter(Boolean);
                    }""",
                    field.name,
                )
                if semantic_candidates:
                    nearest_depth = min(int(item["depth"]) for item in semantic_candidates)
                    nearest = [
                        item for item in semantic_candidates if int(item["depth"]) == nearest_depth
                    ]
                    if len(nearest) == 1:
                        candidate = page.locator("[role='combobox']").nth(int(nearest[0]["index"]))
                        if await candidate.is_visible():
                            control = candidate
            if control is None:
                enriched.append(field)
                continue
            try:
                # Cloud CDP actionability can take a few seconds after a
                # dialog's entrance transition. A 1.5-second probe timeout
                # made a real, visible selector appear optionless and later
                # authorised an incomplete submit. This remains a bounded,
                # reversible discovery click; it is not a production retry.
                await control.click(timeout=5_000)
                # Some accessible comboboxes expose their first available
                # choice only after keyboard expansion. ArrowDown is a
                # reversible inspection gesture: it changes neither the form
                # value nor product state, and lets discovery observe choices
                # without fabricating a search term or selecting anything.
                if field.control_type.casefold() == "combobox":
                    try:
                        await control.press("ArrowDown")
                    except PlaywrightError:
                        pass
                # Remote/custom comboboxes commonly fetch their list after
                # the click.  A single 200ms snapshot made ProductLens treat
                # an otherwise valid dependent control as optionless. Wait
                # only for *visible observed* choices, never for a guessed
                # value or an arbitrary fixed form delay.
                values: list[str] = []
                for _attempt in range(8):
                    options = page.locator(
                        "[role='option'], [role='listbox'] li, [role='menu'] [role='menuitem']"
                    )
                    for index in range(await options.count()):
                        option = options.nth(index)
                        if not await option.is_visible():
                            continue
                        label = " ".join((await option.inner_text()).split())
                        if label:
                            values.append(label)
                    if values:
                        break
                    await page.wait_for_timeout(300)
                await self._safe_escape(page)
                enriched.append(
                    field.model_copy(update={"options": list(dict.fromkeys(values))[:40]})
                )
            except PlaywrightError:
                await self._safe_escape(page)
                enriched.append(field)
        return schema.model_copy(update={"fields": enriched})

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
        raw_blocks = await page.evaluate(
            """() => Array.from(document.querySelectorAll(`main section, main article, section, article, [data-testid]`)).slice(0, 200).map(node => (node.innerText || '').replace(/\\s+/g, ' ').trim()).filter(text => text.length >= 40).slice(0, 20)"""
        )
        # Some content/timeline cards are plain divs rather than semantic
        # sections or heading containers. Select bounded *leaf-like* readable
        # cards so a company/project title retains the visible role and
        # contribution text needed for editorial narration.
        # A locator-wide evaluation can remain pending while a large hydrated
        # dashboard is committing. Query a bounded slice in the page instead;
        # this preserves generic card evidence without consuming discovery's
        # entire deadline on one selector.
        card_blocks = await page.evaluate(
            """() => Array.from(document.querySelectorAll('main div')).slice(0, 4000).map(node => {
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
        heading_blocks = await page.evaluate(
            """() => Array.from(document.querySelectorAll(`h1,h2,h3,h4`)).slice(0, 120).map(node => {
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
        content_blocks = list(
            dict.fromkeys(
                str(block)[:700] for block in [*heading_blocks, *raw_blocks, *card_blocks]
            )
        )
        # Some dashboard shells do not expose a semantic ``main`` container
        # and render their page purpose as short text nodes between controls.
        # Without a fallback, PageKnowledge contains only date chips and the
        # editorial layer has no truthful purpose sentence to use. Collect a
        # bounded set of visible, action-oriented body lines; this remains
        # evidence extraction, never a product-specific template.
        if len(content_blocks) < 3:
            body_lines = await page.locator("body").evaluate(
                "node => (node.innerText || '').split(/\\n+/).map(value => value.replace(/\\s+/g, ' ').trim()).filter(Boolean)"
            )
            action_words = re.compile(
                r"\\b(?:manage|track|review|schedule|configure|organize|monitor|plan|create|compare|follow|coordinate|support|filter|list|build|edit|transition|confirm)\\b",
                re.IGNORECASE,
            )
            fallback_blocks = [
                str(line)[:320]
                for line in body_lines
                if isinstance(line, str)
                and 4 <= len(line.split()) <= 18
                and action_words.search(line)
            ]
            content_blocks = list(dict.fromkeys([*content_blocks, *fallback_blocks]))[:50]
        # Headings are also valid presentation landmarks.  They are not
        # necessarily clickable, but retaining them lets a director create a
        # natural scroll tour of a project collection rather than jumping to
        # whichever CTA happens to be actionable.
        # Read the interactive inventory in one bounded page-side operation.
        # Locator-wide evaluation is disproportionately expensive for virtual
        # lists and frequently re-rendering dashboards, while this query still
        # captures the same semantic evidence and caps work before sorting.
        raw = await page.evaluate(
            """() => {
                const selector = `a,button,input,select,textarea,iframe,canvas,svg,[contenteditable='true'],[draggable='true'],[dropzone],[aria-grabbed='true'],[aria-dropeffect],[role='application'],[role='toolbar'],[role='button'],[role='combobox'],[role='option'],[role='checkbox'],[role='radio'],[role='alert'],[role='status'],[role='dialog'],[role='row'],[role='gridcell'],tr,td,li,h1,h2,h3,h4`;
                // Shadow-root controls are part of the same observable page
                // even though document.querySelectorAll cannot cross the
                // boundary. Include open roots as fresh evidence; closed
                // roots remain represented by their host geometry/text.
                const roots = [document, ...Array.from(document.querySelectorAll('*')).map(node => node.shadowRoot).filter(Boolean)];
                const nodes = [
                    ...roots.flatMap(root => Array.from(root.querySelectorAll(selector))),
                    ...Array.from(document.querySelectorAll('*')).filter(node => node.shadowRoot),
                ];
                return nodes.slice(0, 5000).map((node, index) => ({
                // Keep the same human field identity used by scoped form
                // discovery. A placeholder is an implementation hint, not
                // presenter copy; using it as a scene name can leak a sample
                // email and makes a visible labelled form look anonymous.
                name: (() => {
                    const text = value => (value || '').replace(/\\s+/g, ' ').trim();
                    const labelled = (node.getAttribute('aria-labelledby') || '').split(/\\s+/)
                        .map(id => document.getElementById(id)?.innerText || '').map(text).filter(Boolean).join(' ');
                    const labels = Array.from(node.labels || []).map(label => text(label.innerText)).filter(Boolean).join(' ');
                    const enclosing = text(node.closest('label')?.innerText || '');
                    const idLabel = node.id ? text(document.querySelector(`label[for="${CSS.escape(node.id)}"]`)?.innerText || '') : '';
                    return text(node.getAttribute('aria-label') || labelled || labels || enclosing || idLabel ||
                        node.getAttribute('name') || node.getAttribute('placeholder') ||
                        (/^h[1-4]$/i.test(node.tagName) ? (node.innerText || '').split(/\\n/)[0] : '') ||
                        node.innerText || node.value ||
                        (/^(CANVAS|SVG)$/i.test(node.tagName) || ['application','toolbar'].includes(node.getAttribute('role')) || node.isContentEditable
                            ? `${node.tagName.toLowerCase()} workspace` : `element-${index}`));
                })(),
                tag: node.tagName.toLowerCase(),
                // Headings inside a card-link are still excellent reading
                // landmarks, but they are also a discoverable, semantic way
                // to enter the representative detail. Retain the ancestor's
                // route instead of losing it just because the card's label is
                // rendered by a nested heading.
                role: node.getAttribute('role') || node.closest('a,button,[role="button"]')?.getAttribute('role'),
                // Component libraries often generate ids such as ``:r30:``
                // for a single mount.  Those are useful neither in a fresh
                // rehearsal nor in production, so prefer stable semantic
                // attributes before accepting a real, non-generated id.
                selector: node.getAttribute('data-testid') ? `[data-testid="${node.getAttribute('data-testid')}"]` :
                    node.getAttribute('name') ? `[name="${node.getAttribute('name')}"]` :
                    node.getAttribute('placeholder') ? `[placeholder="${node.getAttribute('placeholder')}"]` :
                    (node.id && !/^:r[0-9a-z]+:$/i.test(node.id)) ? `#${CSS.escape(node.id)}` :
                    node.tagName.toLowerCase(),
                href: node.getAttribute('href') || node.closest('a[href]')?.getAttribute('href'), type: node.getAttribute('type'),
                required: node.required === true, autocomplete: node.getAttribute('autocomplete'),
                draggable: node.draggable === true || node.getAttribute('aria-grabbed') === 'true',
                dropzone: node.getAttribute('dropzone') !== null || node.getAttribute('aria-dropeffect') !== null,
                shadowRoot: !!node.shadowRoot,
                options: node.tagName.toLowerCase() === 'select' ? Array.from(node.options).map(option => option.value || option.text).filter(Boolean).slice(0, 40) : [],
                text: node.innerText || null,
                formPriority: node.closest('form,[role="dialog"],dialog') ? 1 : 0,
                navigation_scope: (() => { const container=node.closest('header,nav,footer,[role="navigation"]'); if (!container) return 'unknown'; if (container.tagName.toLowerCase()==='footer') return 'footer'; return 'primary'; })(),
                visible: !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length)
                })).sort((left, right) => right.formPriority - left.formPriority || Number(right.visible) - Number(left.visible)).slice(0, 160).map(({formPriority, ...item}) => item);
            }"""
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
                draggable=bool(item.get("draggable")),
                dropzone=bool(item.get("dropzone")),
                shadow_host=bool(item.get("shadowRoot")),
            )
            for item in raw
            if item["visible"] and str(item["name"]).strip()
        ]
        objective_words = _tokens(objective)
        origin = urlparse(page.url)
        # A link without an accessible/visible semantic name is not a
        # reliable production target. Keep it out of route planning rather
        # than manufacturing labels such as ``element-10`` from DOM order;
        # the raw evidence remains available for diagnostics.
        navigation = [
            item
            for item in elements
            if item.href
            and not re.fullmatch(r"element-\d+", item.name.strip(), flags=re.IGNORECASE)
        ]
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
        password_control = any("password" in (item.element_type or "").lower() for item in elements)
        # A public automation sandbox may intentionally expose a *login
        # example* alongside many other controls.  Treating any visible
        # password input as an authentication wall incorrectly blocks
        # preflight and full-tour planning.  Require a semantic auth heading
        # or an auth-like route before classifying the opening page as gated.
        auth_heading = any(
            item.tag in {"h1", "h2", "h3"}
            and re.search(r"\b(?:sign[ -]?in|log[ -]?in|authenticate)\b", item.name, re.IGNORECASE)
            for item in elements
        )
        auth_route = bool(
            re.search(r"/(?:login|signin|sign-in|auth)(?:/|$)", origin.path, re.IGNORECASE)
        )
        login = password_control and (auth_heading or auth_route)
        return ProductContext(
            url=page.url,
            title=title[:500],
            application_type=_classify(title, visible_text, elements),
            authentication_state="login_required" if login else "unknown",
            visible_text=visible_text,
            content_blocks=content_blocks,
            relevant_routes=routes,
            navigation=navigation[:40],
            # Form/dialog controls take priority above and remain intact here;
            # a global navigation-heavy shell must not erase the submit/result
            # evidence needed to compile a safe capability.
            elements=elements[:160],
            blockers=["authentication required"] if login else [],
            evidence=[
                "current DOM",
                "visible text",
                "accessible names",
                "same-origin navigation only",
            ],
            confidence=0.8 if elements else 0.2,
        )

__all__ = [
    "PageCaptureMixin",
]
