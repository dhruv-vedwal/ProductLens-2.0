from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from pydantic import ValidationError

from app.artifacts.store import RunArtifacts
from app.contracts.models import (
    ActionCapability,
    FormField,
    ObservedElement,
    OperationKind,
    Postcondition,
    ProductContext,
    SemanticOperation,
    Target,
)
from app.execution.playwright_adapter import GroundingError, PlaywrightAdapter
from app.planning.capabilities import (
    CapabilityCompilationError,
    compile_rehearsal_operations,
)
from app.planning.rehearsal import (
    CapabilitySelectionError,
    derive_outcome_witness,
    select_rehearsal_capability,
)
from app.planning.side_effects import SideEffectPolicyError, authorize_operation
from app.planning.synthetic import SyntheticDataError, hydrate_operations
from app.services.generation_policy import (
    canonical_url as _canonical_url,
)
from app.services.generation_policy import (
    normalise_observed_selector as _normalise_observed_selector,
)

from .render import GenerationPreconditionError


async def _rehearsal_outcome_candidates(
    page,
    *,
    submitted_values: list[str] | None = None,
) -> list[ObservedElement]:
    """Collect bounded post-submit result evidence outside the broad page scan.

    A normal discovery scan favours navigation and form controls. Once a form
    closes, a new row may be far enough down a dense DOM to fall outside that
    bounded list even though it is the only valid independent result witness.
    This collector is read-only and deliberately limited to confirmation and
    result structures; it never turns arbitrary page prose into an outcome.
    """
    raw = await page.locator(
        "[role='alert'],[role='status'],[role='row'],[role='gridcell'],tr,td,li"
    ).evaluate_all(
        """nodes => nodes.map(node => {
            const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
            const visible = !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
            const stableId = node.id && !/^:r[0-9a-z]+:$/i.test(node.id);
            const selector = node.getAttribute('data-testid') ? `[data-testid="${CSS.escape(node.getAttribute('data-testid'))}"]` :
                stableId ? `#${CSS.escape(node.id)}` : node.tagName.toLowerCase();
            return {tag: node.tagName.toLowerCase(), role: node.getAttribute('role') || '', text, visible, selector};
        }).filter(item => item.visible && item.text && item.text.length <= 700).slice(0, 240)"""
    )
    candidates = [
        ObservedElement(
            tag=str(item["tag"]),
            role=str(item["role"]) or None,
            name=str(item["text"])[:300],
            text=str(item["text"])[:700],
            selector=str(item["selector"]),
            source_url=page.url,
            actionable=False,
        )
        for item in raw
    ]
    # Data-heavy applications frequently render result cards with plain divs
    # instead of table/list ARIA. A submitted synthetic value is uniquely
    # generated for this rehearsal, so use it only to find a visible stable
    # result container—not to promote arbitrary page prose into an outcome.
    compact_values = [
        re.sub(r"[^a-z0-9]", "", str(value).casefold())
        for value in (submitted_values or [])
        if len(re.sub(r"[^a-z0-9]", "", str(value))) >= 5
    ]
    if compact_values:
        value_nodes = await page.locator("body *").evaluate_all(
            """(nodes, submitted) => {
                const compact = value => (value || '').toLowerCase().replace(/[^a-z0-9]/g, '');
                const visible = node => !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
                const stableSelector = node => {
                    if (node.dataset && node.dataset.testid) return `[data-testid="${CSS.escape(node.dataset.testid)}"]`;
                    if (node.id && !/^:r[0-9a-z]+:$/i.test(node.id)) return `#${CSS.escape(node.id)}`;
                    return '';
                };
                const result = [];
                for (const node of nodes) {
                    if (!visible(node)) continue;
                    const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                    const normalized = compact(text);
                    if (!text || text.length > 700 || !submitted.some(value => normalized.includes(value))) continue;
                    if ([...node.children].some(child => compact(child.innerText || '').includes(submitted.find(value => normalized.includes(value)) || ''))) continue;
                    let container = node;
                    for (let depth = 0; depth < 5 && container.parentElement; depth += 1) {
                        const selector = stableSelector(container);
                        const role = container.getAttribute('role') || '';
                        if (selector || ['row', 'gridcell', 'listitem', 'alert', 'status'].includes(role) || ['TR', 'TD', 'LI'].includes(container.tagName)) break;
                        container = container.parentElement;
                    }
                    const selector = stableSelector(container);
                    result.push({ tag: container.tagName.toLowerCase(), role: container.getAttribute('role') || '', text: (container.innerText || text).replace(/\\s+/g, ' ').trim(), visible: true, selector: selector || container.tagName.toLowerCase() });
                    if (result.length >= 24) break;
                }
                return result;
            }""",
            compact_values,
        )
        candidates.extend(
            ObservedElement(
                tag=str(item["tag"]),
                role=str(item["role"]) or None,
                name=str(item["text"])[:300],
                text=str(item["text"])[:700],
                selector=str(item["selector"]),
                source_url=page.url,
                actionable=False,
            )
            for item in value_nodes
        )
    return candidates

async def _rehearsal_post_submit_state(page) -> dict[str, int]:
    """Return structural, non-content diagnostics for a rejected rehearsal."""
    return {
        "visible_dialog_count": await page.locator("[role='dialog']").evaluate_all(
            "nodes => nodes.filter(node => !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length)).length"
        ),
        "invalid_field_count": await page.locator(
            "[aria-invalid='true'],input:invalid,select:invalid,textarea:invalid"
        ).count(),
        "visible_alert_count": await page.locator("[role='alert']").evaluate_all(
            "nodes => nodes.filter(node => !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length)).length"
        ),
    }

