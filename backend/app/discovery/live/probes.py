"""Reversible capability probes during live discovery."""

from __future__ import annotations

import asyncio

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.contracts.models import (
    ActionCapability,
    FormSchema,
    ObservedElement,
    ProductContext,
)
from app.discovery.live.helpers import *


class CapabilityProbeMixin:
    async def _probe_reversible_capabilities(
        self,
        page,
        context: ProductContext,
        objective: str,
        *,
        remaining: int,
    ) -> tuple[list[ActionCapability], list[str], list[str]]:
        """Open a few reversible product controls to learn their real schema.

        A page title or a visible "Create" button is not workflow evidence.
        This probe is deliberately constrained to non-link controls with a
        reversible intent, and it never fills or submits.  It gives planning
        a generic form/modal capability without turning discovery into a
        crawler or changing product data.
        """
        if remaining <= 0:
            return [], [], []
        wanted = _tokens(objective)
        ranked: list[tuple[int, ObservedElement]] = []
        for item in context.elements:
            if not item.actionable or item.href or item.tag not in {"button", "input"}:
                continue
            words = _tokens(f"{item.name} {item.text or ''}")
            if words & _UNSAFE_ACTION_WORDS or not (words & _REVERSIBLE_ACTION_WORDS):
                continue
            ranked.append((len(words & wanted) * 3 + len(words & _REVERSIBLE_ACTION_WORDS), item))
        capabilities: list[ActionCapability] = []
        actions: list[str] = []
        blockers: list[str] = []
        for _, item in sorted(ranked, key=lambda candidate: -candidate[0])[:remaining]:
            prior_url = page.url
            try:
                visible = await self._visible_semantic_control(page, item)
                dom_clicked = False
                if visible is None:
                    dom_clicked = await self._dom_semantic_click(page, item)
                if visible is None and not dom_clicked:
                    continue
                # A visible control can still be completing a design-system
                # entrance/portal transition immediately after a route probe.
                # Give the browser one bounded paint interval before deciding
                # it is non-actionable; production has its own stricter scene
                # readiness checks and never relies on this discovery timing.
                await page.wait_for_timeout(500)
                try:
                    before_editable_count = await asyncio.wait_for(
                        page.evaluate(
                            """() => Array.from(document.querySelectorAll(
                                'input, textarea, select, [contenteditable="true"], [role="textbox"], [role="combobox"]'
                            )).filter(node => {
                                const rect = node.getBoundingClientRect();
                                return !!(rect.width || rect.height || node.getClientRects().length);
                            }).length"""
                        ),
                        timeout=3,
                    )
                except (PlaywrightError, TimeoutError):
                    before_editable_count = 0
                if not dom_clicked:
                    try:
                        await visible.click(timeout=5_000)
                    except PlaywrightTimeoutError:
                        # This remains a safe, pre-submit exploration click on
                        # an already verified visible semantic control. A
                        # single force attempt handles transient animation
                        # wrappers without broadening to coordinates.
                        await visible.click(timeout=3_000, force=True)
                await page.wait_for_timeout(500)
                # Resolve the newly opened surface without counting large
                # portal/virtualized subtrees (``count`` can block a remote
                # CDP target while the SPA is re-rendering).  The first
                # visible semantic surface is sufficient because the click
                # itself was already ranked and grounded.
                scope = None
                for candidate in (
                    page.get_by_role("dialog").first,
                    page.locator("[aria-modal='true']").first,
                    page.locator("form").first,
                    page.locator(
                        "[class*='drawer' i], [class*='modal' i], [class*='panel' i]"
                    ).first,
                ):
                    try:
                        if await asyncio.wait_for(candidate.is_visible(), timeout=1.5):
                            scope = candidate
                            break
                    except (PlaywrightError, TimeoutError):
                        continue
                if scope is None:
                    # Plain portals may expose no semantic form surface.  A
                    # positive editable-control delta is direct evidence that
                    # the reversible entry click opened a workflow; use a
                    # bounded body scope instead of a product-specific class.
                    try:
                        after_editable_count = await asyncio.wait_for(
                            page.evaluate(
                                """() => Array.from(document.querySelectorAll(
                                    'input, textarea, select, [contenteditable="true"], [role="textbox"], [role="combobox"]'
                                )).filter(node => {
                                    const rect = node.getBoundingClientRect();
                                    return !!(rect.width || rect.height || node.getClientRects().length);
                                }).length"""
                            ),
                            timeout=3,
                        )
                    except (PlaywrightError, TimeoutError):
                        after_editable_count = before_editable_count
                    if after_editable_count > 0:
                        scope = page.locator("body")
                if scope is None:
                    if _canonical_route(page.url) != _canonical_route(prior_url):
                        try:
                            await page.go_back(wait_until="domcontentloaded", timeout=15_000)
                        except PlaywrightError:
                            await self._safe_escape(page)
                    else:
                        await self._safe_escape(page)
                    continue
                try:
                    observed = await asyncio.wait_for(
                        self.inspect(page, objective), timeout=30
                    )
                except TimeoutError:
                    blockers.append(f"capability_probe_inspect_timeout:{item.name}")
                    await self._safe_escape(page)
                    continue
                try:
                    schema: FormSchema = await asyncio.wait_for(
                        self._form_schema_from_scope(scope, page.url), timeout=20
                    )
                except TimeoutError:
                    blockers.append(f"capability_probe_schema_timeout:{item.name}")
                    schema = FormSchema(source_url=page.url, fields=[], evidence=[])
                # Enrichment opens choice lists and can consume the whole probe
                # budget on dense CRM forms. Prefer a shallow labelled schema
                # over discarding a successfully opened create surface.
                if schema.fields:
                    try:
                        schema = await asyncio.wait_for(
                            self._enrich_choice_options(page, schema), timeout=35
                        )
                    except TimeoutError:
                        blockers.append(f"capability_probe_enrich_timeout:{item.name}")
                        # A partial enrich is still useful: finish only the
                        # selectable fields that would otherwise block rehearsal
                        # compilation (non-optional selects without choices).
                        try:
                            critical = schema.model_copy(
                                update={
                                    "fields": [
                                        field
                                        for field in schema.fields
                                        if field.control_type.casefold()
                                        in {"select", "combobox"}
                                        and not field.options
                                        and "optional" not in " ".join(field.validation_messages).casefold()
                                    ][:3]
                                }
                            )
                            if critical.fields:
                                enriched_critical = await asyncio.wait_for(
                                    self._enrich_choice_options(page, critical), timeout=30
                                )
                                options_by_name = {
                                    field.name.casefold(): field.options
                                    for field in enriched_critical.fields
                                    if field.options
                                }
                                schema = schema.model_copy(
                                    update={
                                        "fields": [
                                            field.model_copy(
                                                update={
                                                    "options": options_by_name.get(
                                                        field.name.casefold(), field.options
                                                    )
                                                }
                                            )
                                            for field in schema.fields
                                        ]
                                    }
                                )
                        except TimeoutError:
                            blockers.append(
                                f"capability_probe_critical_enrich_timeout:{item.name}"
                            )
                # A portal can expose a visible shell (or even a semantic
                # dialog) while rendering its actual controls in a sibling
                # subtree.  If the click produced a positive editable-control
                # delta and the selected shell yielded no fields, re-scan the
                # bounded page body as the evidence scope.  This remains
                # generic and state-grounded; it never invents selectors.
                if not schema.fields:
                    try:
                        after_editable_count = await asyncio.wait_for(
                            page.evaluate(
                                """() => Array.from(document.querySelectorAll(
                                    'input, textarea, select, [contenteditable="true"], [role="textbox"], [role="combobox"]'
                                )).filter(node => {
                                    const rect = node.getBoundingClientRect();
                                    return !!(rect.width || rect.height || node.getClientRects().length);
                                }).length"""
                            ),
                            timeout=3,
                        )
                    except (PlaywrightError, TimeoutError):
                        after_editable_count = before_editable_count
                    if after_editable_count > 0:
                        try:
                            schema = await asyncio.wait_for(
                                self._form_schema_from_scope(page.locator("body"), page.url),
                                timeout=20,
                            )
                        except TimeoutError:
                            blockers.append(f"capability_probe_body_schema_timeout:{item.name}")
                            schema = FormSchema(source_url=page.url, fields=[], evidence=[])
                        if schema.fields:
                            try:
                                schema = await asyncio.wait_for(
                                    self._enrich_choice_options(page, schema), timeout=20
                                )
                            except TimeoutError:
                                blockers.append(
                                    f"capability_probe_body_enrich_timeout:{item.name}"
                                )
                if not schema.fields:
                    await self._safe_escape(page)
                    continue
                try:
                    schema = await asyncio.wait_for(
                        self._probe_form_dependencies(page, scope, schema), timeout=45
                    )
                except TimeoutError:
                    # Dependency transitions are useful but optional. Keep the
                    # grounded fields so rehearsal can still exercise the form.
                    blockers.append(f"capability_probe_dependency_timeout:{item.name}")
                submit_target = None
                close_target = None
                for candidate in observed.elements:
                    name = candidate.name.lower()
                    # The entry control itself (for example ``New ...``) can
                    # remain mounted behind a portal and match the generic
                    # create/add vocabulary.  It opens the surface; it is not
                    # the submit action.  Exclude that exact semantic control
                    # so rehearsal cannot dispatch the entry click twice.
                    if name == item.name.casefold():
                        continue
                    if (
                        candidate.tag in {"button", "input"}
                        and submit_target is None
                        and (
                            "submit" in name
                            or "save" in name
                            or "create" in name
                            or "add" in name
                            or name == "new lead"
                        )
                    ):
                        submit_target = _capability_target(candidate)
                    if (
                        candidate.tag == "button"
                        and close_target is None
                        and any(word in name for word in ("close", "cancel", "dismiss"))
                    ):
                        close_target = _capability_target(candidate)
                # Dialog submit controls are often labelled Save/Create inside
                # the open form rather than in the pre-click page inventory.
                if submit_target is None:
                    try:
                        dialog_submits = await asyncio.wait_for(
                            page.locator(
                                "[role='dialog'] button, [aria-modal='true'] button, form button[type='submit']"
                            ).evaluate_all(
                                """nodes => nodes.map(node => {
                                    const text = (node.innerText || node.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
                                    const visible = !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
                                    return {text, visible, disabled: !!node.disabled};
                                }).filter(item => item.visible && item.text && !item.disabled).slice(0, 20)"""
                            ),
                            timeout=5,
                        )
                    except (PlaywrightError, TimeoutError):
                        dialog_submits = []
                    for candidate in dialog_submits if isinstance(dialog_submits, list) else []:
                        label = str(candidate.get("text") or "").casefold()
                        if any(
                            word in label
                            for word in ("save", "create", "submit", "add", "continue")
                        ):
                            submit_target = _capability_target(
                                ObservedElement(
                                    tag="button",
                                    role="button",
                                    name=str(candidate.get("text") or "Save")[:200],
                                    selector="button",
                                    source_url=page.url,
                                    actionable=True,
                                )
                            )
                            break
                if close_target is None:
                    await self._safe_escape(page)
                else:
                    closer = page.get_by_role("button", name=close_target.name, exact=True)
                    try:
                        closer_count = await asyncio.wait_for(closer.count(), timeout=3)
                    except (PlaywrightError, TimeoutError):
                        closer_count = 0
                    if closer_count:
                        try:
                            await closer.first.click(timeout=3_000)
                        except PlaywrightError:
                            await self._safe_escape(page)
                    else:
                        await self._safe_escape(page)
                await page.wait_for_timeout(250)
                capabilities.append(
                    ActionCapability(
                        kind="form",
                        purpose=item.name,
                        source_url=prior_url,
                        entry_target=_capability_target(item),
                        form_schema=schema,
                        submit_target=submit_target,
                        close_target=close_target,
                        evidence_refs=[f"capability:{prior_url}:{item.name}", *schema.evidence],
                        # Discovery proves only that a reversible form can be
                        # opened.  It must never imply that submission succeeds
                        # or that a new record has appeared; rehearsal promotes
                        # it only after an independent visible outcome witness.
                        verified=False,
                    )
                )
                actions.append(f"reversible_form_probe:{item.name}")
            except PlaywrightError as error:
                blockers.append(f"capability_probe_failed:{item.name}:{str(error)[:120]}")
                await self._safe_escape(page)
        return capabilities, actions, blockers

__all__ = [
    "CapabilityProbeMixin",
]
