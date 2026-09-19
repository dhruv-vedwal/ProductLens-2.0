"""Page inspection and DOM semantic capture for live discovery."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.contracts.models import (
    DiscoveryBudget,
    FormField,
    FormSchema,
    ObservedElement,
    ProductContext,
)
from app.discovery.live.helpers import *
from app.interaction.state import classify_control, stable_control_id


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
        raw = await asyncio.wait_for(
            scope.locator(
                "input, select, textarea, [contenteditable='true'], [role='combobox'], [role='textbox'], [role='searchbox'], [role='spinbutton'], [role='checkbox'], [role='radio']"
            ).evaluate_all(
                """nodes => nodes.slice(0, 120).map(node => {
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
                    tag: node.tagName.toLowerCase(), role,
                    inputType: type,
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
                    observedValue: displayText || null,
                    geometryY: node.getBoundingClientRect().y,
                };
            }).filter(item => item.visible && item.selector && item.name &&
                !/^(?:element-\\d+|on|off|x|true|false|\\d+)$/i.test(item.name) &&
                !['hidden', 'submit', 'button', 'reset'].includes(item.controlType))"""
            ),
            timeout=18,
        )
        fields: list[FormField] = []
        seen: set[tuple[str, str]] = set()
        for item in raw:
            key = (str(item["selector"]), str(item["name"]).casefold())
            if key in seen:
                continue
            seen.add(key)
            behavior_class, _confidence = classify_control(
                {
                    "name": item["name"],
                    "tag": item.get("tag"),
                    "role": item.get("role"),
                    "type": item.get("inputType"),
                    "options": item.get("options", []),
                }
            )
            fields.append(
                FormField(
                    name=str(item["name"])[:200],
                    selector=str(item["selector"]),
                    control_type=str(item["controlType"]),
                    behavior_class=behavior_class,
                    stable_id=stable_control_id(
                        {
                            "name": item["name"],
                            "tag": item.get("tag"),
                            "role": item.get("role"),
                            "type": item.get("inputType"),
                            "geometry": {"x": 0, "y": item.get("geometryY") or 0},
                        },
                        surface_id=f"form:{source_url}",
                    ),
                    required=bool(item["required"]),
                    enabled=not bool(item.get("disabled")),
                    options=[str(value)[:200] for value in item.get("options", [])],
                    observed_value=(
                        str(item["observedValue"])[:300]
                        if item.get("observedValue")
                        else None
                    ),
                    geometry_y=(
                        float(item["geometryY"]) if item.get("geometryY") is not None else None
                    ),
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
        # Prefer selectable fields that lack options first—these are the ones
        # that later block safe rehearsal compilation—then fill remaining
        # choice controls within a small bounded budget.
        pending_choice_enrichment = 0
        ordered_fields = sorted(
            schema.fields,
            key=lambda field: (
                0
                if field.control_type.casefold() in {"select", "combobox"} and not field.options
                else 1,
                0 if field.required else 1,
            ),
        )
        for field in ordered_fields:
            if field.control_type.casefold() not in {"select", "combobox"} or field.options:
                enriched.append(field)
                continue
            if pending_choice_enrichment >= 6:
                enriched.append(field)
                continue
            pending_choice_enrichment += 1
            try:
                enriched_field = await asyncio.wait_for(
                    self._enrich_one_choice_field(page, field), timeout=10
                )
            except TimeoutError:
                enriched.append(field)
                continue
            enriched.append(enriched_field)
        enriched_by_key = {
            (field.name.casefold(), field.selector): field for field in enriched
        }
        restored = [
            enriched_by_key.get((field.name.casefold(), field.selector), field)
            for field in schema.fields
        ]
        return schema.model_copy(update={"fields": restored})

    async def _enrich_one_choice_field(self, page, field: FormField) -> FormField:
        """Open one reversible choice control and capture its visible options."""

        locators = []
        if field.selector.startswith("label:"):
            label = field.selector.removeprefix("label:").strip() or field.name
            locators.extend(
                [
                    page.get_by_role("combobox", name=re.compile(rf"^{re.escape(label)}$", re.I)),
                    page.get_by_label(re.compile(rf"^{re.escape(label)}$", re.I)),
                    page.get_by_text(label, exact=True),
                ]
            )
        locators.append(page.get_by_label(field.name, exact=True))
        if field.control_type.casefold() == "combobox":
            locators.append(page.get_by_role("combobox", name=field.name, exact=True))
        if field.selector.startswith(("#", "[")):
            locators.append(page.locator(field.selector))
        control = None
        for locator in locators:
            try:
                count = await locator.count()
            except PlaywrightError:
                continue
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if await candidate.is_visible():
                        # A plain text label match may resolve to the legend;
                        # prefer an actual combobox/input near that label.
                        tag = (
                            await candidate.evaluate("node => node.tagName.toLowerCase()")
                        ).casefold()
                        role = (
                            await candidate.evaluate(
                                "node => (node.getAttribute('role') || '').toLowerCase()"
                            )
                        ).casefold()
                        if tag in {"input", "select", "button"} or role in {
                            "combobox",
                            "listbox",
                            "button",
                        }:
                            control = candidate
                            break
                        nearby = candidate.locator(
                            "xpath=ancestor-or-self::*[1]/following::input[1] | ancestor-or-self::*[1]/following::*[@role='combobox'][1]"
                        )
                        if await nearby.count():
                            control = nearby.first
                            break
                except PlaywrightError:
                    continue
            if control is not None:
                break
        if control is None and field.control_type.casefold() == "combobox":
            # Last-resort: attribute/name hints often survive when the
            # accessible name is only rendered as a floating label.
            hint = re.sub(r"[^a-z0-9]", "", field.name.casefold())
            if hint:
                try:
                    attr = page.locator(
                        f"[role='combobox'][name*='{hint}' i], "
                        f"[role='combobox'][id*='{hint}' i], "
                        f"input[name*='{hint}' i][role='combobox'], "
                        f"div[role='combobox'][aria-label*='{field.name}' i]"
                    )
                    if await attr.count():
                        candidate = attr.first
                        if await candidate.is_visible():
                            control = candidate
                except PlaywrightError:
                    pass
        if control is None and field.control_type.casefold() == "combobox":
            semantic_candidates = await page.locator("[role='combobox']").evaluate_all(
                """(nodes, fieldName) => {
                    const normal = value => (value || '').replace(/\\s+/g, ' ').trim().toLocaleLowerCase();
                    const sought = normal(fieldName);
                    return nodes.map((node, index) => {
                        let parent = node.parentElement;
                        for (let depth = 1; parent && depth <= 6; depth += 1, parent = parent.parentElement) {
                            const label = normal(parent.innerText);
                            if (!label.includes(sought) || label.length > 420) continue;
                            const exact = label === sought || label.startsWith(sought + ' ') || label.startsWith(sought + '\\n');
                            return {index, depth, exact: exact ? 1 : 0, span: label.length};
                        }
                        return null;
                    }).filter(Boolean);
                }""",
                field.name,
            )
            if semantic_candidates:
                # Prefer an exact local label over a distant form container that
                # merely contains the field name among many siblings.
                best_exact = max(int(item.get("exact") or 0) for item in semantic_candidates)
                ranked = [
                    item for item in semantic_candidates if int(item.get("exact") or 0) == best_exact
                ]
                nearest_depth = min(int(item["depth"]) for item in ranked)
                nearest = [
                    item
                    for item in ranked
                    if int(item["depth"]) == nearest_depth
                ]
                nearest.sort(key=lambda item: int(item.get("span") or 10_000))
                if nearest:
                    candidate = page.locator("[role='combobox']").nth(int(nearest[0]["index"]))
                    if await candidate.is_visible():
                        control = candidate
        if control is None:
            return field
        try:
            await control.click(timeout=5_000)
            if field.control_type.casefold() == "combobox":
                # Design-system autocompletes often need an explicit popup
                # affordance or keyboard open after focus; try both before
                # treating the control as optionless.
                try:
                    popup = control.locator(
                        "xpath=ancestor::*[contains(@class,'Autocomplete') or contains(@class,'autocomplete')][1]//button[contains(@class,'popupIndicator') or @aria-label='Open' or @title='Open']"
                    )
                    if await popup.count():
                        await popup.first.click(timeout=2_000)
                except PlaywrightError:
                    pass
                try:
                    await control.press("ArrowDown")
                except PlaywrightError:
                    pass
            values: list[str] = []
            for _attempt in range(10):
                try:
                    await page.wait_for_selector(
                        "[role='listbox'], [role='option'], [role='menu']",
                        state="attached",
                        timeout=500,
                    )
                except PlaywrightError:
                    pass
                # Portaled listboxes can report not-visible briefly while still
                # exposing readable option text; harvest attached nodes too.
                harvested = await page.evaluate(
                    """() => {
                      const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
                      const selected = document.querySelector(
                        '[role="option"][aria-selected="true"], [role="option"].Mui-focused, [role="option"].Mui-focusVisible'
                      );
                      if (selected) {
                        const label = clean(selected.innerText || selected.textContent);
                        if (label) return [label];
                      }
                      const nodes = Array.from(document.querySelectorAll(
                        "[role='option'], [role='listbox'] li, [role='menu'] [role='menuitem'], ul[role='listbox'] li"
                      ));
                      return nodes
                        .map(node => clean(node.innerText || node.textContent || node.getAttribute('data-value')))
                        .filter(Boolean)
                        .slice(0, 40);
                    }"""
                )
                for label in harvested or []:
                    if label and str(label).casefold() not in {
                        "no options",
                        "no results",
                    }:
                        values.append(str(label))
                if values:
                    break
                if _attempt in {2, 5}:
                    try:
                        await control.click(timeout=2_000)
                        await control.press("Control+A")
                        await control.type("a", delay=40)
                        await control.press("ArrowDown")
                    except PlaywrightError:
                        pass
                await page.wait_for_timeout(350)
            await self._safe_escape(page)
            unique = list(dict.fromkeys(values))[:40]
            if not unique:
                # MUI Autocomplete sometimes leaves the focused option text on
                # the input itself after ArrowDown without mounting a listbox
                # long enough to harvest. Treat that typed/focused value as a
                # one-item option list so rehearsal can still select it.
                try:
                    typed = (await control.input_value(timeout=1_000) or "").strip()
                except PlaywrightError:
                    typed = ""
                if typed and typed.casefold() not in {
                    "select",
                    "select an option",
                    "choose",
                    "choose an option",
                }:
                    unique = [typed]
            if not unique:
                return field
            observed = (field.observed_value or "").strip()
            safe = [
                item
                for item in unique
                if item.casefold()
                not in {"select", "select an option", "choose", "choose an option"}
            ]
            chosen = next(
                (item for item in safe if item.casefold() == observed.casefold()),
                min(safe, key=str.casefold) if safe else None,
            )
            update = {"options": unique}
            if chosen and not observed:
                update["observed_value"] = chosen
            return field.model_copy(update=update)
        except PlaywrightError:
            await self._safe_escape(page)
            return field

    async def _probe_form_dependencies(self, page, scope, schema: FormSchema) -> FormSchema:
        """Select one reversible observed choice and record resulting child state."""

        fields = list(schema.fields)
        for parent in fields:
            if (
                not parent.enabled
                or parent.control_type.casefold() not in {"select", "combobox"}
                or not parent.options
            ):
                continue
            safe_options = [
                value
                for value in parent.options
                if value.strip().casefold()
                not in {"select", "select an option", "choose", "choose an option"}
            ]
            if not safe_options:
                continue
            value = min(safe_options, key=str.casefold)
            locators = [page.get_by_label(parent.name, exact=True)]
            if parent.selector.startswith(("#", "[")):
                locators.append(page.locator(parent.selector))
            control = None
            for locator in locators:
                try:
                    candidate = locator.first
                    if await candidate.is_visible() and await candidate.is_enabled():
                        control = candidate
                        break
                except PlaywrightError:
                    continue
            if control is None:
                continue
            try:
                tag = await control.evaluate("element => element.tagName.toLowerCase()")
                if tag == "select":
                    await control.select_option(label=value)
                else:
                    await control.click()
                    option = page.get_by_role("option", name=value, exact=True)
                    visible = [
                        option.nth(index)
                        for index in range(await option.count())
                        if await option.nth(index).is_visible()
                    ]
                    if len(visible) != 1:
                        await self._safe_escape(page)
                        continue
                    await visible[0].click()
                await page.wait_for_timeout(350)
                refreshed = await self._enrich_choice_options(
                    page, await self._form_schema_from_scope(scope, schema.source_url)
                )
            except PlaywrightError:
                await self._safe_escape(page)
                continue
            before_by_name = {item.name.casefold(): item for item in fields}
            promoted: list[FormField] = []
            for child in refreshed.fields:
                before = before_by_name.get(child.name.casefold())
                if before is None:
                    promoted.append(child)
                    continue
                became_enabled = not before.enabled and child.enabled
                became_populated = not before.options and bool(child.options)
                options_changed = bool(before.options and child.options != before.options)
                promoted.append(
                    child.model_copy(
                        update={
                            "depends_on": list(
                                dict.fromkeys(
                                    [
                                        *child.depends_on,
                                        *(
                                            [parent.name]
                                            if child.name != parent.name
                                            and (
                                                became_enabled
                                                or became_populated
                                                or options_changed
                                            )
                                            else []
                                        ),
                                    ]
                                )
                            ),
                        }
                    )
                )
            fields = [
                item.model_copy(update={"observed_value": value})
                if item.name.casefold() == parent.name.casefold()
                else item
                for item in promoted
            ]
        return schema.model_copy(
            update={
                "fields": fields,
                "evidence": [*schema.evidence, "reversible dependency transition probe"],
            }
        )

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

        async def _bounded_evaluate(expression: str, *, timeout: float = 12.0):
            # Browser-side DOM walks can freeze a remote CDP session long after
            # Python's wait_for fires. Keep each evaluate short so a heavy CRM
            # shell cannot wedge discovery for the full outer stage deadline.
            return await asyncio.wait_for(page.evaluate(expression), timeout=timeout)

        try:
            title = await asyncio.wait_for(page.title(), timeout=8)
        except TimeoutError:
            title = ""
        try:
            visible_text = (
                await asyncio.wait_for(page.locator("body").inner_text(), timeout=10)
            )[:8_000]
        except TimeoutError:
            visible_text = ""
        # Cards and article bodies carry the explanation a viewer needs, but
        # are often not actionable controls. Capture their visible prose as
        # evidence for the editorial layer without turning them into click
        # targets or treating a title as a complete fact.
        try:
            raw_blocks = await _bounded_evaluate(
                """() => Array.from(document.querySelectorAll(`main section, main article, section, article, [data-testid]`)).slice(0, 200).map(node => (node.innerText || '').replace(/\\s+/g, ' ').trim()).filter(text => text.length >= 40).slice(0, 20)"""
            )
        except TimeoutError:
            raw_blocks = []
        # Some content/timeline cards are plain divs rather than semantic
        # sections or heading containers. Select bounded *leaf-like* readable
        # cards so a company/project title retains the visible role and
        # contribution text needed for editorial narration.
        # Cap the candidate pool tightly: scanning thousands of nested CRM
        # divs on Browserbase previously wedged CDP until the outer discovery
        # watchdog cancelled the whole stage.
        try:
            card_blocks = await _bounded_evaluate(
                """() => Array.from(document.querySelectorAll('main div')).slice(0, 800).map(node => {
                const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                const childWithSameText = Array.from(node.children).some(child =>
                    ((child.innerText || '').replace(/\\s+/g, ' ').trim()) === text
                );
                return {text, childWithSameText};
            }).filter(item => item.text.length >= 120 && item.text.length <= 1400 && !item.childWithSameText)
              .map(item => item.text).slice(0, 40)"""
            )
        except TimeoutError:
            card_blocks = []
        # Component libraries frequently render project cards as nested divs
        # rather than semantic articles. Associate every visible heading with
        # its nearest readable container so editorial facts retain the card's
        # description, role, contribution, and outcome—not merely its title.
        try:
            heading_blocks = await _bounded_evaluate(
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
        except TimeoutError:
            heading_blocks = []
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
            try:
                body_lines = await asyncio.wait_for(
                    page.locator("body").evaluate(
                        "node => (node.innerText || '').split(/\\n+/).map(value => value.replace(/\\s+/g, ' ').trim()).filter(Boolean)"
                    ),
                    timeout=8,
                )
            except TimeoutError:
                body_lines = []
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
        # Never scan every DOM node for shadow roots: on large CRM shells that
        # walk freezes the remote renderer and leaves discovery waiting until
        # the outer stage watchdog fires. Sample a capped host set instead.
        try:
            raw = await _bounded_evaluate(
                """() => {
                const selector = `a,button,input,select,textarea,iframe,canvas,svg,[contenteditable='true'],[draggable='true'],[dropzone],[aria-grabbed='true'],[aria-dropeffect],[role='application'],[role='toolbar'],[role='button'],[role='combobox'],[role='option'],[role='checkbox'],[role='radio'],[role='alert'],[role='status'],[role='dialog'],[role='row'],[role='gridcell'],tr,td,li,h1,h2,h3,h4`;
                // Shadow-root controls are part of the same observable page
                // even though document.querySelectorAll cannot cross the
                // boundary. Include open roots as fresh evidence; closed
                // roots remain represented by their host geometry/text.
                const roots = [document];
                const hostSample = Array.from(document.querySelectorAll('body *')).slice(0, 1200);
                for (const host of hostSample) {
                    if (host.shadowRoot) roots.push(host.shadowRoot);
                }
                const nodes = roots.flatMap(root => Array.from(root.querySelectorAll(selector))).slice(0, 2000);
                return nodes.map((node, index) => ({
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
            }""",
                timeout=15,
            )
        except TimeoutError:
            raw = []
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
        # CRM/SPA shells often render primary destinations as plain clickable
        # text inside aside/nav containers rather than semantic anchors. When
        # the standard inventory yields no same-origin routes, harvest a
        # bounded set of visible nav-like controls so objective ranking can
        # still probe the requested area.
        if not any(item.href for item in elements):
            try:
                nav_raw = await _bounded_evaluate(
                    """() => {
                    const roots = [
                        ...Array.from(document.querySelectorAll('aside,nav,header,[role="navigation"],[class*="sidebar" i],[class*="sidenav" i],[class*="menu" i]')).slice(0, 40),
                        document.body,
                    ];
                    const seen = new Set();
                    const out = [];
                    for (const root of roots) {
                        if (!root) continue;
                        for (const node of Array.from(root.querySelectorAll('a,button,[role="link"],[role="button"],[role="menuitem"],[role="tab"],li,div,span')).slice(0, 500)) {
                            const text = (node.innerText || node.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
                            if (!text || text.length < 2 || text.length > 48) continue;
                            if (text.split(' ').length > 6) continue;
                            const visible = !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
                            if (!visible) continue;
                            const key = text.toLowerCase();
                            if (seen.has(key)) continue;
                            seen.add(key);
                            const href = node.getAttribute('href') || node.closest('a[href]')?.getAttribute('href') || null;
                            out.push({
                                name: text,
                                tag: node.tagName.toLowerCase(),
                                role: node.getAttribute('role') || (href ? 'link' : 'button'),
                                selector: node.getAttribute('data-testid')
                                    ? `[data-testid="${node.getAttribute('data-testid')}"]`
                                    : node.getAttribute('aria-label')
                                    ? `[aria-label="${node.getAttribute('aria-label')}"]`
                                    : href
                                    ? `a[href="${href}"]`
                                    : node.tagName.toLowerCase(),
                                href,
                                type: node.getAttribute('type'),
                                text,
                                visible: true,
                                navigation_scope: node.closest('aside,nav,[role="navigation"],[class*="sidebar" i]') ? 'primary' : 'unknown',
                                formPriority: 0,
                                required: false,
                                autocomplete: null,
                                options: [],
                                draggable: false,
                                dropzone: false,
                                shadowRoot: !!node.shadowRoot,
                            });
                            if (out.length >= 60) return out;
                        }
                    }
                    return out;
                }""",
                    timeout=10,
                )
            except TimeoutError:
                nav_raw = []
            for item in nav_raw if isinstance(nav_raw, list) else []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                elements.append(
                    ObservedElement(
                        tag=str(item.get("tag") or "button"),
                        role=item.get("role"),
                        name=name[:300],
                        selector=str(item.get("selector") or "button"),
                        href=item.get("href"),
                        element_type=item.get("type"),
                        required=False,
                        options=[],
                        autocomplete=None,
                        text=item.get("text"),
                        source_url=page.url,
                        actionable=True,
                        navigation_scope=str(item.get("navigation_scope") or "unknown"),
                        draggable=False,
                        dropzone=False,
                        shadow_host=bool(item.get("shadowRoot")),
                    )
                )
        # Always harvest create/add CTAs. Interactive CRM toolbars often render
        # "New Lead" outside the primary inventory slice or as non-semantic
        # clickable text; missing them leaves discovery with only template
        # forms on sibling routes.
        try:
            cta_raw = await _bounded_evaluate(
                """() => {
                    const needles = /\\b(new|create|add|compose|schedule|book)\\b/i;
                    const nodes = Array.from(document.querySelectorAll(
                        'a,button,[role="button"],[role="link"],[class*="btn" i],[class*="button" i]'
                    )).slice(0, 800);
                    const out = [];
                    const seen = new Set();
                    for (const node of nodes) {
                        const text = (node.innerText || node.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
                        if (!text || text.length > 48 || !needles.test(text)) continue;
                        const visible = !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
                        if (!visible) continue;
                        const key = text.toLowerCase();
                        if (seen.has(key)) continue;
                        seen.add(key);
                        out.push({
                            name: text,
                            tag: node.tagName.toLowerCase(),
                            role: node.getAttribute('role') || 'button',
                            selector: node.getAttribute('data-testid')
                                ? `[data-testid="${node.getAttribute('data-testid')}"]`
                                : node.getAttribute('aria-label')
                                ? `[aria-label="${node.getAttribute('aria-label')}"]`
                                : node.tagName.toLowerCase(),
                            href: node.getAttribute('href'),
                            type: node.getAttribute('type') || 'button',
                            text,
                            visible: true,
                            navigation_scope: 'unknown',
                            formPriority: 1,
                            required: false,
                            autocomplete: null,
                            options: [],
                            draggable: false,
                            dropzone: false,
                            shadowRoot: !!node.shadowRoot,
                        });
                        if (out.length >= 20) break;
                    }
                    return out;
                }""",
                timeout=8,
            )
        except TimeoutError:
            cta_raw = []
        existing_names = {item.name.casefold() for item in elements}
        for item in cta_raw if isinstance(cta_raw, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or name.casefold() in existing_names:
                continue
            existing_names.add(name.casefold())
            elements.append(
                ObservedElement(
                    tag=str(item.get("tag") or "button"),
                    role=item.get("role"),
                    name=name[:300],
                    selector=str(item.get("selector") or "button"),
                    href=item.get("href"),
                    element_type=item.get("type"),
                    required=False,
                    options=[],
                    autocomplete=None,
                    text=item.get("text"),
                    source_url=page.url,
                    actionable=True,
                    navigation_scope=str(item.get("navigation_scope") or "unknown"),
                    draggable=False,
                    dropzone=False,
                    shadow_host=bool(item.get("shadowRoot")),
                )
            )
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