def _rehearsal_detail_navigation_witness(
    capability: ActionCapability,
    *,
    before_url: str | None,
    after_url: str,
) -> ActionCapability | None:
    """Prove creation from the application's post-submit detail navigation.

    Many applications intentionally navigate to the newly-created entity's
    detail screen instead of showing a toast or returning to a filtered list.
    That route transition is a stronger independent witness than page text,
    but only when it remains on the same origin and changes the canonical path.
    No product-specific route shape or entity name is assumed here.
    """
    if not before_url:
        return None
    before = urlsplit(before_url)
    after = urlsplit(after_url)
    if (
        not before.scheme
        or not before.netloc
        or (before.scheme, before.netloc) != (after.scheme, after.netloc)
        or _canonical_url(before_url) == _canonical_url(after_url)
    ):
        return None
    # Queries/fragments may contain transient UI state. The path is sufficient
    # for the future production postcondition and avoids persisting it.
    verified_url = urlunsplit((after.scheme, after.netloc, after.path, "", ""))
    return capability.model_copy(
        update={
            "verified": True,
            "outcome_target": Target(
                name="verified created record",
                source_url=verified_url,
                actionable=False,
            ),
            "outcome_evidence": [
                *[
                    evidence
                    for evidence in capability.outcome_evidence
                    if not evidence.startswith("rehearsal-visible-outcome:")
                ],
                "rehearsal-visible-outcome:post-submit-detail-navigation",
            ],
        }
    )

def _rehearsal_witness_is_reusable(capability: ActionCapability) -> bool:
    """Reject legacy/weak witnesses before they can poison a production plan."""
    target = capability.outcome_target
    if target is None or not capability.verified:
        return False
    # A header/table transcript can be structurally visible while proving
    # nothing was created. It is also not a groundable target in a fresh
    # production context. Durable witnesses need either a stable selector,
    # semantic role, or a concise visible value that can be re-grounded.
    text = (target.text or target.name or "").strip()
    if not target.selector and not target.role and text.startswith("#"):
        return False
    if target.role == "dialog" and len(re.findall(r"[A-Za-z0-9]{2,}", text)) > 6:
        return False
    if (
        target.role == "dialog"
        and capability.form_schema is not None
        and not any(
            "rehearsal:include" in field.validation_messages
            for field in capability.form_schema.fields
        )
    ):
        # A verified dependent form must carry the controls learned during
        # rehearsal; a schema without promotion markers is an older, shallow
        # witness and must be refreshed before production reuse.
        return False
    if (
        target.role == "dialog"
        and capability.form_schema is not None
        and any(
            "rehearsal:include" in field.validation_messages
            for field in capability.form_schema.fields
        )
        and not any(
            field.required
            and field.control_type.casefold() not in {"select", "combobox", "radio", "checkbox"}
            for field in capability.form_schema.fields
        )
    ):
        return False
    if capability.form_schema is not None and any(
        field.selector and field.selector.count("[") != field.selector.count("]")
        for field in capability.form_schema.fields
    ):
        return False
    if len(re.findall(r"[A-Za-z0-9]{2,}", text)) > 24 and not target.selector:
        return False
    return bool(target.selector or target.role or len(text) >= 5)

async def _rehearsal_form_readiness(page, submit_target: Target | None) -> dict[str, int | bool]:
    """Check visible form readiness before an authorised submit is dispatched."""
    invalid = await page.locator(
        "[aria-invalid='true'],input:invalid,select:invalid,textarea:invalid"
    ).count()
    disabled_submit = False
    if submit_target is not None:
        try:
            if submit_target.selector:
                control = page.locator(submit_target.selector)
            else:
                control = page.get_by_role(
                    "button", name=re.compile(re.escape(submit_target.name), re.IGNORECASE)
                )
            if await control.count() == 1 and await control.is_visible():
                disabled_submit = await control.is_disabled()
        except PlaywrightError:
            # Readiness is an extra precondition. A target resolution failure
            # remains owned by the semantic executor and does not broaden a
            # submit permission here.
            pass
    return {"invalid_field_count": invalid, "submit_disabled": disabled_submit}

async def _rehearsal_validation_recovery_operations(
    page,
    *,
    source_url: str,
    capability: ActionCapability,
) -> list[SemanticOperation]:
    """Derive bounded edits for controls revealed by native/custom validation.

    Forms frequently discover dependencies only after the first submit (for
    example a doctor becomes required after a branch is chosen).  Discovery's
    initial schema therefore cannot be the only source of truth.  Re-read the
    *visible invalid controls* and compile semantic operations from their live
    labels, names, roles and observed options.  This is deliberately product
    neutral: no field names, route fragments, or application selectors are
    embedded here.
    """
    controls = await page.locator(
        "[aria-invalid='true'], input:invalid, select:invalid, textarea:invalid, [aria-required='true'], [required], [role='dialog'] input:not([type='hidden']):not([type='checkbox']), [role='dialog'] select, [role='dialog'] textarea, [role='dialog'] [role='combobox'], [role='dialog'] button"
    ).evaluate_all(
        """nodes => {
          const visible = node => {
            const r = node.getBoundingClientRect();
            return !!(r.width || r.height || node.getClientRects().length);
          };
          const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
          const esc = value => (globalThis.CSS && CSS.escape)
            ? CSS.escape(String(value)) : String(value).replace(/[^a-zA-Z0-9_-]/g, '_');
          const labelFor = node => {
            const labelled = node.getAttribute('aria-label') || node.getAttribute('placeholder');
            if (labelled) return clean(labelled);
            const id = node.getAttribute('id');
            const associated = id && document.querySelector(`label[for="${esc(id)}"]`);
            if (associated) return clean(associated.innerText);
            const parent = node.closest('label, [data-field], [class*="field" i], [class*="form-group" i]');
            const text = parent ? clean(parent.innerText) : '';
            return clean(text.split(/\\n/)[0] || node.getAttribute('name') || node.tagName);
          };
          const selectorFor = node => {
            const type = (node.getAttribute('type') || '').toLowerCase();
            const radioName = node.getAttribute('name');
            const radioValue = node.getAttribute('value');
            if ((type === 'radio' || type === 'checkbox') && radioName && radioValue) {
              return `input[type="${type}"][name="${String(radioName).replace(/\\"/g, '\\\\"')}"][value="${String(radioValue).replace(/\\"/g, '\\\\"')}"]`;
            }
            for (const [attribute, prefix] of [['data-testid','[data-testid="'], ['name','[name="']]) {
              const value = node.getAttribute(attribute);
              if (value) return `${prefix}${String(value).replace(/\\"/g, '\\\\"')}"]`;
            }
            const id = node.getAttribute('id');
            return id ? `#${esc(id)}` : null;
          };
          const seenRadioGroups = new Set();
          return nodes.slice(0, 32).map(node => {
            // Form libraries often mark a hidden bookkeeping input invalid
            // while the user-facing combobox/input is its visible sibling.
            // Rebind to that visible semantic control before compiling an
            // operation; never dispatch against the hidden validator node.
            const actual = visible(node) ? node : (node.closest('label, [data-field], [class*="field" i], [class*="form-group" i]') || node.parentElement)?.querySelector('input:not([type="hidden"]), select, textarea, [role="combobox"]');
            if (!actual || !visible(actual)) return null;
            node = actual;
            if (node.disabled || node.getAttribute('aria-disabled') === 'true') return null;
            const inputType = (node.getAttribute('type') || '').toLowerCase();
            if (inputType === 'radio') {
              const group = node.getAttribute('name') || node.getAttribute('id') || 'radio-group';
              if (seenRadioGroups.has(group)) return null;
              seenRadioGroups.add(group);
            }
            const tag = node.tagName.toLowerCase();
            const type = (node.getAttribute('type') || tag).toLowerCase();
            const role = node.getAttribute('role') || (tag === 'select' ? 'combobox' : '');
            const optionNodes = tag === 'select'
              ? Array.from(node.options || [])
              : Array.from(document.querySelectorAll('[role="option"]')).filter(visible);
            const options = optionNodes.map(option => clean(option.innerText || option.textContent || option.value)).filter(Boolean).slice(0, 20);
            return {label: labelFor(node), name: node.getAttribute('name') || '', value: node.getAttribute('value') || '', tag, type, role, selector: selectorFor(node), options, text: clean(node.innerText || node.textContent)};
          }).filter(Boolean);
        }"""
    )
    if not controls:
        return []
    known = {
        re.sub(r"[^a-z0-9]", "", field.name.casefold()): field
        for field in (capability.form_schema.fields if capability.form_schema else [])
    }
    operations: list[SemanticOperation] = []
    for item in controls:
        name = str(item.get("label") or item.get("name") or item.get("text") or "field").strip()[
            :160
        ]
        selector = item.get("selector")
        if not selector:
            continue
        compact_name = re.sub(r"[^a-z0-9]", "", name.casefold())
        compact_attr_name = re.sub(r"[^a-z0-9]", "", str(item.get("name") or "").casefold())
        field = known.get(compact_name) or known.get(compact_attr_name)
        if field is None:
            field = next(
                (
                    value
                    for key, value in known.items()
                    if compact_name in key
                    or key in compact_name
                    or (
                        compact_attr_name and (compact_attr_name in key or key in compact_attr_name)
                    )
                ),
                None,
            )
        control_type = str(item.get("type") or item.get("tag") or "text").casefold()
        if compact_attr_name in {"type", "id"} or compact_name in {"type", "id"}:
            continue
        if str(item.get("tag") or "").casefold() == "button":
            if not (
                re.search(r"date|time|calendar|picker", name, re.IGNORECASE)
                or str(item.get("role") or "").casefold() == "gridcell"
            ):
                continue
            operations.append(
                SemanticOperation(
                    kind=OperationKind.CLICK,
                    intent=f"Open or choose the observed {name} control revealed by validation",
                    target=Target(
                        name=name,
                        role="gridcell"
                        if str(item.get("role") or "").casefold() == "gridcell"
                        else "button",
                        label=name,
                        text=name,
                        selector=str(selector),
                        source_url=source_url,
                    ),
                    story_phase="demonstrate",
                    page_url=source_url,
                    evidence_refs=[f"live-validation:{source_url}:{name}"],
                )
            )
            continue
        if control_type in {"checkbox", "hidden"}:
            # Checkboxes and hidden bookkeeping controls need a control-level
            # policy; a broad dialog inventory cannot safely toggle them.
            continue
        role = str(item.get("role") or "") or None
        target = Target(
            name=name,
            label=name,
            selector=str(selector),
            role=role,
            source_url=source_url,
        )
        if control_type in {"checkbox", "radio"}:
            kind = OperationKind.CHECK if control_type == "checkbox" else OperationKind.CHOOSE_RADIO
            value = True
        elif control_type in {"select", "combobox"} or role == "combobox":
            choices = [
                str(value).strip()
                for value in (item.get("options") or (field.options if field else []))
                if str(value).strip().casefold()
                not in {"select", "select an option", "choose", "choose an option"}
            ]
            if not choices:
                # A custom combobox may hide its listbox until focused. It is
                # safer to leave it for a later targeted re-exploration than
                # to type a value that the widget cannot commit.
                continue
            kind, value = OperationKind.SELECT_OPTION, choices[0]
        elif "email" in control_type or "email" in name.casefold():
            kind, value = OperationKind.FILL_EMAIL, None
        elif any(
            token in f"{control_type} {name.casefold()}"
            for token in ("phone", "mobile", "telephone", "tel")
        ):
            kind, value = OperationKind.FILL_PHONE, None
        elif (
            control_type in {"date", "time"}
            or "date" in name.casefold()
            or "time" in name.casefold()
            or re.search(r"\b(?:dd|mm|yyyy)[-/]", name.casefold())
        ):
            kind = OperationKind.SELECT_DATE
            # Preserve the format advertised by the live control (native date
            # inputs accept ISO, while design-system text pickers commonly use
            # dd-mm-yyyy). The value remains synthetic and deterministic.
            if re.search(r"dd[-/]mm[-/]yyyy", name.casefold()):
                value = (datetime.now(UTC).date() + timedelta(days=10)).strftime("%d-%m-%Y")
            else:
                value = None
        else:
            kind, value = OperationKind.FILL_TEXT, None
        operations.append(
            SemanticOperation(
                kind=kind,
                intent=f"Resolve the observed validation requirement for {name}",
                target=target,
                value=value,
                postconditions=[Postcondition(kind="value", expected=value, target=target)]
                if kind
                in {
                    OperationKind.FILL_TEXT,
                    OperationKind.FILL_EMAIL,
                    OperationKind.FILL_PHONE,
                    OperationKind.SELECT_DATE,
                }
                else [],
                story_phase="demonstrate",
                page_url=source_url,
                evidence_refs=[f"live-validation:{source_url}:{name}"],
            )
        )
    try:
        # Validation often reports a disabled date before the radio/select that
        # enables it. Execute observed prerequisite choices first, then text,
        # and finally date/time controls. This is a generic dependency-safe
        # ordering; it does not name any product field or route.
        operations.sort(
            key=lambda operation: (
                0
                if operation.kind in {OperationKind.CHOOSE_RADIO, OperationKind.CHECK}
                else 1
                if operation.kind is OperationKind.SELECT_OPTION
                else 3
                if operation.kind is OperationKind.SELECT_DATE
                else 2
            )
        )
        hydrated, _ = hydrate_operations(
            operations,
            product_key=source_url,
            forbidden_values=set(capability.outcome_evidence),
        )
    except SyntheticDataError:
        # A missing synthetic value is a bounded recovery miss, not permission
        # to invent a credential or bypass the form's validation contract.
        return []
    return hydrated

async def _wait_for_rehearsal_outcome(
    page, *, source_url: str, submitted_values: list[str]
) -> list[ObservedElement]:
    """Find a submitted record through the current or a safe reset list state."""

    async def collect() -> list[ObservedElement]:
        deadline = perf_counter() + 8
        candidates: list[ObservedElement] = []
        while perf_counter() < deadline:
            candidates = await _rehearsal_outcome_candidates(
                page,
                submitted_values=submitted_values,
            )
            if candidates:
                return candidates
            await page.wait_for_timeout(500)
        return candidates

    candidates = await collect()
    if candidates:
        return candidates
    # A freshly created entity can be excluded by an existing date/status
    # filter. Prefer an observed, reversible UI reset rather than changing a
    # route directly; only a unique visible semantic control is safe here.
    for label in ("Clear filters", "Reset filters", "Clear all", "Reset"):
        control = page.get_by_role("button", name=re.compile(label, re.IGNORECASE))
        if await control.count() != 1 or not await control.is_visible():
            continue
        await control.click()
        await page.wait_for_timeout(700)
        return await collect()
    # If no reset control is available, a query-free same-origin list is a
    # read-only fallback. It is intentionally limited to the discovered
    # source path and never guesses a different product route.
    parsed = urlsplit(source_url)
    if parsed.query:
        await page.goto(
            urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")),
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(700)
        return await collect()
    return candidates

class RehearseMixin:
    async def rehearsal_stage(
        self,
        *,
        run_id: str,
        url: str,
        objective: str,
        artifact_root: Path,
        credential_reference: str | None = None,
        cloud_rehearsal: bool = False,
        allow_isolated_record_creation: bool = False,
    ) -> ProductContext:
        """Authorise one isolated create rehearsal and persist only its proof.

        This is intentionally a fresh, non-recorded browser context. It runs
        before planning, never contributes footage, and promotes a capability
        only when the post-submit DOM contains a new independent witness.
        """
        artifacts = RunArtifacts(artifact_root, run_id)
        context = ProductContext.model_validate(
            json.loads(
                (artifacts.root / "discovery" / "product-context.json").read_text(encoding="utf-8")
            )
        )
        if allow_isolated_record_creation and context.objective is not None:
            # Runtime authorization is auditable and may be granted after a
            # read-only discovery checkpoint. Reuse only the fresh, persisted
            # evidence; do not silently rerun discovery or infer permission
            # from a create-shaped control.
            context = context.model_copy(
                update={
                    "objective": context.objective.model_copy(
                        update={
                            "safe_action_policy": "authorized_side_effects",
                            "permitted_mutations": ["create_isolated_record"],
                        }
                    )
                }
            )
            artifacts.write_json("discovery/product-context.json", context.model_dump(mode="json"))
            artifacts.write_json("objective.json", context.objective.model_dump(mode="json"))
            artifacts.write_json(
                "discovery/mutation-authorization.json",
                {
                    "mutation": "create_isolated_record",
                    "authorization": "explicit_runtime_payload",
                    "recording": "disabled_for_rehearsal",
                },
            )
        if (
            not context.objective
            or "create_isolated_record" not in context.objective.permitted_mutations
        ):
            return context
        capabilities = []
        for raw in context.capabilities:
            try:
                capabilities.append(ActionCapability.model_validate(raw))
            except ValidationError:
                continue
        try:
            candidate = select_rehearsal_capability(context, capabilities)
        except CapabilitySelectionError as error:
            raise GenerationPreconditionError(
                f"REHEARSAL_CAPABILITY_UNAVAILABLE: {error}"
            ) from error
        try:
            operations, rehearsal_dataset = hydrate_operations(
                compile_rehearsal_operations(candidate), product_key=candidate.source_url
            )
        except CapabilityCompilationError as error:
            raise GenerationPreconditionError(f"REHEARSAL_CAPABILITY_INVALID: {error}") from error
        try:
            for operation in operations:
                authorize_operation(operation, True)
        except SideEffectPolicyError as error:
            raise GenerationPreconditionError(f"REHEARSAL_SIDE_EFFECT_BLOCKED: {error}") from error
        attempt_path = artifacts.root / "discovery" / "rehearsal-attempt.json"
        prior_attempt = (
            json.loads(attempt_path.read_text(encoding="utf-8")) if attempt_path.exists() else None
        )
        # A completed verified rehearsal is durable proof for this run.  A
        # planner/re-render retry must not open the form or create a second
        # record merely because an earlier downstream layer was repaired.
        if (
            prior_attempt
            and prior_attempt.get("capability_id") == candidate.id
            and prior_attempt.get("status") == "outcome_verified"
            and _rehearsal_witness_is_reusable(candidate)
        ):
            outcome = candidate.outcome_target
            compact_outcome = re.sub(r"[^a-z0-9]", "", (outcome.text or outcome.name).casefold())
            generated_match = next(
                (
                    value
                    for value in rehearsal_dataset.values()
                    if len(re.sub(r"[^a-z0-9]", "", str(value))) >= 5
                    and re.sub(r"[^a-z0-9]", "", str(value).casefold()) in compact_outcome
                ),
                None,
            )
            if generated_match:
                # Runs created before the neutral structural-witness contract
                # may contain an entire result-row transcript. Migrate it at
                # the durable boundary: retain the generated value needed to
                # locate the result, discard unrelated row columns, and never
                # let a downstream plan or editorial layer rehydrate them.
                candidate = candidate.model_copy(
                    update={
                        "outcome_target": outcome.model_copy(
                            update={
                                "name": "verified created record",
                                "text": str(generated_match),
                            }
                        ),
                        "outcome_evidence": [
                            evidence
                            for evidence in candidate.outcome_evidence
                            if not evidence.startswith("rehearsal-visible-outcome:")
                        ]
                        + ["rehearsal-visible-outcome:verified-created-record"],
                    }
                )
                context = context.model_copy(
                    update={
                        "capabilities": [
                            (candidate if item.id == candidate.id else item).model_dump(mode="json")
                            for item in capabilities
                        ]
                    }
                )
                artifacts.write_json(
                    "discovery/product-context.json", context.model_dump(mode="json")
                )
                artifacts.write_json(
                    "discovery/rehearsal-report.json",
                    {
                        "capability_id": candidate.id,
                        "purpose": candidate.purpose,
                        "outcome_target": candidate.outcome_target.model_dump(mode="json"),
                        "outcome_evidence": candidate.outcome_evidence,
                        "recording": "disabled",
                    },
                )
            context = context.model_copy(
                update={
                    "capabilities": [
                        (candidate if item.id == candidate.id else item).model_dump(mode="json")
                        for item in capabilities
                    ]
                }
            )
            artifacts.write_json(
                "discovery/capabilities.json",
                [
                    (candidate if item.id == candidate.id else item).model_dump(mode="json")
                    for item in capabilities
                ],
            )
            artifacts.write_json("discovery/product-context.json", context.model_dump(mode="json"))
            return context
        recover_dispatched_attempt = bool(
            prior_attempt
            and prior_attempt.get("capability_id") == candidate.id
            and prior_attempt.get("status")
            in {
                "submit_intent_recorded",
                "submit_dispatched",
                "outcome_unverified",
            }
        )
        # A historical attempt that was promoted from a weak witness (for
        # example a table header) is not safe recovery evidence. Re-enter a
        # fresh rehearsal with a new synthetic value rather than treating the
        # old dispatch as authoritative or deriving success from the current
        # list page.
        if prior_attempt and not _rehearsal_witness_is_reusable(candidate):
            recover_dispatched_attempt = False
        # Migrate capabilities produced by the earlier broad-required
        # promotion bug. Keep the one native required field when it is the
        # only such field; when several are marked required they are stale
        # broad inventory and become explicitly promoted optional controls.
        if candidate.form_schema is not None and not any(
            "rehearsal:include" in field.validation_messages
            for field in candidate.form_schema.fields
        ):
            required_count = sum(1 for field in candidate.form_schema.fields if field.required)
            candidate = candidate.model_copy(
                update={
                    "form_schema": candidate.form_schema.model_copy(
                        update={
                            "fields": [
                                field.model_copy(
                                    update={
                                        "required": field.required
                                        if required_count <= 1
                                        else False,
                                        "validation_messages": [
                                            *field.validation_messages,
                                            "rehearsal:include",
                                        ],
                                    }
                                )
                                for field in candidate.form_schema.fields
                            ]
                        }
                    )
                }
            )
        if candidate.form_schema is not None and any(
            "rehearsal:include" in field.validation_messages
            for field in candidate.form_schema.fields
        ):
            candidate = candidate.model_copy(
                update={
                    "form_schema": candidate.form_schema.model_copy(
                        update={
                            "fields": [
                                field.model_copy(
                                    update={
                                        "required": field.required or "*" in field.name,
                                    }
                                )
                                for field in candidate.form_schema.fields
                            ]
                        }
                    )
                }
            )
        rehearsal_operations_used: list[SemanticOperation] = []
        async with async_playwright() as pw:
            browser = await pw.chromium.launch() if not cloud_rehearsal else None
            remote = None
            session = None
            lease_guard: asyncio.Task[None] | None = None
            try:
                if cloud_rehearsal:
                    if self.browserbase_provider is None:
                        raise GenerationPreconditionError(
                            "BROWSERBASE_REQUIRED: cloud rehearsal is not configured"
                        )
                    session = await self.browserbase_provider.create_session_info(
                        viewport={"width": 1440, "height": 900},
                        user_metadata={"productlens_run_id": run_id, "stage": "rehearsal"},
                    )
                    lease_guard = asyncio.create_task(
                        self._release_cloud_session_after(
                            session.session_id,
                            # Rehearsal may include CAPTCHA/authentication and
                            # a form outcome witness; use the same configured
                            # deadline as discovery/capture instead of
                            # evicting a valid long workflow at five minutes.
                            seconds=min(1_500, max(300, self.cloud_capture_timeout_seconds + 120)),
                            reason="rehearsal_stage_deadline",
                            run_id=run_id,
                        )
                    )
                    remote = await asyncio.wait_for(
                        pw.chromium.connect_over_cdp(session.connect_url, timeout=60_000),
                        timeout=65,
                    )
                    browser_context = remote.contexts[0]
                    page = (
                        browser_context.pages[0]
                        if browser_context.pages
                        else await browser_context.new_page()
                    )
                    await page.set_viewport_size({"width": 1440, "height": 900})
                else:
                    assert browser is not None
                    browser_context = await browser.new_context(
                        viewport={"width": 1440, "height": 900}
                    )
                    page = await browser_context.new_page()
                await page.goto(url, wait_until="domcontentloaded")
                await self.credential_service.authenticate_if_required(page, credential_reference)
                if _canonical_url(page.url) != _canonical_url(candidate.source_url):
                    await page.goto(candidate.source_url, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=5_000)
                except PlaywrightTimeoutError:
                    await page.wait_for_timeout(700)
                adapter = PlaywrightAdapter(page, cloud_mode=cloud_rehearsal)
                if recover_dispatched_attempt:
                    # A prior submit may have reached the product while a
                    # worker/provider failure hid its immediate toast or row.
                    # Never replay it. Re-ground the deterministic safe record
                    # read-only in a fresh context instead.
                    outcome_candidates = await _wait_for_rehearsal_outcome(
                        page,
                        source_url=candidate.source_url,
                        submitted_values=list(rehearsal_dataset.values()),
                    )
                    observed_elements = outcome_candidates
                    before_text = ""
                    before_submit_url = None
                else:
                    before_submit_url = None
                    for operation in operations:
                        # The form's own labels and validation copy are not an
                        # outcome. Snapshot immediately before the one authorised
                        # submit so a still-open dialog cannot later be promoted
                        # as a newly created record.
                        if operation.kind is OperationKind.SUBMIT:
                            before_text = (await page.locator("body").inner_text())[:8_000]
                            before_submit_url = page.url
                            artifacts.write_json(
                                "discovery/rehearsal-attempt.json",
                                {
                                    "capability_id": candidate.id,
                                    "status": "submit_intent_recorded",
                                    "recording": "disabled",
                                    "submitted_operation_kinds": [
                                        item.kind.value for item in operations
                                    ],
                                },
                            )
                        # Rehearsal must exercise the same overlay policy as
                        # production. A fresh session can leave a branch/help
                        # dialog over the target; dismiss it semantically
                        # before dispatching the observed operation rather than
                        # forcing a click through the modal.
                        blocker = await adapter.blocking_overlay(operation.target)
                        if blocker:
                            dismissed = await adapter.dismiss_safe_overlay()
                            if not dismissed:
                                raise GenerationPreconditionError(
                                    "REHEARSAL_BLOCKED_BY_OVERLAY: " + blocker[:240]
                                )
                            await page.wait_for_timeout(250)
                        await adapter.execute(operation)
                        if operation.kind is OperationKind.SUBMIT:
                            artifacts.write_json(
                                "discovery/rehearsal-attempt.json",
                                {
                                    "capability_id": candidate.id,
                                    "status": "submit_dispatched",
                                    "recording": "disabled",
                                    "submitted_operation_kinds": [
                                        item.kind.value for item in operations
                                    ],
                                },
                            )
                        await page.wait_for_timeout(300)
                    observed = await self.discovery.inspect(page, objective)
                    outcome_candidates = await _wait_for_rehearsal_outcome(
                        page,
                        source_url=candidate.source_url,
                        submitted_values=list(rehearsal_dataset.values()),
                    )
                    observed_elements = [*observed.elements, *outcome_candidates]
                witnessed = _rehearsal_detail_navigation_witness(
                    candidate,
                    before_url=before_submit_url,
                    after_url=page.url,
                ) or derive_outcome_witness(
                    candidate,
                    before_text=before_text,
                    observed=observed_elements,
                    submitted_values=list(rehearsal_dataset.values()),
                )
                if witnessed is None:
                    # Persist only structural, non-sensitive diagnostics. A
                    # failed witness must be explainable and repairable, but
                    # existing product rows or generated values do not belong
                    # in a failure artifact.
                    post_submit_state = await _rehearsal_post_submit_state(page)
                    validation_unresolved = bool(
                        post_submit_state["visible_dialog_count"]
                        or post_submit_state["invalid_field_count"]
                    )
                    if validation_unresolved and not recover_dispatched_attempt:
                        # Some widgets reveal dependent required controls only
                        # after the first submit. Re-ground the live invalid
                        # controls and retry once; this remains generic and
                        # never replays a successful side effect.
                        all_recovery_operations: list[SemanticOperation] = []
                        for recovery_round in range(3):
                            recovery_operations = await _rehearsal_validation_recovery_operations(
                                page,
                                source_url=candidate.source_url,
                                capability=candidate,
                            )
                            artifacts.write_json(
                                "discovery/rehearsal-validation-controls.json",
                                {
                                    "capability_id": candidate.id,
                                    "round": recovery_round + 1,
                                    "controls": [
                                        {
                                            "name": operation.target.name
                                            if operation.target
                                            else None,
                                            "kind": operation.kind.value,
                                            "has_value": operation.value not in (None, ""),
                                        }
                                        for operation in recovery_operations
                                    ],
                                },
                            )
                            if not recovery_operations:
                                break
                            all_recovery_operations.extend(recovery_operations)
                            for recovery in recovery_operations:
                                try:
                                    if recovery.target and recovery.target.selector:
                                        raw = page.locator(recovery.target.selector)
                                        raw_count = await raw.count()
                                        if raw_count:
                                            enabled_visible = any(
                                                await raw.nth(index).is_visible()
                                                and await raw.nth(index).is_enabled()
                                                for index in range(raw_count)
                                            )
                                            if not enabled_visible:
                                                continue
                                    grounded, _ = await adapter.grounded_locator(recovery.target)
                                    if (
                                        hasattr(grounded, "is_enabled")
                                        and not await grounded.is_enabled()
                                    ):
                                        continue
                                except (PlaywrightError, GroundingError):
                                    continue
                                await adapter.execute(recovery)
                                await page.wait_for_timeout(180)
                            readiness = await _rehearsal_form_readiness(
                                page, candidate.submit_target
                            )
                            # Continue re-observing even when native validity
                            # is clear; dependent controls can become enabled
                            # only after the preceding semantic selection has
                            # committed. The three-round bound prevents loops.
                        recovery_operations = all_recovery_operations
                        rehearsal_operations_used = list(recovery_operations)
                        if recovery_operations:
                            artifacts.write_json(
                                "discovery/rehearsal-validation-readiness.json",
                                {
                                    "capability_id": candidate.id,
                                    "readiness": dict(readiness),
                                    "operation_kinds": [
                                        operation.kind.value for operation in recovery_operations
                                    ],
                                },
                            )
                            if (
                                not readiness["invalid_field_count"]
                                and not readiness["submit_disabled"]
                            ):
                                await adapter.execute(
                                    SemanticOperation(
                                        kind=OperationKind.SUBMIT,
                                        intent="Retry form submission after observed validation recovery",
                                        target=candidate.submit_target,
                                        postconditions=[],
                                        story_phase="verify",
                                        page_url=candidate.source_url,
                                        evidence_refs=[
                                            "validation-recovery:resolved-live-controls"
                                        ],
                                    )
                                )
                                await page.wait_for_timeout(500)
                                observed = await self.discovery.inspect(page, objective)
                                recovery_values = [
                                    str(operation.value)
                                    for operation in recovery_operations
                                    if operation.value not in (None, "")
                                ]
                                outcome_candidates = await _wait_for_rehearsal_outcome(
                                    page,
                                    source_url=candidate.source_url,
                                    submitted_values=[
                                        *rehearsal_dataset.values(),
                                        *recovery_values,
                                    ],
                                )
                                observed_elements = [*observed.elements, *outcome_candidates]
                                witnessed = _rehearsal_detail_navigation_witness(
                                    candidate,
                                    before_url=before_submit_url,
                                    after_url=page.url,
                                ) or derive_outcome_witness(
                                    candidate,
                                    before_text=before_text,
                                    observed=observed_elements,
                                    submitted_values=[
                                        *rehearsal_dataset.values(),
                                        *recovery_values,
                                    ],
                                )
                                # Dependent widgets may become enabled only
                                # after the first validation retry (date/time
                                # after branch/doctor, for example). Re-read
                                # the dialog once more and retry only while it
                                # remains open without an outcome witness.
                                for _ in range(2):
                                    if witnessed is not None:
                                        break
                                    follow_up = await _rehearsal_validation_recovery_operations(
                                        page,
                                        source_url=candidate.source_url,
                                        capability=candidate,
                                    )
                                    if not follow_up:
                                        break
                                    recovery_operations.extend(follow_up)
                                    rehearsal_operations_used = list(recovery_operations)
                                    for recovery in follow_up:
                                        try:
                                            if recovery.target and recovery.target.selector:
                                                raw = page.locator(recovery.target.selector)
                                                raw_count = await raw.count()
                                                if raw_count:
                                                    enabled_visible = any(
                                                        await raw.nth(index).is_visible()
                                                        and await raw.nth(index).is_enabled()
                                                        for index in range(raw_count)
                                                    )
                                                    if not enabled_visible:
                                                        continue
                                            grounded, _ = await adapter.grounded_locator(
                                                recovery.target
                                            )
                                            if (
                                                hasattr(grounded, "is_enabled")
                                                and not await grounded.is_enabled()
                                            ):
                                                continue
                                        except (PlaywrightError, GroundingError):
                                            continue
                                        await adapter.execute(recovery)
                                        await page.wait_for_timeout(180)
                                    follow_up_values = [
                                        str(operation.value)
                                        for operation in follow_up
                                        if operation.value not in (None, "")
                                    ]
                                    await adapter.execute(
                                        SemanticOperation(
                                            kind=OperationKind.SUBMIT,
                                            intent="Retry form submission after dependent control became ready",
                                            target=candidate.submit_target,
                                            postconditions=[],
                                            story_phase="verify",
                                            page_url=candidate.source_url,
                                            evidence_refs=["validation-recovery:dependent-control"],
                                        )
                                    )
                                    await page.wait_for_timeout(500)
                                    observed = await self.discovery.inspect(page, objective)
                                    outcome_candidates = await _wait_for_rehearsal_outcome(
                                        page,
                                        source_url=candidate.source_url,
                                        submitted_values=[
                                            *rehearsal_dataset.values(),
                                            *recovery_values,
                                            *follow_up_values,
                                        ],
                                    )
                                    observed_elements = [*observed.elements, *outcome_candidates]
                                    witnessed = _rehearsal_detail_navigation_witness(
                                        candidate,
                                        before_url=before_submit_url,
                                        after_url=page.url,
                                    ) or derive_outcome_witness(
                                        candidate,
                                        before_text=before_text,
                                        observed=observed_elements,
                                        submitted_values=[
                                            *rehearsal_dataset.values(),
                                            *recovery_values,
                                            *follow_up_values,
                                        ],
                                    )
                                if witnessed is not None:
                                    artifacts.write_json(
                                        "discovery/rehearsal-validation-recovery.json",
                                        {
                                            "capability_id": candidate.id,
                                            "operation_kinds": [
                                                operation.kind.value
                                                for operation in recovery_operations
                                            ],
                                            "status": "resolved_and_retried",
                                            "recording": "disabled",
                                        },
                                    )
                            post_submit_state = await _rehearsal_post_submit_state(page)
                            validation_unresolved = bool(
                                post_submit_state["visible_dialog_count"]
                                or post_submit_state["invalid_field_count"]
                            )
                    if witnessed is None:
                        try:
                            validation_messages = [
                                " ".join(text.split())[:300]
                                for text in await page.locator("[role='alert']").all_inner_texts()
                                if text.strip()
                            ][:8]
                        except PlaywrightError:
                            validation_messages = []
                        artifacts.write_json(
                            "discovery/rehearsal-validation-messages.json",
                            {"capability_id": candidate.id, "messages": validation_messages},
                        )
                        with suppress(PlaywrightError, OSError):
                            screenshot_bytes = await page.screenshot(full_page=False)
                            (artifacts.root / "discovery" / "rehearsal-failure.png").write_bytes(
                                screenshot_bytes
                            )
                        try:
                            invalid_controls = await page.locator(
                                "[aria-invalid='true'], input:invalid, select:invalid, textarea:invalid"
                            ).evaluate_all(
                                """nodes => nodes.map(node => ({
                                  tag: node.tagName.toLowerCase(), type: node.getAttribute('type'),
                                  name: node.getAttribute('name'), id: node.getAttribute('id'),
                                  placeholder: node.getAttribute('placeholder'),
                                  ariaLabel: node.getAttribute('aria-label'),
                                  value: node.value || '', disabled: !!node.disabled,
                                  invalid: node.getAttribute('aria-invalid'),
                                  html: node.outerHTML.slice(0, 500)
                                })).slice(0, 12)"""
                            )
                        except PlaywrightError:
                            invalid_controls = []
                        artifacts.write_json(
                            "discovery/rehearsal-invalid-controls.json",
                            {"capability_id": candidate.id, "controls": invalid_controls},
                        )
                        artifacts.write_json(
                            "discovery/rehearsal-attempt.json",
                            {
                                "capability_id": candidate.id,
                                "status": "outcome_unverified",
                                "recording": "disabled",
                                "recovery_only": recover_dispatched_attempt,
                                "submitted_operation_kinds": [
                                    operation.kind.value for operation in operations
                                ],
                                "post_submit_candidate_count": len(outcome_candidates),
                                "post_submit_candidate_structures": sorted(
                                    {f"{item.role or item.tag}" for item in outcome_candidates}
                                ),
                                "post_submit_state": post_submit_state,
                                "reason": (
                                    "form_validation_or_overlay_remained_after_submit"
                                    if validation_unresolved
                                    else "no_independent_visible_result_witness"
                                ),
                            },
                        )
                        raise GenerationPreconditionError(
                            "REHEARSAL_FORM_VALIDATION_UNRESOLVED: form remained invalid or open after submit"
                            if validation_unresolved
                            else "REHEARSAL_OUTCOME_UNVERIFIED: submission did not yield an independent visible result witness"
                        )
                if witnessed is not None and rehearsal_operations_used and candidate.form_schema:
                    # Promote controls proven necessary by the live validation
                    # loop into the capability contract. Production will then
                    # compile the same observed dependency sequence instead of
                    # reverting to the initial shallow schema.
                    fields = list(candidate.form_schema.fields)
                    for operation in rehearsal_operations_used:
                        target = operation.target
                        if target is None or operation.kind is OperationKind.CHECK:
                            continue
                        match = next(
                            (
                                field
                                for field in fields
                                if field.selector == target.selector
                                or field.name.casefold() == target.name.casefold()
                            ),
                            None,
                        )
                        if match is not None:
                            options = list(match.options)
                            if (
                                operation.kind is OperationKind.SELECT_OPTION
                                and operation.value
                                and str(operation.value) not in options
                            ):
                                options.append(str(operation.value))
                            fields[fields.index(match)] = match.model_copy(
                                update={
                                    "required": match.required
                                    or operation.kind
                                    in {
                                        OperationKind.FILL_TEXT,
                                        OperationKind.FILL_EMAIL,
                                        OperationKind.FILL_PHONE,
                                        OperationKind.SELECT_DATE,
                                    }
                                    or "*" in target.name,
                                    "options": options,
                                    "selector": _normalise_observed_selector(
                                        match.selector or target.selector
                                    ),
                                    "validation_messages": [
                                        *match.validation_messages,
                                        "rehearsal:include",
                                    ],
                                }
                            )
                        elif operation.kind in {
                            OperationKind.FILL_TEXT,
                            OperationKind.FILL_EMAIL,
                            OperationKind.FILL_PHONE,
                            OperationKind.SELECT_DATE,
                            OperationKind.SELECT_OPTION,
                        }:
                            fields.append(
                                FormField(
                                    name=target.name,
                                    selector=_normalise_observed_selector(target.selector)
                                    or target.label
                                    or target.name,
                                    control_type=(
                                        "combobox"
                                        if operation.kind is OperationKind.SELECT_OPTION
                                        else "date"
                                        if operation.kind is OperationKind.SELECT_DATE
                                        else "text"
                                    ),
                                    required=operation.kind
                                    in {
                                        OperationKind.FILL_TEXT,
                                        OperationKind.FILL_EMAIL,
                                        OperationKind.FILL_PHONE,
                                        OperationKind.SELECT_DATE,
                                    },
                                    options=[str(operation.value)]
                                    if operation.kind is OperationKind.SELECT_OPTION
                                    and operation.value
                                    else [],
                                    confidence=0.8,
                                    validation_messages=["rehearsal:include"],
                                )
                            )
                    candidate = candidate.model_copy(
                        update={
                            "form_schema": candidate.form_schema.model_copy(
                                update={"fields": fields}
                            )
                        }
                    )
                artifacts.write_json(
                    "discovery/rehearsal-attempt.json",
                    {
                        "capability_id": candidate.id,
                        "status": "outcome_verified",
                        "recording": "disabled",
                        "recovery_only": recover_dispatched_attempt,
                        "outcome_structure": witnessed.outcome_target.role or "visible_result",
                    },
                )
            finally:
                if lease_guard is not None:
                    lease_guard.cancel()
                    with suppress(asyncio.CancelledError):
                        await lease_guard
                if remote is not None:
                    with suppress(Exception):
                        await asyncio.wait_for(remote.close(), timeout=10)
                if browser is not None:
                    await browser.close()
                if session is not None:
                    await self._release_cloud_session(
                        session.session_id,
                        reason="rehearsal_complete",
                        run_id=run_id,
                    )
        updated = [
            (candidate if item.id == candidate.id else item).model_dump(mode="json")
            for item in capabilities
        ]
        context = context.model_copy(update={"capabilities": updated})
        artifacts.write_json(
            "discovery/rehearsal-report.json",
            {
                "capability_id": candidate.id,
                "purpose": candidate.purpose,
                "outcome_target": witnessed.outcome_target.model_dump(mode="json"),
                "outcome_evidence": witnessed.outcome_evidence,
                "recording": "disabled",
                "context": "fresh_rehearsal",
            },
        )
        artifacts.write_json("discovery/capabilities.json", updated)
        artifacts.write_json("discovery/product-context.json", context.model_dump(mode="json"))
        return context

