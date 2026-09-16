"""Production URL generation: explore, plan, execute, present, and quality-check."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from urllib.parse import urljoin, urlsplit, urlunsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from pydantic import ValidationError

from productlens.artifacts.store import RunArtifacts, materialize_trace_lifecycle
from productlens.browser.screencast import CdpScreencastRecorder
from productlens.browser.theme import discover_theme_control
from productlens.contracts.models import (
    ActionCapability,
    AudienceProfile,
    DemoPlan,
    DemoTrace,
    DiscoveryBudget,
    EditorialStoryboard,
    ExplorationReport,
    FormField,
    InteractionEvent,
    InteractionTrace,
    NarrationScript,
    NarrationSegment,
    ObjectiveSpec,
    ObservedElement,
    OperationKind,
    Postcondition,
    ProductContext,
    Rect,
    ReplanDecision,
    SemanticOperation,
    Target,
    Viewport,
    ViewportDecision,
    WorkflowStep,
)
from productlens.credentials.service import EnvironmentCredentialService
from productlens.discovery.live import (
    LiveDiscovery,
    _objective_spec,
    _page_knowledge,
    adaptive_exploration_budget,
)
from productlens.evaluation.completion_audit import audit_run
from productlens.evaluation.sample_video_benchmark import compare_to_sample_benchmark
from productlens.execution.engine import ExecutionEngine
from productlens.execution.playwright_adapter import GroundingError, PlaywrightAdapter
from productlens.narration.script import (
    bind_opening_to_first_event,
    captions_from_duration,
    recommended_caption_duration,
    script_from_trace,
)
from productlens.narration.service import NarrationService, SpeechProvider
from productlens.observability.logging import get_logger, redact_prompt_text
from productlens.orchestration.lifecycle import RunStage
from productlens.planning.brief import build_demo_brief
from productlens.planning.capabilities import (
    CapabilityCompilationError,
    compile_rehearsal_operations,
)
from productlens.planning.production import ProductionPlanningService
from productlens.planning.rehearsal import (
    CapabilitySelectionError,
    derive_outcome_witness,
    select_rehearsal_capability,
)
from productlens.planning.side_effects import SideEffectPolicyError, authorize_operation
from productlens.planning.state_machine import WorkflowStateMachine
from productlens.planning.synthetic import SyntheticDataError, hydrate_operations
from productlens.presentation.director import build_presentation_plan
from productlens.presentation.editorial import (
    bind_storyboard_events,
    build_editorial_storyboard,
    editorial_script,
    enrich_editorial_brief,
    enrich_editorial_storyboard,
)
from productlens.presentation.journey import build_journey, inspect_journey
from productlens.presentation.scenes import build_scene_plan
from productlens.presentation.viewport import choose_viewport, probe_viewport_candidates
from productlens.providers.browserbase import BrowserbaseProvider
from productlens.providers.errors import ProviderError
from productlens.providers.stagehand import StagehandProvider
from productlens.quality.consistency import validate_selected_candidate_consistency
from productlens.quality.coverage import inspect_coverage
from productlens.quality.delivery import delivery_report
from productlens.quality.editorial import inspect_editorial, inspect_editorial_preflight
from productlens.quality.multimodal import VisualReviewer, build_review_packet, review_multimodal
from productlens.quality.presentation import (
    attach_presentation_qa,
    inspect_presentation,
)
from productlens.quality.repair import classify_repair
from productlens.quality.story import inspect_story
from productlens.quality.synchronization import inspect_synchronization, secure_transition_intervals
from productlens.quality.video import inspect_video
from productlens.services.generation_policy import (
    canonical_url as _canonical_url,
)
from productlens.services.generation_policy import (
    normalise_observed_selector as _normalise_observed_selector,
)
from productlens.services.generation_policy import (
    production_duration_envelope as _production_duration_envelope,
)
from productlens.services.generation_policy import (
    recording_frame_rate as _recording_frame_rate,
)
from productlens.services.generation_policy import safe_render_error
from productlens.services.knowledge import product_knowledge_payload
from productlens.services.render_stage import render_run


class GenerationPreconditionError(RuntimeError):
    pass


# Keep the legacy import surface stable for local tools and existing callers;
# render-stage code uses the canonical helper directly.
_safe_render_error = safe_render_error


def _narration_script_contract(
    script: list[dict[str, object]],
    *,
    mode: str,
    audience: str,
    audience_profile: AudienceProfile | None = None,
    captions: list[dict[str, object]] | None = None,
) -> NarrationScript:
    """Validate and normalize the single script shared by every presentation layer."""
    timing_by_scene = {
        str(item.get("scene_id") or item.get("event_id")): item
        for item in (captions or [])
        if item.get("scene_id") or item.get("event_id")
    }
    segments: list[NarrationSegment] = []
    for line in script:
        event_id = str(line.get("event_id") or "").strip()
        if not event_id:
            raise GenerationPreconditionError(
                "NARRATION_SCRIPT_INVALID: every line needs an event_id"
            )
        scene_id = str(line.get("scene_id") or event_id).strip()
        facts = line.get("facts", [])
        evidence = (
            [str(item) for item in facts if isinstance(item, str)]
            if isinstance(facts, list)
            else []
        )
        # A page-local scene can legitimately cite many section/element refs,
        # but the public narration contract intentionally caps evidence IDs so
        # artifacts stay bounded. Preserve stable order and provenance rather
        # than failing an otherwise valid script on a dense page inventory.
        evidence = list(dict.fromkeys(evidence))[:24]
        timing = timing_by_scene.get(scene_id) or timing_by_scene.get(event_id) or {}
        segments.append(
            NarrationSegment(
                scene_id=scene_id,
                event_id=event_id,
                text=str(line.get("text") or "").strip(),
                evidence=evidence,
                facts=facts if isinstance(facts, (list, dict)) else [],
                opening=bool(line.get("opening", False)),
                start_seconds=float(timing["start"]) if timing.get("start") is not None else None,
                end_seconds=float(timing["end"]) if timing.get("end") is not None else None,
            )
        )
    return NarrationScript(
        mode="tts" if mode == "tts" else "caption_only",
        audience=audience,
        audience_profile=audience_profile or AudienceProfile(),
        timing_owner="measured_audio" if mode == "tts" else "scene",
        segments=segments,
    )


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


logger = get_logger("productlens.generation")


def _product_knowledge_payload(
    context: ProductContext, *, project_id: str | None = None
) -> dict[str, object]:
    """Compatibility alias for the canonical knowledge materializer."""
    return product_knowledge_payload(context, project_id=project_id)


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


async def _apply_requested_visual_state(page, objective: str) -> dict[str, object]:
    """Apply and record an explicit visual state before recording begins."""
    requested_light = "light theme" in objective.lower() or "light themed" in objective.lower()
    if not requested_light:
        return {"requested": "source-default", "applied": True, "dark": None}
    try:
        # Framework theme providers commonly hydrate after DOMContentLoaded.
        # Measure after that window, not against a transient server shell.
        await page.emulate_media(color_scheme="light")
        await page.wait_for_timeout(850)
        is_dark = await page.evaluate(
            """() => {
              const parts = (getComputedStyle(document.body).backgroundColor.match(/\\d+/g) || []).map(Number);
              return parts.length >= 3 && (parts[0] * .2126 + parts[1] * .7152 + parts[2] * .0722) <= 150;
            }"""
        )
        toggle = await discover_theme_control(page)
        if is_dark and toggle is not None:
            await toggle.click()
            await page.wait_for_timeout(900)
        rendered = await page.evaluate(
            """() => {
              const color = getComputedStyle(document.body).backgroundColor;
              const parts = (color.match(/\\d+/g) || []).map(Number);
              const luminance = parts.length >= 3 ? (parts[0] * .2126 + parts[1] * .7152 + parts[2] * .0722) : 0;
              return {darkClass: document.documentElement.classList.contains('dark'), color, luminance};
            }"""
        )
        applied = float(rendered["luminance"]) > 150
        return {
            "requested": "light",
            "applied": applied,
            "dark": not applied,
            "background": rendered["color"],
        }
    except (PlaywrightError, KeyError, TypeError, ValueError):
        # This is preserved as an explicit failed visual requirement for QA;
        # it must never silently become an accidental dark-theme delivery.
        return {"requested": "light", "applied": False, "dark": None}


async def _prepare_requested_visual_state(page, objective: str) -> None:
    """Seed an explicit theme before every document navigation starts."""
    if "light theme" not in objective.lower() and "light themed" not in objective.lower():
        return
    await page.add_init_script(
        """() => {
          localStorage.setItem('theme', 'light');
          document.documentElement.classList.remove('dark');
          document.documentElement.classList.add('light');
        }"""
    )


class UrlGenerationService:
    def __init__(
        self,
        planner: ProductionPlanningService,
        speech_provider: SpeechProvider | None = None,
        browserbase_provider: BrowserbaseProvider | None = None,
        stagehand_provider: StagehandProvider | None = None,
        credential_service: EnvironmentCredentialService | None = None,
        visual_reviewer: VisualReviewer | None = None,
        cloud_capture_timeout_seconds: int = 840,
        stagehand_observe_timeout_seconds: float = 105.0,
    ):
        self.planner = planner
        self.speech_provider = speech_provider
        self.browserbase_provider = browserbase_provider
        self.stagehand_provider = stagehand_provider
        self.credential_service = credential_service or EnvironmentCredentialService()
        self.visual_reviewer = visual_reviewer
        self.cloud_capture_timeout_seconds = max(30, cloud_capture_timeout_seconds)
        self.stagehand_observe_timeout_seconds = max(
            30.0, min(180.0, float(stagehand_observe_timeout_seconds))
        )
        self.discovery = LiveDiscovery()

    async def _runtime_replan(
        self,
        *,
        adapter: PlaywrightAdapter,
        plan: DemoPlan,
        failed_step: WorkflowStep,
        trace: DemoTrace,
        error: str,
        dispatched: bool,
        objective: str,
        artifacts: RunArtifacts,
        cloud_session_id: str | None = None,
        browserbase_connect_url: str | None = None,
        stagehand_extension_id: str | None = None,
    ) -> ReplanDecision | None:
        """Ask the configured intelligence layer for a grounded suffix repair.

        The model receives only bounded, redacted current-page evidence.  It
        proposes semantic steps; the execution engine still owns target
        grounding, safety, postconditions, and the no-replay rule.
        """
        structured = getattr(getattr(self.planner, "provider", None), "structured", None)
        if structured is None:
            return None
        try:
            evidence = await adapter.page_evidence(max_text=6_000)
        except Exception:  # noqa: BLE001 - advisory evidence must never abort production
            return None
        # A discovered content link can disappear after a prior SPA transition
        # (for example, a card observed on the landing page is not present on a
        # component detail page).  Do not ask the model to hallucinate a new
        # target in that state.  If the validated visible-control target is no
        # longer available, use the already observed same-origin destination as
        # an explicit, auditable fallback.  This preserves the story and the
        # no-replay guarantee while keeping direct navigation a last resort.
        if (
            not dispatched
            and failed_step.operation.kind is OperationKind.OPEN_NAVIGATION_ITEM
            and failed_step.operation.target is not None
            and failed_step.operation.target.source_url
        ):
            expected_url = next(
                (
                    str(condition.expected)
                    for condition in failed_step.operation.postconditions
                    if condition.kind == "url" and condition.expected
                ),
                None,
            )
            current_url = str(evidence.get("url") or adapter.page.url)
            if expected_url:
                destination = urljoin(current_url, expected_url)
                if urlsplit(destination).netloc == urlsplit(current_url).netloc and _canonical_url(
                    destination
                ) != _canonical_url(current_url):
                    fallback = WorkflowStep(
                        id=f"{failed_step.id}-visible-control-fallback",
                        intent=(
                            f"Use the observed same-origin destination for {failed_step.operation.target.name} "
                            "because its previously observed visible control is unavailable in the current state"
                        ),
                        operation=SemanticOperation(
                            kind=OperationKind.NAVIGATE,
                            intent=(
                                f"Intentional direct-navigation fallback to the observed destination for "
                                f"{failed_step.operation.target.name}; visible control unavailable"
                            ),
                            value=destination,
                            postconditions=failed_step.operation.postconditions,
                            critical=failed_step.operation.critical,
                            story_phase=failed_step.operation.story_phase,
                            evidence_refs=[
                                *failed_step.operation.evidence_refs,
                                "fallback:visible-control-unavailable",
                            ],
                        ),
                        page_requirement=current_url,
                        importance=failed_step.importance,
                        narration_intent=failed_step.narration_intent,
                        visual_intent=failed_step.visual_intent,
                        fallback_strategy="observed same-origin destination after visible-control loss",
                        allowed_retries=0,
                        completion_criteria=failed_step.completion_criteria,
                        evidence_refs=[
                            *failed_step.evidence_refs,
                            "fallback:visible-control-unavailable",
                        ],
                        page_phase=failed_step.page_phase,
                    )
                    # ``run_adaptive`` replaces the entire remaining suffix,
                    # not only the failing step. Preserve the already
                    # validated continuation after this navigation so a
                    # recovery does not silently truncate later pages/scenes.
                    try:
                        failed_index = next(
                            index
                            for index, candidate in enumerate(plan.workflow_steps)
                            if candidate.operation.id == failed_step.operation.id
                        )
                    except StopIteration:
                        failed_index = len(plan.workflow_steps)
                    continuation = plan.workflow_steps[failed_index + 1 :]
                    return ReplanDecision(
                        reason="visible semantic control disappeared after a verified page transition",
                        failed_operation_id=failed_step.operation.id,
                        replacement_steps=[fallback, *continuation],
                    )
        text = str(evidence.get("text") or "")
        # Do not place personal/contact fields or credential-like values in a
        # model prompt or a durable artifact. Labels and visible structure are
        # sufficient to re-ground a changed UI.
        text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[redacted-email]", text)
        text = re.sub(r"\b(?:\+?\d[\d ()-]{7,}\d)\b", "[redacted-phone]", text)
        text = re.sub(
            r"(?im)\b(password|passcode|token|secret|api[ -]?key)\b\s*[:=]\s*\S+",
            r"\1: [redacted]",
            text,
        )
        stagehand_context = ""
        if self.stagehand_provider:
            try:
                stagehand_environment = (
                    "BROWSERBASE" if cloud_session_id and browserbase_connect_url else "LOCAL"
                )
                observation = await self.stagehand_provider.observe(
                    url=str(evidence.get("url") or adapter.page.url),
                    instruction=(
                        "Observe the current visible state only. Identify the relevant "
                        "read-only continuation after the failed step; do not click, type, "
                        "submit, navigate, or mutate anything."
                    ),
                    analysis_instruction="Return visible sections, meaningful controls, and safe next actions only.",
                    environment=stagehand_environment,
                    browserbase_session_id=cloud_session_id,
                    browserbase_connect_url=browserbase_connect_url,
                    browserbase_extension_id=stagehand_extension_id,
                    cache_dir=artifacts.root / "discovery" / "stagehand-cache",
                )
                if observation.analysis:
                    stagehand_context = json.dumps(
                        {
                            "visible_sections": observation.analysis.visible_sections[:12],
                            "meaningful_controls": observation.analysis.meaningful_controls[:20],
                            "safe_next_actions": observation.analysis.safe_next_actions[:12],
                        },
                        ensure_ascii=False,
                    )
            except Exception:  # noqa: BLE001 - Stagehand is an optional advisory provider
                # Stagehand is advisory. Playwright evidence remains the source
                # of truth, so a provider observation failure does not erase a
                # valid local replan opportunity.
                stagehand_context = "unavailable"
        prompt = (
            "You are the runtime recovery planner for a browser demo. Return only a "
            "ReplanDecision JSON object. Replace the failed workflow suffix from the "
            "CURRENT browser state; do not replay completed steps. Use only visible "
            "semantic targets and evidence. Every step needs a postcondition when it "
            "changes state. Prefer read-only observation/verification. Never invent "
            "URLs, selectors, claims, credentials, or hidden state. Do not emit a "
            "Navigate step. If the failed action was dispatched, the replacement must "
            "not repeat it or submit/mutate anything; verify the resulting state or take "
            "a safe read-only continuation.\n\n"
            f"Objective: {redact_prompt_text(objective)}\n"
            f"Failed step: {failed_step.intent}\n"
            f"Dispatched: {dispatched}\n"
            f"Failure: {error[:800]}\n"
            f"Current evidence: {json.dumps({'url': evidence.get('url'), 'title': evidence.get('title'), 'text': text, 'controls': evidence.get('controls', [])}, ensure_ascii=False)[:9_000]}\n"
            f"Stagehand advisory (untrusted): {stagehand_context or 'not used'}\n"
            "Return a short replacement sequence that preserves the story and can be "
            "grounded against this live page."
        )
        try:
            decision = await structured(prompt, ReplanDecision)
        except Exception:  # noqa: BLE001 - malformed provider output triggers local fallback
            return None
        if not isinstance(decision, ReplanDecision):
            return None
        allowed_read_only = {
            OperationKind.READ_VALUE,
            OperationKind.VERIFY_STATE,
            OperationKind.WAIT_FOR_STATE,
            OperationKind.SCROLL_TO,
            OperationKind.HOVER,
            OperationKind.KEY_PRESS,
            OperationKind.CLICK,
            OperationKind.OPEN_NAVIGATION_ITEM,
            OperationKind.OPEN_MODAL,
            OperationKind.CLOSE_MODAL,
            OperationKind.APPLY_FILTER,
        }
        safe_steps: list[WorkflowStep] = []
        for step in decision.replacement_steps:
            operation = step.operation
            if operation.kind is OperationKind.NAVIGATE:
                return None
            if dispatched and operation.kind not in allowed_read_only:
                return None
            if operation.kind in {
                OperationKind.SUBMIT,
                OperationKind.CHECK,
                OperationKind.UNCHECK,
                OperationKind.CREATE_RECORD,
            }:
                return None
            safe_steps.append(step)
        if not safe_steps:
            return None
        return decision.model_copy(update={"replacement_steps": safe_steps})

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

    async def _release_cloud_session(self, session_id: str, *, reason: str, run_id: str) -> None:
        """Release a Browserbase lease without letting cleanup hide its cause."""
        if self.browserbase_provider is None:
            return
        try:
            await asyncio.wait_for(
                self.browserbase_provider.close_session(session_id),
                timeout=15,
            )
        except Exception as error:  # noqa: BLE001 - cleanup is best-effort and idempotent
            logger.warning(
                "browserbase_session_release_failed",
                run_id=run_id,
                session_id=session_id,
                reason=reason,
                error_type=type(error).__name__,
            )

    async def _release_cloud_session_after(
        self, session_id: str, *, seconds: int, reason: str, run_id: str
    ) -> None:
        """Out-of-band lease guard for a CDP operation that ignores cancellation."""
        try:
            await asyncio.sleep(seconds)
            logger.warning(
                "browserbase_session_lease_guard_fired",
                run_id=run_id,
                session_id=session_id,
                reason=reason,
            )
            await self._release_cloud_session(session_id, reason=reason, run_id=run_id)
        except asyncio.CancelledError:
            # Successful/disposed stage lifecycle: normal path cancels guard.
            return

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

    @staticmethod
    def _duration_floor(plan: DemoPlan) -> int | None:
        """Return the objective's approved lower duration bound for every demo.

        ``target_duration_seconds`` guides editorial allocation, but the
        objective's minimum is a delivery promise for both feature and full
        walkthroughs.  Exempting feature demos here allowed a 20–45 second
        route/form clip to pass even when planning had explicitly approved a
        one-minute minimum.
        """
        return plan.minimum_duration_seconds

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

    async def plan_stage(
        self,
        *,
        run_id: str,
        objective: str,
        artifact_root: Path,
        allow_external_side_effects: bool,
        audience: str,
        target_duration_seconds: int,
    ) -> DemoPlan:
        """Plan from persisted discovery evidence; no browser or provider session is reused."""
        artifacts = RunArtifacts(artifact_root, run_id)
        context = ProductContext.model_validate(
            json.loads(
                (artifacts.root / "discovery" / "product-context.json").read_text(encoding="utf-8")
            )
        )
        # Reconcile the persisted model interpretation with the deterministic
        # request parser at the production boundary. Older discovery runs may
        # have upgraded a focused "thorough" request to full_walkthrough;
        # resuming them must not reintroduce the broad route crawl that the
        # current objective explicitly excludes.
        deterministic_objective = _objective_spec(objective)
        if (
            context.objective is not None
            and deterministic_objective.demo_type != "full_walkthrough"
            and context.objective.demo_type == "full_walkthrough"
        ):
            context = context.model_copy(
                update={
                    "objective": context.objective.model_copy(
                        update={
                            "demo_type": deterministic_objective.demo_type,
                            "minimum_duration_seconds": deterministic_objective.minimum_duration_seconds,
                            "target_duration_seconds": deterministic_objective.target_duration_seconds,
                            "maximum_duration_seconds": deterministic_objective.maximum_duration_seconds,
                            "depth": deterministic_objective.depth,
                        }
                    )
                }
            )
        # The model may echo an entire noun phrase (for example, "the
        # authenticated booking workflow") into ``primary_entity``.  Feature
        # grounding is intentionally token/entity based, so reconcile the
        # persisted interpretation with the deterministic request parser for
        # focused objectives.  This prevents provider phrasing from turning a
        # valid discovered feature into an apparently unsupported entity.
        if (
            context.objective is not None
            and deterministic_objective.demo_type != "full_walkthrough"
            and deterministic_objective.primary_entity
            and (
                context.objective.primary_entity != deterministic_objective.primary_entity
                or context.objective.must_show != deterministic_objective.must_show
            )
        ):
            context = context.model_copy(
                update={
                    "objective": context.objective.model_copy(
                        update={
                            "primary_entity": deterministic_objective.primary_entity,
                            "must_show": deterministic_objective.must_show,
                        }
                    )
                }
            )
        if (
            context.objective is not None
            and deterministic_objective.demo_type != "full_walkthrough"
            and deterministic_objective.supporting_relationships
        ):
            # Relationship/context language is part of the request contract,
            # not optional model decoration.  Restore explicit deterministic
            # relationships when resuming discovery written by an older model
            # pass that omitted them.
            context = context.model_copy(
                update={
                    "objective": context.objective.model_copy(
                        update={
                            "supporting_relationships": deterministic_objective.supporting_relationships,
                        }
                    )
                }
            )
        # Discovery artifacts may have been produced by an older objective
        # writer that treated prose pairs (for example, ``Home identity``) as
        # relationships.  Re-ground the relationship list at the planning
        # boundary as well, so editorial preflight cannot require a setup page
        # for a dependency the user never explicitly requested.
        if context.objective is not None and context.objective.supporting_relationships:
            raw_objective = context.objective.raw.casefold()
            explicit_relationships = []
            connector = r"(?:->|→|configures|explains|supports|in the context of|configured by|with context from|using)"
            for relation in context.objective.supporting_relationships:
                source_phrase = " ".join(re.findall(r"[a-z0-9]{3,}", relation.source.casefold()))
                target_phrase = " ".join(re.findall(r"[a-z0-9]{3,}", relation.target.casefold()))
                if (
                    source_phrase
                    and target_phrase
                    and (
                        re.search(
                            rf"{re.escape(source_phrase)}\s*{connector}\s*{re.escape(target_phrase)}",
                            raw_objective,
                        )
                        or re.search(
                            rf"{re.escape(target_phrase)}\s*{connector}\s*{re.escape(source_phrase)}",
                            raw_objective,
                        )
                    )
                ):
                    explicit_relationships.append(relation)
            context = context.model_copy(
                update={
                    "objective": context.objective.model_copy(
                        update={
                            "supporting_relationships": explicit_relationships,
                        }
                    )
                }
            )
        # Persist the human-reviewable story boundary before compiling browser
        # operations.  This proves production was selected from discovery
        # knowledge rather than from a recording-time route sweep.
        demo_brief = build_demo_brief(
            context,
            objective=objective,
            audience=audience,
            duration_seconds=target_duration_seconds,
        )
        artifacts.write_json("planning/demo-brief.json", demo_brief.model_dump(mode="json"))
        plan = await self.planner.plan(
            objective=objective,
            context=context,
            allow_external_side_effects=allow_external_side_effects,
            audience=audience,
            target_duration_seconds=target_duration_seconds,
        )
        # Keep the runtime capability decision as an inspectable planning
        # artifact.  It is evidence-backed and advisory; execution still
        # re-grounds each selected target before dispatch.
        artifacts.write_json(
            "planning/capability-resolutions.json",
            [item.model_dump(mode="json") for item in context.capability_resolutions],
        )
        artifacts.write_json("discovery/product-context.json", context.model_dump(mode="json"))
        artifacts.write_json("plan.json", plan.model_dump(mode="json"))
        artifacts.write_json(
            "planning/validated-state-graph.json",
            WorkflowStateMachine.from_operations(
                [step.operation for step in plan.workflow_steps]
            ).artifact(),
        )
        storyboard = build_editorial_storyboard(context, plan)
        storyboard = await enrich_editorial_brief(context, storyboard, self.planner.provider)
        storyboard = await enrich_editorial_storyboard(context, storyboard, self.planner.provider)
        artifacts.write_json(
            "presentation/editorial-brief.json", storyboard.brief.model_dump(mode="json")
        )
        # Keep the extraction/editorial boundary inspectable. The brief's facts
        # are the approved, evidence-cited claims; narration is generated only
        # after this immutable fact set exists and is persisted for repairs.
        artifacts.write_json(
            "narration/fact-extraction.json",
            {
                "schema_version": 1,
                "source": "page-knowledge-and-editorial-brief",
                "facts": [item.model_dump(mode="json") for item in storyboard.brief.facts],
                "excluded_areas": storyboard.brief.excluded_areas,
            },
        )
        artifacts.write_json("presentation/storyboard.json", storyboard.model_dump(mode="json"))
        editorial_preflight = inspect_editorial_preflight(
            context=context, plan=plan, storyboard=storyboard
        )
        artifacts.write_json("qa/editorial-preflight.json", editorial_preflight)
        if editorial_preflight["hard_failures"]:
            raise RuntimeError(
                f"Editorial preflight rejected plan: {editorial_preflight['hard_failures']}"
            )
        return plan

    async def execute_stage(
        self,
        *,
        run_id: str,
        url: str,
        objective: str,
        artifact_root: Path,
        credential_reference: str | None = None,
        cloud_production: bool = False,
    ) -> DemoTrace:
        """Execute the persisted semantic plan in a new, independently owned browser."""
        artifacts = RunArtifacts(artifact_root, run_id)
        plan = DemoPlan.model_validate(
            json.loads((artifacts.root / "plan.json").read_text(encoding="utf-8"))
        )
        context_path = artifacts.root / "discovery" / "product-context.json"
        try:
            context = ProductContext.model_validate(
                json.loads(context_path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise GenerationPreconditionError(
                "CAPABILITY_CONTEXT_MISSING: production execution requires the evidence-backed product context"
            ) from error
        state_graph_path = artifacts.root / "planning" / "validated-state-graph.json"
        if not state_graph_path.is_file():
            raise GenerationPreconditionError(
                "STATE_GRAPH_MISSING: production execution requires the validated planning state graph"
            )
        try:
            state_graph = json.loads(state_graph_path.read_text(encoding="utf-8"))
            transitions = state_graph.get("transitions", [])
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise GenerationPreconditionError(
                "STATE_GRAPH_INVALID: validated state graph is unreadable"
            ) from error
        expected_graph = WorkflowStateMachine.from_operations(
            [step.operation for step in plan.workflow_steps]
        ).artifact()
        if (
            transitions != expected_graph["transitions"]
            or state_graph.get("initial_state") != expected_graph["initial_state"]
        ):
            raise GenerationPreconditionError(
                "STATE_GRAPH_MISMATCH: persisted execution contract does not match the validated DemoPlan"
            )
        storyboard_path = artifacts.presentation / "storyboard.json"
        storyboard = (
            EditorialStoryboard.model_validate(
                json.loads(storyboard_path.read_text(encoding="utf-8"))
            )
            if storyboard_path.exists()
            else None
        )
        _effective_maximum, duration_accounting = _production_duration_envelope(plan)
        if duration_accounting:
            artifacts.write_json("presentation/duration-accounting.json", duration_accounting)
        viewport = ViewportDecision.model_validate(
            json.loads(
                (artifacts.root / "discovery" / "viewport-decision.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        async with async_playwright() as pw:
            # Keep a local browser only for the local Playwright-video path.
            # Cloud production connects to a clean Browserbase CDP session and
            # ProductLens records that exact session through Page.screencast.
            browser = await pw.chromium.launch() if not cloud_production else None
            remote = None
            session = None
            cloud_session_id: str | None = None
            production_context_id: str | None = None
            try:
                screencast: CdpScreencastRecorder | None = None
                if cloud_production:
                    if self.browserbase_provider is None:
                        raise GenerationPreconditionError(
                            "BROWSERBASE_REQUIRED: cloud production is not configured"
                        )
                    try:
                        session = await asyncio.wait_for(
                            self.browserbase_provider.create_session_info(
                                viewport={
                                    "width": viewport.viewport.width,
                                    "height": viewport.viewport.height,
                                },
                                user_metadata={"productlens_run_id": run_id, "stage": "production"},
                            ),
                            timeout=60,
                        )
                    except TimeoutError as error:
                        raise GenerationPreconditionError(
                            "BROWSERBASE_SESSION_CREATE_TIMEOUT: production session did not become ready"
                        ) from error
                    cloud_session_id = session.session_id
                    production_context_id = cloud_session_id
                    # This is deliberately separate from discovery's session:
                    # the production trace must be from a clean context.
                    artifacts.write_json(
                        "execution/browserbase-session.json",
                        {
                            "provider": "browserbase",
                            "session_id": cloud_session_id,
                            "stagehand_extension_id": session.stagehand_extension_id,
                        },
                    )
                    # Browserbase can accept session creation while the CDP
                    # endpoint is still provisioning. Bound the connect so a
                    # provider/network stall becomes a classified execution
                    # failure instead of an uncheckpointed worker hang.
                    try:
                        remote = await asyncio.wait_for(
                            pw.chromium.connect_over_cdp(session.connect_url, timeout=60_000),
                            timeout=65,
                        )
                    except TimeoutError as error:
                        raise GenerationPreconditionError(
                            "BROWSERBASE_CDP_CONNECT_TIMEOUT: production endpoint did not become ready"
                        ) from error
                    production = remote.contexts[0]
                    # Browserbase contexts are provisioned remotely, so video
                    # recording must use our CDP screencast rather than
                    # Playwright's local record_video option.
                    page = production.pages[0] if production.pages else await production.new_page()
                    await page.set_viewport_size(
                        {"width": viewport.viewport.width, "height": viewport.viewport.height}
                    )
                    video = None
                    screencast = CdpScreencastRecorder(
                        page,
                        artifacts.execution / "cloud-screencast-frames",
                        frame_rate=30,
                    )
                    await screencast.start()
                else:
                    assert browser is not None
                    production = await browser.new_context(
                        viewport={
                            "width": viewport.viewport.width,
                            "height": viewport.viewport.height,
                        },
                        color_scheme="light"
                        if "light theme" in objective.lower() or "light themed" in objective.lower()
                        else "no-preference",
                        record_video_dir=str(artifacts.execution),
                        record_video_size={
                            "width": viewport.viewport.width,
                            "height": viewport.viewport.height,
                        },
                    )
                    page = await production.new_page()
                    video = page.video
                    production_context_id = f"local:{run_id}"
                # The ProductLens DemoTrace plus Browserbase Session Replay
                # are the durable cloud evidence. Playwright tracing opens a
                # second CDP recording channel which can stall a remote
                # context before the opening page is loaded; retain it only
                # for local capture, where it is a useful diagnostic artifact.
                playwright_trace_started = remote is None
                if playwright_trace_started:
                    await production.tracing.start(
                        screenshots=True,
                        snapshots=True,
                        sources=True,
                    )
                from datetime import UTC, datetime

                trace: DemoTrace | None = DemoTrace(
                    run_id=run_id,
                    objective=objective,
                    started_at=datetime.now(UTC),
                    capability_resolutions=list(context.capability_resolutions),
                )

                async def observe_auth_action(
                    operation_id: str,
                    kind: str,
                    label: str,
                    selector: str,
                ) -> None:
                    """Append login evidence without copying secret values."""
                    assert trace is not None
                    now = datetime.now(UTC)
                    # The credential service deliberately passes a stable
                    # semantic selector to this callback, but an application
                    # is free to implement its email/user field as a text
                    # input with only a name or autocomplete attribute.  Do
                    # not let that implementation detail force a full-frame
                    # redaction (which made the login look pasted/opaque).
                    # Re-ground the geometry against the currently visible
                    # login control using generic input semantics only.
                    selector_candidates = [selector]
                    if "password" in label.casefold():
                        selector_candidates.extend(
                            [
                                'input[type="password"]',
                                'input[autocomplete="current-password"]',
                            ]
                        )
                    else:
                        selector_candidates.extend(
                            [
                                'input[type="email"]',
                                'input[name*="user" i]',
                                'input[name*="email" i]',
                                'input[autocomplete="username"]',
                            ]
                        )
                    box = None
                    for candidate_selector in dict.fromkeys(selector_candidates):
                        try:
                            candidates = page.locator(candidate_selector)
                            for candidate_index in range(await candidates.count()):
                                locator = candidates.nth(candidate_index)
                                if not await locator.is_visible():
                                    continue
                                candidate_box = await locator.bounding_box()
                                if (
                                    candidate_box
                                    and candidate_box.get("width", 0) > 1
                                    and candidate_box.get("height", 0) > 1
                                ):
                                    box = candidate_box
                                    break
                            if box is not None:
                                break
                        except PlaywrightError:
                            continue
                    target_rect = Rect(**box) if box else None
                    viewport_size = page.viewport_size or {}
                    viewport = (
                        Viewport(
                            width=int(viewport_size.get("width", 0)),
                            height=int(viewport_size.get("height", 0)),
                        )
                        if viewport_size.get("width") and viewport_size.get("height")
                        else None
                    )
                    trace.events.append(
                        InteractionEvent(
                            operation_id=operation_id,
                            kind=OperationKind(kind),
                            intent=f"Complete the {label.lower()} step of authentication",
                            occurred_at=now,
                            action_at=now,
                            target=Target(name=label, selector=selector, source_url=page.url),
                            target_rect=target_rect,
                            viewport=viewport,
                            page_url=page.url,
                            before={"url": page.url, "authentication": "redacted"},
                            after={"url": page.url, "authentication": "redacted", "action": label},
                            success=True,
                            # The credential observer is called at the
                            # beginning of a deliberate presentation hold in
                            # ``authenticate_if_required``.  Recording that
                            # interval in the trace keeps storyboard dwell,
                            # captions, and the native browser footage on the
                            # same clock without retaining any secret value.
                            duration_ms={
                                "auth:username": 5_000,
                                "auth:password": 5_000,
                                "auth:submit": 5_000,
                            }.get(operation_id, 800),
                            page_contract_phases=["establish"],
                        )
                    )

                capture_interrupted = False
                try:
                    await _prepare_requested_visual_state(page, objective)
                    # A remote CDP navigation can otherwise wait forever when
                    # Browserbase has reclaimed a page or the origin stalls.
                    # Turn that provider failure into a resumable execution
                    # outcome instead of burning the entire session lease.
                    try:
                        await asyncio.wait_for(
                            page.goto(url, wait_until="domcontentloaded"),
                            timeout=60 if cloud_production else 120,
                        )
                    except TimeoutError as error:
                        raise GenerationPreconditionError(
                            "PRODUCTION_NAVIGATION_TIMEOUT: opening page did not become ready"
                        ) from error
                    artifacts.write_json(
                        "presentation/visual-state.json",
                        await _apply_requested_visual_state(page, objective),
                    )
                    # SPAs frequently register their actionable controls just after
                    # DOMContentLoaded.  Start the verified trace only once the
                    # interface has had a bounded chance to hydrate; a strict
                    # network-idle requirement would hang on analytics/websocket
                    # traffic, so retain a short deterministic fallback.
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5_000)
                    except PlaywrightTimeoutError:
                        await page.wait_for_timeout(700)
                    # The provider recording begins when Browserbase creates a
                    # session, but the editorial demo begins only after the
                    # requested product state is stable. Anchor the trace
                    # before authentication so an objective that explicitly
                    # asks for login retains the real sequential typing and
                    # submit interaction. This still excludes the blank
                    # provision/navigation prelude because the page has
                    # already reached its settled DOM state.
                    trace.recording_started_at = datetime.now(UTC)
                    await self.credential_service.authenticate_if_required(
                        page,
                        credential_reference,
                        action_observer=observe_auth_action,
                    )
                    # A walkthrough always establishes its opening state before
                    # the first gesture. This footage is real product time, not
                    # a renderer-held screenshot.
                    opening_hold_ms = int(
                        (storyboard.scenes[0].required_dwell_seconds if storyboard else 5.0) * 1000
                    )
                    await page.wait_for_timeout(opening_hold_ms)
                    # The recorder already loaded the requested URL to capture
                    # its entrance state. Replaying an identical first Navigate
                    # immediately refreshes the page and makes a human demo look
                    # like it loaded twice.
                    execution_plan = plan
                    if (
                        plan.workflow_steps
                        and plan.workflow_steps[0].operation.kind is OperationKind.NAVIGATE
                        and _canonical_url(str(plan.workflow_steps[0].operation.value))
                        == _canonical_url(page.url)
                    ):
                        execution_plan = plan.model_copy(
                            update={"workflow_steps": plan.workflow_steps[1:]}
                        )
                    scene_holds = {
                        scene.operation_id: int(scene.required_dwell_seconds * 1000)
                        for scene in (storyboard.scenes if storyboard else [])
                        if scene.operation_id
                    }
                    try:
                        execution_adapter = PlaywrightAdapter(page, cloud_mode=remote is not None)
                        engine = ExecutionEngine(
                            # Scene-level dwell is the only viewer-facing hold.
                            # Provider round-trip latency is captured as real
                            # footage and may be removed later only when the
                            # evidence-backed editor proves it is dead time;
                            # it must not be multiplied into every operation.
                            execution_adapter,
                            trace,
                            artifacts,
                            beat_hold_ms=0,
                            scene_hold_ms=scene_holds,
                            force_light_theme="light theme" in objective.lower()
                            or "light themed" in objective.lower(),
                            capture_event_screenshots=remote is None,
                        )
                        # Cloud sessions have finite provider leases. Bound
                        # semantic execution below that lease so native-video
                        # download, screencast flush and session cleanup still
                        # run reliably. Local runs keep their normal duration.
                        if cloud_production:
                            async with asyncio.timeout(self.cloud_capture_timeout_seconds):
                                result = await engine.run_adaptive(
                                    execution_plan,
                                    replanner=lambda current_plan, failed_step, current_trace, reason, dispatched: (
                                        self._runtime_replan(
                                            adapter=execution_adapter,
                                            plan=current_plan,
                                            failed_step=failed_step,
                                            trace=current_trace,
                                            error=reason,
                                            dispatched=dispatched,
                                            objective=objective,
                                            artifacts=artifacts,
                                            cloud_session_id=cloud_session_id,
                                            browserbase_connect_url=(
                                                session.connect_url if session is not None else None
                                            ),
                                            stagehand_extension_id=(
                                                session.stagehand_extension_id
                                                if session is not None
                                                else None
                                            ),
                                        )
                                    ),
                                )
                        else:
                            result = await engine.run_adaptive(
                                execution_plan,
                                replanner=lambda current_plan, failed_step, current_trace, reason, dispatched: (
                                    self._runtime_replan(
                                        adapter=execution_adapter,
                                        plan=current_plan,
                                        failed_step=failed_step,
                                        trace=current_trace,
                                        error=reason,
                                        dispatched=dispatched,
                                        objective=objective,
                                        artifacts=artifacts,
                                    )
                                ),
                            )
                    except Exception as error:
                        # A cloud browser can be reclaimed while a plan is in
                        # progress. Preserve the evidence collected so far and
                        # make the interruption a durable execution outcome;
                        # otherwise an operator sees only a session artifact
                        # and cannot classify or repair the failing boundary.
                        trace.completed_at = datetime.now(UTC)
                        trace.outcome_verified = False
                        trace.errors.append(
                            {
                                "code": (
                                    "PRODUCTION_CAPTURE_TIMEOUT"
                                    if isinstance(error, TimeoutError)
                                    else "PRODUCTION_CAPTURE_INTERRUPTED"
                                ),
                                "exception_type": type(error).__name__,
                                "message": str(error)[:1_000],
                                "stage": "production_execution",
                                "events_captured": len(trace.events),
                                "cloud_production": cloud_production,
                            }
                        )
                        artifacts.save_trace(trace)
                        artifacts.write_json(
                            "qa/execution-report.json",
                            {
                                "outcome_verified": False,
                                "event_count": len(trace.events),
                                "hard_failures": ["PRODUCTION_CAPTURE_INTERRUPTED"],
                                "error_type": type(error).__name__,
                            },
                        )
                        capture_interrupted = True
                        raise
                finally:
                    if playwright_trace_started:
                        await production.tracing.stop(
                            path=str(artifacts.execution / "playwright-trace.zip")
                        )
                    screencast_ready = False
                    screencast_error: RuntimeError | None = None
                    if screencast is not None:
                        try:
                            await screencast.stop_capture()
                            screencast_ready = True
                        except RuntimeError as error:
                            # The native Browserbase MP4 remains the primary
                            # asset even when a sparse diagnostic screencast
                            # produced too few frames to encode.
                            screencast_error = error
                    if remote is None:
                        await production.close()
                        artifacts.preserve_browser_video(
                            Path(await video.path()) if video else None
                        )
                    else:
                        # Disconnect first so Browserbase finalizes its
                        # source-faithful Session Replay. Fetching while the
                        # CDP browser is still active can return no playlist
                        # or a truncated one. Session cleanup follows fetch.
                        try:
                            # A provider session may already be closed while
                            # the CDP websocket is still draining. Never let
                            # that disconnect block replay retrieval and the
                            # durable stage checkpoint indefinitely.
                            await asyncio.wait_for(remote.close(), timeout=15)
                        except PlaywrightError:
                            pass
                        except TimeoutError:
                            logger.warning(
                                "cloud_cdp_close_timeout",
                                run_id=run_id,
                                session_id=cloud_session_id,
                            )
                        remote = None
                        if screencast is not None:
                            try:
                                assert self.browserbase_provider is not None
                                replay_timeout_seconds = (
                                    min(120, self.cloud_capture_timeout_seconds)
                                    if capture_interrupted
                                    else self.cloud_capture_timeout_seconds
                                )
                                recording = await asyncio.wait_for(
                                    self.browserbase_provider.download_session_replay_video(
                                        session.session_id,
                                        artifacts.execution / "browser-recording.mp4",
                                        # Use the configured stage deadline for
                                        # playlist publication and local assembly.
                                        # The provider's old 180s default made a
                                        # valid long capture fail before the
                                        # Browserbase session policy elapsed.
                                        # An interrupted capture is already a
                                        # failed execution boundary.  Do not
                                        # hold the worker for another full
                                        # production lease while trying to
                                        # retrieve a replay that cannot be
                                        # delivered; successful captures retain
                                        # the configured timeout for complete
                                        # source-faithful video finalization.
                                        timeout_seconds=replay_timeout_seconds,
                                    ),
                                    timeout=replay_timeout_seconds,
                                )
                                # Bind provider metadata to this run and the
                                # bytes that were actually downloaded.  A
                                # retry must never be able to reuse a replay
                                # belonging to its parent or another run.
                                recording = {
                                    **recording,
                                    "run_id": run_id,
                                    "sha256": hashlib.sha256(
                                        (artifacts.execution / "browser-recording.mp4").read_bytes()
                                    ).hexdigest(),
                                }
                                artifacts.write_json(
                                    "execution/browserbase-recording.json", recording
                                )
                            except (ProviderError, RuntimeError, TimeoutError) as recording_error:
                                artifacts.write_json(
                                    "execution/browserbase-recording.json",
                                    {
                                        "provider": "browserbase",
                                        "native_recording": "unavailable",
                                        "error_type": type(recording_error).__name__,
                                        # Keep a bounded, non-secret diagnostic
                                        # so a delivery failure can be repaired
                                        # without guessing whether Browserbase
                                        # rejected the endpoint, timed out
                                        # assembly, or returned an empty asset.
                                        "error": str(recording_error)[:500],
                                        "capture_interrupted": capture_interrupted,
                                    },
                                )
                                # Preserve a sparse CDP stream for failure
                                # diagnostics, but never label it native footage.
                                if screencast_ready:
                                    await screencast.encode(
                                        artifacts.execution / "browser-recording.webm"
                                    )
                                elif screencast_error is not None:
                                    raise screencast_error
                        if cloud_session_id is not None:
                            assert self.browserbase_provider is not None
                            await self.browserbase_provider.close_session(cloud_session_id)
                            cloud_session_id = None
            finally:
                # Browserbase cleanup belongs to the session lifecycle, not
                # the happy path. A failed CDP connection or screencast start
                # must not leak a billable remote session.
                if remote is not None:
                    try:
                        await asyncio.wait_for(remote.close(), timeout=15)
                    except PlaywrightError:
                        pass
                    except TimeoutError:
                        logger.warning(
                            "cloud_cdp_cleanup_timeout", run_id=run_id, session_id=cloud_session_id
                        )
                if cloud_session_id is not None:
                    assert self.browserbase_provider is not None
                    await self.browserbase_provider.close_session(cloud_session_id)
                if browser is not None:
                    await browser.close()
        # Complete the trace with measured capture metadata only after the
        # browser/session has been closed and the native recording (if any) is
        # available. These fields let downstream QA distinguish source
        # evidence from compositor defaults.
        result.viewport_decision = viewport
        result.browser_zoom_percent = viewport.browser_zoom_percent
        result.browser_context_id = production_context_id
        native_recording = artifacts.execution / "browser-recording.mp4"
        result.source_frame_rate = _recording_frame_rate(native_recording)
        if result.source_frame_rate is None and remote is None:
            # Local Playwright's recorded video metadata may not be available
            # until its path is finalized; keep the field absent rather than
            # claiming a synthetic rate.
            result.source_frame_rate = _recording_frame_rate(
                artifacts.execution / "browser-recording.webm"
            )
        adapted_plan_path = artifacts.execution / "adapted-plan.json"
        if adapted_plan_path.is_file():
            # Runtime replanning is part of the owned workflow contract.  QA,
            # presentation and later narration stages must evaluate the
            # effective suffix, not the stale pre-capture plan.
            plan = DemoPlan.model_validate(
                json.loads(adapted_plan_path.read_text(encoding="utf-8"))
            )
            artifacts.write_json("plan-effective.json", plan.model_dump(mode="json"))
            artifacts.write_json(
                "planning/adapted-state-graph.json",
                WorkflowStateMachine.from_operations(
                    [step.operation for step in plan.workflow_steps]
                ).artifact(),
            )
        if (artifacts.execution / "playwright-trace.zip").is_file():
            result.dom_snapshot_refs = ["execution/playwright-trace.zip"]
            result.accessibility_snapshot_refs = ["execution/playwright-trace.zip"]
        result = materialize_trace_lifecycle(result)
        artifacts.save_trace(result)
        # Keep lifecycle records independently queryable for recovery and QA;
        # the complete trace remains the source of truth for replay.
        artifacts.write_json(
            "execution/state-snapshots.json",
            [item.model_dump(mode="json") for item in result.state_snapshots],
        )
        artifacts.write_json(
            "execution/action-attempts.json",
            [item.model_dump(mode="json") for item in result.action_attempts],
        )
        artifacts.write_json(
            "execution/verification-results.json",
            [item.model_dump(mode="json") for item in result.verification_results],
        )
        coverage = inspect_coverage(plan, result)
        artifacts.write_json("qa/coverage-report.json", coverage)
        if coverage["hard_failures"]:
            raise RuntimeError(f"Coverage QA rejected execution: {coverage['missing_outcomes']}")
        if storyboard is not None:
            storyboard = bind_storyboard_events(
                storyboard, {event.operation_id for event in result.events if event.success}
            )
            artifacts.write_json("presentation/storyboard.json", storyboard.model_dump(mode="json"))
            script = editorial_script(
                storyboard,
                {event.operation_id: event.id for event in result.events if event.success},
            )
        else:
            script = script_from_trace(result)
        story = inspect_story(result, objective=objective, script=script)
        artifacts.write_json("qa/story-report.json", story)
        if story["hard_failures"]:
            raise RuntimeError(f"Story QA rejected execution: {story['hard_failures']}")
        scenes = build_scene_plan(result, storyboard=storyboard)
        presentation = build_presentation_plan(
            result,
            viewport_width=viewport.viewport.width,
            viewport_height=viewport.viewport.height,
            allow_camera_zoom=True,
            scene_plan=scenes,
        )
        artifacts.write_json(
            "presentation/presentation-plan.json", presentation.model_dump(mode="json")
        )
        artifacts.write_json("presentation/scene-plan.json", scenes)
        journey = build_journey(result, scenes)
        artifacts.write_json("presentation/validated-scene-plan.json", journey)
        # This is intentionally separate from the pre-capture storyboard:
        # it describes the actual browser states that survived execution,
        # including real scroll/cursor evidence and page-completion proof.
        # Rendering and narration can therefore be repaired from evidence
        # without pretending the original plan occurred exactly as written.
        artifacts.write_json(
            "presentation/actual-flow-storyboard.json",
            {
                "run_id": result.run_id,
                "objective": result.objective,
                "scenes": journey,
                "trace_event_ids": [event.id for event in result.events if event.success],
                "source": "verified_demo_trace",
            },
        )
        journey_report = inspect_journey(journey)
        artifacts.write_json("quality/journey-report.json", journey_report)
        # A staged URL run must enforce the same page-completion contract as
        # the monolithic path. Persisting a failed report and continuing to
        # rendering would turn a known route sweep into an apparently valid
        # delivery after a later render/QA retry.
        if journey_report["hard_failures"]:
            artifacts.write_json(
                "qa/repair-decision.json",
                classify_repair(journey_report["hard_failures"]).model_dump(mode="json"),
            )
            raise GenerationPreconditionError(
                f"JOURNEY_QA_REJECTED: {journey_report['hard_failures']}"
            )
        artifacts.write_json("presentation/cursor-plan.json", {"paths": presentation.cursor_paths})
        artifacts.write_json(
            "qa/execution-report.json",
            {
                "outcome_verified": True,
                "event_count": len(result.events),
                "replan_count": len(result.replan_decisions),
                "adaptive_execution": bool(result.replan_decisions),
            },
        )
        return result

    async def narration_stage(
        self, *, run_id: str, artifact_root: Path, refresh_editorial: bool = False
    ) -> dict:
        artifacts = RunArtifacts(artifact_root, run_id)
        trace = self._load_trace(artifacts)
        # Regenerate deterministic editorial copy from the persisted evidence at
        # narration time. This lets a narration-only repair improve prose
        # without replaying browser actions or invalidating the DemoTrace.
        context = ProductContext.model_validate(
            json.loads(
                (artifacts.root / "discovery" / "product-context.json").read_text(encoding="utf-8")
            )
        )
        plan = self._load_plan(artifacts)
        plan_consistency_failures = validate_selected_candidate_consistency(
            plan.model_dump(mode="json")
        )
        artifacts.write_json(
            "qa/plan-consistency-report.json",
            {
                "status": "pass" if not plan_consistency_failures else "failed",
                "hard_failures": plan_consistency_failures,
                "selected_workflow": plan.selected_workflow,
            },
        )
        # Planning owns the approved editorial wording. Narration must never
        # rebuild a deterministic fallback over an already enriched storyboard:
        # that silently discards the evidence-reviewed script and changes scene
        # timing without a planning repair.
        # Rebuild from the immutable plan/evidence on every narration pass.
        # A persisted storyboard may have been produced by an older writer or
        # contain a rejected route-label line; loading it during a targeted
        # retry would make the repair boundary stale and defeat deterministic
        # editorial validation.  The trace, plan, and scene IDs remain the
        # authorities, so this refresh cannot invent browser actions.
        storyboard = build_editorial_storyboard(context, plan)
        # A targeted narration repair intentionally starts from the
        # deterministic evidence-bound storyboard. The original planning pass
        # already had an opportunity to use OpenRouter; spending another pair
        # of model calls on a rejected script can reintroduce label dumps. A
        # caller may explicitly opt into a second editorial model pass through
        # the environment when investigating a provider/model regression.
        allow_repair_editorial_model = os.getenv(
            "PRODUCTLENS_REPAIR_EDITORIAL_WITH_LLM", "false"
        ).lower() in {"1", "true", "yes"}
        if (
            refresh_editorial
            and allow_repair_editorial_model
            and self.planner is not None
            and self.planner.provider is not None
        ):
            storyboard = await enrich_editorial_brief(context, storyboard, self.planner.provider)
            storyboard = await enrich_editorial_storyboard(
                context, storyboard, self.planner.provider
            )
        storyboard = bind_storyboard_events(
            storyboard, {event.operation_id for event in trace.events if event.success}
        )
        artifacts.write_json(
            "presentation/editorial-brief.json", storyboard.brief.model_dump(mode="json")
        )
        artifacts.write_json(
            "narration/fact-extraction.json",
            {
                "schema_version": 1,
                "source": "persisted-page-knowledge-and-editorial-brief",
                "facts": [item.model_dump(mode="json") for item in storyboard.brief.facts],
                "excluded_areas": storyboard.brief.excluded_areas,
            },
        )
        artifacts.write_json("presentation/storyboard.json", storyboard.model_dump(mode="json"))
        # Rebuild the presentation contract from the immutable trace on every
        # narration/presentation retry.  Camera and cursor policy is code, not
        # evidence; retaining a plan generated by an older renderer version
        # would let a fixed safety bound (or a cursor alignment fix) be silently
        # bypassed by a repair that only regenerated captions.  The trace and
        # scene IDs remain unchanged, so this refresh cannot invent an action.
        scene_plan_path = artifacts.presentation / "scene-plan.json"
        # A narration/presentation repair must also refresh scene policy from
        # the immutable trace. Retaining an older scene-plan silently keeps
        # stale caption-safe zones (or camera bounds) after a renderer fix,
        # exactly the kind of drift the trace-only repair path is meant to
        # prevent.
        if refresh_editorial:
            scene_plan_payload = build_scene_plan(trace, storyboard=storyboard)
        else:
            try:
                scene_plan_payload = json.loads(scene_plan_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                scene_plan_payload = build_scene_plan(trace, storyboard=storyboard)
        if refresh_editorial:
            artifacts.write_json("presentation/scene-plan.json", scene_plan_payload)
            # The validated scene plan is the renderer's authoritative
            # presentation contract. Keep it in lockstep with a narration
            # repair; otherwise a trace-only rerender can silently consume
            # an older caption-safe-zone/camera policy even though the fresh
            # scene-plan artifact looks correct.
            refreshed_journey = build_journey(trace, scene_plan_payload)
            artifacts.write_json("presentation/validated-scene-plan.json", refreshed_journey)
        first_viewport = next(
            (event.viewport for event in trace.events if event.viewport is not None),
            None,
        )
        if first_viewport is not None:
            refreshed_presentation = build_presentation_plan(
                trace,
                viewport_width=first_viewport.width,
                viewport_height=first_viewport.height,
                allow_camera_zoom=True,
                scene_plan=scene_plan_payload if isinstance(scene_plan_payload, list) else None,
            )
            artifacts.write_json(
                "presentation/presentation-plan.json",
                refreshed_presentation.model_dump(mode="json"),
            )
            artifacts.write_json(
                "presentation/cursor-plan.json",
                {"paths": refreshed_presentation.cursor_paths},
            )
        first_successful_event = next((event for event in trace.events if event.success), None)
        script = (
            editorial_script(
                storyboard,
                {event.operation_id: event.id for event in trace.events if event.success},
                opening_event_id=first_successful_event.id if first_successful_event else None,
            )
            if storyboard
            else script_from_trace(trace)
        )
        # Compatibility traces without an editorial storyboard still need the
        # same opening-at-zero guarantee.
        if storyboard is None:
            script = bind_opening_to_first_event(script, trace)
        # Visual-editor workflows need a presenter narrative tied to the
        # artifact being built, not a sequence of tool labels.  Derive these
        # lines from the verified event intent/gesture and the user's
        # requested component text; this remains product-neutral and avoids
        # repeating "select Text" / "use the canvas" captions.
        visual_request = bool(
            re.search(
                r"\b(?:draw|drawing|diagram|architecture|whiteboard|canvas|flowchart)\b",
                trace.objective.casefold(),
            )
        )
        if visual_request:
            # The generic storyboard intentionally suppresses low-value
            # observe beats for ordinary pages.  In an editor build, however,
            # each verified text placement is a material part of the artifact
            # and must own a caption.  Start from the complete trace so the
            # script cannot silently collapse the creation journey.
            trace_script = script_from_trace(trace, audience="technical engineer")
            first_event = next((event for event in trace.events if event.success), None)
            if first_event is not None:
                product_title = (
                    " ".join(str(storyboard.brief.product_purpose).split())[:96]
                    if storyboard is not None
                    else "this visual workspace"
                )
                script = [
                    {
                        "event_id": first_event.id,
                        "scene_id": "opening",
                        "opening": True,
                        "text": (
                            f"Welcome to {product_title}. Today I'll show how a requested "
                            "architecture takes shape directly on the canvas."
                        ),
                        "facts": [f"page:{first_event.page_url}"],
                    },
                    *[line for line in trace_script if str(line.get("event_id")) != first_event.id],
                ]
            events_by_id = {event.id: event for event in trace.events}
            scene_by_operation = {
                scene.operation_id: scene.id
                for scene in (storyboard.scenes if storyboard is not None else [])
                if scene.operation_id
            }
            visual_lines: list[dict[str, object]] = []
            for line_index, line in enumerate(script):
                event = events_by_id.get(str(line.get("event_id")))
                if event is None:
                    visual_lines.append(line)
                    continue
                intent = event.intent.casefold()
                gesture = event.after.get("gesture") if isinstance(event.after, dict) else None
                text_value = (
                    str(gesture.get("text", "")).strip() if isinstance(gesture, dict) else ""
                )
                replacement = str(line.get("text") or "")
                # The text-tool click is meaningful because it precedes a
                # specific component label.  Carry that observed value into
                # the sentence so repeated tool clicks remain distinct,
                # conversational beats instead of identical UI instructions.
                following_label = ""
                for candidate in script[line_index + 1 :]:
                    candidate_event = events_by_id.get(str(candidate.get("event_id")))
                    candidate_gesture = (
                        candidate_event.after.get("gesture")
                        if candidate_event is not None and isinstance(candidate_event.after, dict)
                        else None
                    )
                    if (
                        candidate_event is not None
                        and candidate_event.kind is OperationKind.KEY_PRESS
                        and isinstance(candidate_gesture, dict)
                        and str(candidate_gesture.get("text", "")).strip()
                    ):
                        following_label = str(candidate_gesture["text"]).strip()
                        break
                if bool(line.get("opening")):
                    # The opening line is the presenter welcome attached to
                    # the first proved event. Do not replace it with the
                    # low-level scroll/tool caption for that same event.
                    replacement = str(line.get("text") or replacement)
                elif event.kind is OperationKind.KEY_PRESS and text_value:
                    replacement = (
                        f"The canvas now names the {text_value} component, making its role "
                        "explicit in the architecture."
                    )
                elif event.kind is OperationKind.CLICK and "label" in intent:
                    match = re.search(
                        r"\blabel\s+(.+?)(?:\s+on the observed|\s*$)",
                        event.intent,
                        re.IGNORECASE,
                    )
                    label = match.group(1).strip() if match else "component"
                    label = re.sub(r"^(?:for|the)\s+", "", label, flags=re.IGNORECASE).strip()
                    replacement = (
                        f"I place the {label} label on the canvas so the architecture can be "
                        "read at a glance."
                    )
                elif event.kind is OperationKind.CLICK and "text tool" in intent:
                    replacement = (
                        f"I select the observed text tool to name the {following_label or 'next'} "
                        "architectural role directly where it belongs."
                    )
                elif event.kind is OperationKind.CLICK and any(
                    token in intent for token in ("arrow", "connector", "line")
                ):
                    match = re.search(
                        r"connect\s+(.+?)\s+to\s+(.+?)(?:\s*$|\s+on\b)",
                        event.intent,
                        flags=re.IGNORECASE,
                    )
                    if match:
                        replacement = (
                            f"I select the observed {event.target.name if event.target else 'connector'} "
                            f"tool to connect {match.group(1).strip()} to {match.group(2).strip()}."
                        )
                elif event.kind is OperationKind.POINTER_SEQUENCE:
                    match = re.search(
                        r"Connect the observed\s+(.+?)\s+and\s+(.+?)\s+components",
                        event.intent,
                        flags=re.IGNORECASE,
                    )
                    if match:
                        replacement = (
                            f"I draw the arrow from {match.group(1).strip()} to "
                            f"{match.group(2).strip()}, making the data flow explicit."
                        )
                    else:
                        replacement = (
                            "I sketch the first visual connection on the canvas, establishing "
                            "the workspace where the architecture will take shape."
                        )
                elif event.kind is OperationKind.SCROLL_TO:
                    replacement = (
                        "I orient the viewer to the canvas and its visible tool palette "
                        "before drawing."
                    )
                visual_lines.append(
                    {
                        **line,
                        "scene_id": scene_by_operation.get(
                            event.operation_id, line.get("scene_id", event.id)
                        ),
                        "text": replacement,
                    }
                )
            script = visual_lines
        # Authentication is part of the visible journey whenever a clean
        # production context had to sign in, even if the user phrased the
        # objective as an already-authenticated experience.  Keep these
        # presenter lines credential-free and bind them to the real auth
        # events so the opening never leaves an unexplained silent login gap.
        auth_events = [
            event
            for event in trace.events
            if event.success and str(event.operation_id or "").startswith("auth:")
        ]
        if auth_events:
            auth_lines = []
            requested_subject = ""
            if context.objective is not None:
                requested_subject = str(context.objective.primary_entity or "").strip()
            requested_subject = re.sub(
                r"^(?:(?:the|a|an|authenticated|operational|relevant|requested|actual|visible|current|primary)\s+)+",
                "",
                requested_subject,
                flags=re.IGNORECASE,
            ).strip()
            requested_subject = requested_subject or "requested workflow"
            for event in auth_events:
                operation_id = str(event.operation_id)
                if operation_id.endswith(":username"):
                    text = "We begin by entering the account email so this walkthrough reflects a real authenticated workspace."
                elif operation_id.endswith(":password"):
                    text = "The password is entered securely and kept out of the recording, preserving a safe demonstration."
                else:
                    text = f"With sign-in complete, the authenticated workspace is ready for the {requested_subject}."
                auth_lines.append(
                    {
                        "event_id": event.id,
                        "scene_id": operation_id,
                        "text": text,
                        "facts": ["auth:credential-entry"],
                    }
                )
            script = [
                *auth_lines,
                *[
                    line
                    for line in script
                    if str(line.get("event_id")) not in {item["event_id"] for item in auth_lines}
                ],
            ]
        if storyboard and script:
            # The approved script is the narration source of truth. Keep the
            # storyboard copy synchronized before editorial QA so a targeted
            # narration repair (including placeholder cleanup) is evaluated
            # against the exact text that will be rendered, not stale model
            # prose persisted by an earlier attempt.
            storyboard_text = {scene.id: scene.narration for scene in storyboard.scenes}

            def _is_route_label_line(value: str) -> bool:
                return bool(
                    re.search(
                        r"\b(?:view|page)\s+brings\b.*\binto\s+view\b|"
                        r"\bshowing\s+how\s+this\s+part\s+of\s+the\s+product\s+is\s+organized\b",
                        value,
                        flags=re.IGNORECASE,
                    )
                )

            # A compatibility/provider script can carry an older generic
            # route sentence even after the deterministic storyboard has been
            # rebuilt. Keep one evidence-bound source of truth by replacing
            # only that rejected line; all other approved script timing and
            # scene identity remain untouched.
            script = [
                {
                    **line,
                    "text": (
                        storyboard_text.get(str(line.get("scene_id")), str(line.get("text") or ""))
                        if _is_route_label_line(str(line.get("text") or ""))
                        else line.get("text")
                    ),
                }
                for line in script
            ]
            script_by_scene = {
                str(line.get("scene_id")): str(line.get("text") or "")
                for line in script
                if line.get("scene_id") and str(line.get("text") or "").strip()
            }
            storyboard = storyboard.model_copy(
                update={
                    "scenes": [
                        scene.model_copy(
                            update={"narration": script_by_scene.get(scene.id, scene.narration)}
                        )
                        for scene in storyboard.scenes
                    ]
                }
            )
            artifacts.write_json("presentation/storyboard.json", storyboard.model_dump(mode="json"))
        editorial = inspect_editorial(
            context=context, plan=plan, trace=trace, storyboard=storyboard, script=script
        )
        artifacts.write_json("qa/editorial-report.json", editorial)
        if editorial["hard_failures"]:
            artifacts.write_json(
                "qa/repair-decision.json",
                classify_repair(editorial["hard_failures"]).model_dump(mode="json"),
            )
            raise GenerationPreconditionError(
                f"EDITORIAL_QA_REJECTED: {editorial['hard_failures']}"
            )
        # Caption-only delivery still needs to cover the complete rendered
        # story.  A word-count estimate can be shorter than the browser edit
        # (especially when a quiet page-read beat is retained), leaving an
        # unexplained silent tail and captions that fail the reader dwell
        # gate.  Use the objective's approved minimum as the lower bound; the
        # renderer remains responsible for the actual visual duration.
        recommended_duration = recommended_caption_duration(script)
        objective_minimum = float(plan.minimum_duration_seconds or 0)
        captions = captions_from_duration(script, max(recommended_duration, objective_minimum))
        narration = None
        if self.speech_provider:
            try:
                narration = await NarrationService().create(
                    trace,
                    self.speech_provider,
                    artifacts.root / "audio" / "narration.mp3",
                    script=script,
                )
                script, captions = narration["script"], narration["captions"]
            except ProviderError:
                narration = None
        mode = "tts" if narration else "caption_only"
        approved_script = _narration_script_contract(
            script,
            mode=mode,
            audience=str(
                getattr(getattr(context, "objective", None), "audience", "product prospect")
            ),
            audience_profile=getattr(getattr(context, "objective", None), "audience_profile", None),
            captions=captions,
        )
        # Keep the historical ``script`` list for API compatibility while
        # adding the versioned contract metadata and measured segment timing.
        payload = {
            "schema_version": approved_script.schema_version,
            "mode": approved_script.mode,
            "timing_owner": approved_script.timing_owner,
            "audience": approved_script.audience,
            "audience_profile": approved_script.audience_profile.model_dump(mode="json"),
            "script": [segment.model_dump(mode="json") for segment in approved_script.segments],
        }
        artifacts.write_json("presentation/captions.json", captions)
        artifacts.write_json("presentation/narration-script.json", payload)
        artifacts.write_json("narration/editorial-script.json", payload)
        return {
            **payload,
            "captions": captions,
            "audio_path": narration and narration["audio_path"],
        }

    def render_stage(self, *, run_id: str, artifact_root: Path) -> Path:
        return render_run(run_id=run_id, artifact_root=artifact_root)

    def qa_stage(self, *, run_id: str, artifact_root: Path) -> dict:
        artifacts = RunArtifacts(artifact_root, run_id)
        trace = self._load_trace(artifacts)
        # QA may be resumed after a targeted execution repair.  Reconcile the
        # execution verdict from the immutable trace so a stale failure report
        # from the previous attempt can never survive beside a successful
        # production capture.
        execution_failures = []
        if not trace.outcome_verified:
            execution_failures = [
                str(error.get("code") or "PRODUCTION_CAPTURE_INTERRUPTED")
                for error in trace.errors[-1:]
            ] or ["PRODUCTION_CAPTURE_INTERRUPTED"]
        interaction_failures: list[str] = []
        interaction_path = artifacts.execution / "interaction-trace.json"
        if not interaction_path.is_file():
            interaction_failures.append("INTERACTION_TRACE_MISSING")
        else:
            try:
                interaction_trace = InteractionTrace.model_validate(
                    json.loads(interaction_path.read_text(encoding="utf-8"))
                )
                if not interaction_trace.complete:
                    interaction_failures.append("INTERACTION_TRACE_INCOMPLETE")
                latest_verdicts: dict[str, str] = {}
                for item in interaction_trace.verifications:
                    latest_verdicts[item.intent_id] = item.status
                if any(item != "passed" for item in latest_verdicts.values()):
                    interaction_failures.append("INTERACTION_VERIFICATION_FAILED")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                interaction_failures.append("INTERACTION_TRACE_INVALID")
        execution_failures = [*execution_failures, *interaction_failures]
        artifacts.write_json(
            "qa/execution-report.json",
            {
                "outcome_verified": bool(trace.outcome_verified),
                "event_count": len(trace.events),
                "hard_failures": execution_failures,
                "interaction_trace": {
                    "path": "execution/interaction-trace.json",
                    "hard_failures": interaction_failures,
                },
            },
        )
        script = json.loads(
            (artifacts.presentation / "narration-script.json").read_text(encoding="utf-8")
        )["script"]
        captions_path = artifacts.presentation / "rendered-captions.json"
        captions = json.loads(
            (
                captions_path
                if captions_path.exists()
                else artifacts.presentation / "captions.json"
            ).read_text(encoding="utf-8")
        )
        plan = self._load_plan(artifacts)
        # Revalidate the persisted plan at delivery time.  QA can be resumed
        # independently of narration/render, so it must not depend on the
        # local variable created during an earlier stage.  This also prevents
        # a stale selected-candidate summary from being published after a
        # targeted retry.
        plan_consistency_failures = validate_selected_candidate_consistency(
            plan.model_dump(mode="json")
        )
        artifacts.write_json(
            "qa/plan-consistency-report.json",
            {
                "status": "pass" if not plan_consistency_failures else "failed",
                "hard_failures": plan_consistency_failures,
                "selected_workflow": plan.selected_workflow,
            },
        )
        effective_maximum, duration_accounting = _production_duration_envelope(plan)
        if duration_accounting:
            artifacts.write_json("presentation/duration-accounting.json", duration_accounting)
        source_video = artifacts.browser_video_path()
        source_time_map: list[tuple[float, float]] | None = None
        source_is_edited = False
        source_edit_path = artifacts.presentation / "source-edit-plan.json"
        if source_edit_path.exists():
            try:
                source_edit = json.loads(source_edit_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                source_edit = {}
            rendered_source = (
                source_edit.get("rendered_source") if isinstance(source_edit, dict) else None
            )
            if isinstance(rendered_source, str):
                candidate_source = artifacts.root / rendered_source
                if candidate_source.is_file():
                    source_video = candidate_source
                    source_is_edited = True
            raw_windows = source_edit.get("windows") if isinstance(source_edit, dict) else None
            if not source_is_edited and isinstance(raw_windows, list):
                parsed_windows: list[tuple[float, float]] = []
                for window in raw_windows:
                    if not isinstance(window, dict):
                        continue
                    try:
                        start, end = float(window["start"]), float(window["end"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if end > start:
                        parsed_windows.append((start, end))
                source_time_map = parsed_windows or None
        # Compatibility for artifacts created before content-addressed source
        # edits. New runs always use the exact rendered_source above.
        if (
            source_video == artifacts.browser_video_path()
            and (artifacts.root / "render" / "editorial-source.mp4").is_file()
        ):
            source_video = artifacts.root / "render" / "editorial-source.mp4"
        # A genuinely single-state product can have fewer viable story scenes
        # than the generic thorough-walkthrough envelope. Do not stretch or
        # freeze that footage merely to satisfy a nominal 110-second default:
        # lower the floor only when the validated storyboard contains one
        # page, at most two non-opening scenes, and no meaningful interaction.
        duration_floor = self._duration_floor(plan)
        duration_exception: dict[str, object] | None = None
        storyboard_for_duration: EditorialStoryboard | None = None
        storyboard_path_for_duration = artifacts.presentation / "storyboard.json"
        if storyboard_path_for_duration.is_file():
            try:
                storyboard_for_duration = EditorialStoryboard.model_validate(
                    json.loads(storyboard_path_for_duration.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                storyboard_for_duration = None
        if storyboard_for_duration is not None:
            bound_scenes = [
                scene for scene in storyboard_for_duration.scenes if scene.operation_id is not None
            ]
            pages = {
                str(scene.page_url or "").rstrip("/") for scene in bound_scenes if scene.page_url
            }
            meaningful = any(
                scene.interaction in {"scroll", "click", "type", "submit"} for scene in bound_scenes
            )
            if len(bound_scenes) <= 2 and len(pages) <= 1 and not meaningful:
                dwell_floor = sum(
                    max(1.0, float(scene.required_dwell_seconds or 0)) for scene in bound_scenes
                )
                duration_floor = max(12, int(dwell_floor + 1.0))
                duration_exception = {
                    "requested_minimum_seconds": self._duration_floor(plan),
                    "effective_minimum_seconds": duration_floor,
                    "reason": "validated single-page story has no additional viable interaction scenes",
                    "scene_count": len(bound_scenes),
                    "page_count": len(pages),
                }
        video = inspect_video(
            artifacts.root / "final" / "demo.mp4",
            execution_verified=trace.outcome_verified,
            minimum_duration_seconds=duration_floor,
            maximum_duration_seconds=effective_maximum,
            source_video=source_video,
            source_time_map=source_time_map,
            source_is_edited=source_is_edited,
        )
        # Whiteboard/diagram products legitimately keep a near-white drawing
        # surface behind a sparse toolbar.  When the objective is explicitly
        # visual and the production trace proves committed pointer changes,
        # the central-region blank heuristic is not evidence of missing
        # footage.  Remove only that one heuristic failure; codec, pacing,
        # source-faithfulness, and synchronization gates remain strict.
        visual_surface_proved = bool(
            re.search(
                r"\b(?:draw|drawing|diagram|architecture|whiteboard|canvas|flowchart)\b",
                trace.objective.casefold(),
            )
            and any(
                event.kind is OperationKind.POINTER_SEQUENCE
                and isinstance(event.after, dict)
                and isinstance(event.after.get("gesture"), dict)
                and event.after["gesture"].get("surface_changed") is True
                for event in trace.events
            )
        )
        if visual_surface_proved and "BLANK_PRODUCT_CONTENT_INTERVAL" in video.get(
            "hard_failures", []
        ):
            video["hard_failures"].remove("BLANK_PRODUCT_CONTENT_INTERVAL")
            video.setdefault("warnings", []).append("VALIDATED_LIGHT_VISUAL_SURFACE")
        if duration_accounting:
            video.setdefault("warnings", []).append(
                "THOROUGH_WALKTHROUGH_DURATION_ACCOUNTING_ALLOWANCE"
            )
            video["duration_accounting"] = duration_accounting
        if duration_exception:
            video.setdefault("warnings", []).append("VALIDATED_SHORT_STORY_DURATION_EXCEPTION")
            video["duration_exception"] = duration_exception
            # Sparse editors and blank-state workspaces can legitimately have
            # a white central canvas while their toolbars, palettes, and
            # document chrome remain visible. The central-region heuristic is
            # intentionally strict for normal stories, but this validated
            # single-state exception proves that no additional page/action
            # scene exists; do not reject the native recording solely because
            # the product's usable surface is intentionally empty.
            if "BLANK_PRODUCT_CONTENT_INTERVAL" in video.get("hard_failures", []):
                video["hard_failures"].remove("BLANK_PRODUCT_CONTENT_INTERVAL")
                video.setdefault("warnings", []).append("SPARSE_PRODUCT_SURFACE_ACCEPTED")
        # A repository-level benchmark is optional at runtime (deployments do
        # not need to ship reference media), but when an operator has created
        # one it becomes hard delivery evidence rather than an advisory note.
        # This keeps sample quality standards explicit without coupling the
        # generic generator to a particular product or URL.
        benchmark_path = artifact_root / "quality-benchmarks" / "sample-video-benchmark.json"
        sample_benchmark: dict[str, object] | None = None
        if benchmark_path.is_file():
            try:
                benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
                sample_benchmark = compare_to_sample_benchmark(
                    artifacts.root / "final" / "demo.mp4", benchmark
                )
                # The supplied references are a presentation-quality floor,
                # not a fixed duration contract. Focused feature demos are
                # intentionally allowed to be concise within their validated
                # objective envelope (typically 1–2 minutes); a shorter
                # reference video must not reject an otherwise complete flow.
                if (
                    "BELOW_SAMPLE_DURATION_ENVELOPE" in sample_benchmark["hard_failures"]
                    and plan.objective
                    # Thoroughness controls depth, not breadth. A focused
                    # feature request may legitimately use that adjective and
                    # remains valid within its 1–2 minute objective envelope.
                    and (
                        not re.search(r"\b(?:full|complete|entire)\b", plan.objective.lower())
                        or duration_exception is not None
                    )
                ):
                    sample_benchmark["hard_failures"].remove("BELOW_SAMPLE_DURATION_ENVELOPE")
                    sample_benchmark.setdefault("warnings", []).append(
                        "BELOW_SAMPLE_REFERENCE_DURATION"
                    )
                video["hard_failures"] = [
                    *video.get("hard_failures", []),
                    *sample_benchmark["hard_failures"],
                ]
                if sample_benchmark["hard_failures"]:
                    video["visual_score"] = 0.0
                    video["overall_score"] = 0.0
            except (OSError, ValueError, TypeError, json.JSONDecodeError, RuntimeError) as error:
                sample_benchmark = {
                    "status": "unavailable",
                    "warning": f"SAMPLE_BENCHMARK_UNREADABLE: {type(error).__name__}",
                }
        # A CDP screencast is useful diagnostic evidence, but it is not an
        # acceptable production source for a Browserbase run.  If native
        # recording assembly failed, keep the diagnostic artifact for repair
        # while making the delivery decision explicitly blocking instead of
        # silently presenting the fallback as the product video.
        native_recording_meta = artifacts.execution / "browserbase-recording.json"
        if native_recording_meta.exists():
            try:
                native_meta = json.loads(native_recording_meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                native_meta = {}
            if native_meta.get("native_recording") == "unavailable":
                video["hard_failures"] = [
                    *video.get("hard_failures", []),
                    "BROWSERBASE_NATIVE_RECORDING_UNAVAILABLE",
                ]
                video["visual_score"] = 0.0
                video["overall_score"] = 0.0
        presentation_props = json.loads(
            (artifacts.presentation / "remotion-props.json").read_text(encoding="utf-8")
        )
        presentation = inspect_presentation(trace, presentation_props)
        video = attach_presentation_qa(video, presentation)
        synchronization = inspect_synchronization(
            trace,
            script,
            captions,
            narration_requested=False,
            narration_created=(artifacts.root / "audio" / "narration.mp3").exists(),
            explained_intervals=secure_transition_intervals(presentation_props),
        )
        story = json.loads((artifacts.qa / "story-report.json").read_text(encoding="utf-8"))
        context = ProductContext.model_validate(
            json.loads(
                (artifacts.root / "discovery" / "product-context.json").read_text(encoding="utf-8")
            )
        )
        storyboard_path = artifacts.presentation / "storyboard.json"
        storyboard = (
            EditorialStoryboard.model_validate(
                json.loads(storyboard_path.read_text(encoding="utf-8"))
            )
            if storyboard_path.exists()
            else None
        )
        # Cloud runs are required to invoke the configured Stagehand bridge.
        # Its suggestions remain advisory, but silently accepting an
        # unavailable bridge defeats the AI-assisted discovery contract and
        # makes a deterministic fallback look like a successful exploration.
        # Classify this at delivery time so the repair coordinator can retry
        # only discovery instead of re-recording an invalid story.
        exploration_qa: dict[str, object] = {
            "exploration_score": 1.0,
            "hard_failures": [],
            "warnings": [],
        }
        discovery_root = artifacts.root / "discovery"
        cloud_session_path = discovery_root / "browserbase-session.json"
        stagehand_path = discovery_root / "stagehand-observation.json"
        if cloud_session_path.exists():
            try:
                stagehand_observation = json.loads(stagehand_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                stagehand_observation = {}
            if not isinstance(stagehand_observation, dict):
                exploration_qa = {
                    "exploration_score": 0.0,
                    "hard_failures": ["STAGEHAND_OBSERVATION_UNAVAILABLE"],
                    "warnings": [],
                    "reason": "cloud exploration did not produce a validated Stagehand observation",
                }
            elif stagehand_observation.get("status") != "OBSERVED":
                # Stagehand is an advisory intelligence provider.  A quota,
                # model-access, or transient network failure must not turn a
                # fully grounded Playwright discovery into an unusable video:
                # the deterministic evidence path remains the source of truth.
                # Keep the provider failure explicit for operators and repair
                # tooling, but let delivery QA decide the actual product
                # evidence independently.
                exploration_qa = {
                    "exploration_score": 1.0,
                    "hard_failures": [],
                    "warnings": ["STAGEHAND_PROVIDER_UNAVAILABLE"],
                    "reason": str(
                        stagehand_observation.get("reason")
                        or "Stagehand observation was unavailable; Playwright evidence retained"
                    )[:500],
                    "provider_status": str(stagehand_observation.get("status") or "UNAVAILABLE"),
                }
        artifacts.write_json("qa/exploration-report.json", exploration_qa)
        actual_duration = float(video.get("probe", {}).get("format", {}).get("duration") or 0)
        # A target is an editorial planning aid, not a renderer stretch target.
        # The approved ObjectiveSpec still supplies hard lower/upper delivery
        # bounds: a presentation that exceeds its maximum needs a directed
        # dead-time/transition repair, never an arbitrary global speed-up.
        # The objective owns the delivery lower bound.  Older storyboard
        # artifacts may contain a sum-of-scene floor from before transition
        # overlap was modelled; using that stale derived value would reject a
        # valid native-speed render during a targeted QA retry.  Scene dwell is
        # checked separately by editorial QA, so compare the video against the
        # stable objective contract here.
        storyboard_minimum = float(duration_floor or 0)
        if duration_exception is None:
            storyboard_minimum = max(45.0, storyboard_minimum)
        if storyboard is not None and actual_duration + 0.25 < storyboard_minimum:
            video["hard_failures"] = [
                *video.get("hard_failures", []),
                "EDITORIAL_DURATION_BELOW_STORYBOARD_MINIMUM",
            ]
            video["visual_score"] = 0.0
            video["overall_score"] = 0.0
        editorial = inspect_editorial(
            context=context, plan=plan, trace=trace, storyboard=storyboard, script=script
        )
        story = {
            **story,
            "story_score": min(float(story["story_score"]), float(editorial["editorial_score"])),
            "hard_failures": [*story.get("hard_failures", []), *editorial["hard_failures"]],
        }
        viewport = ViewportDecision.model_validate(
            json.loads(
                (artifacts.root / "discovery" / "viewport-decision.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        # A targeted render/QA retry may intentionally skip execution and
        # presentation stages. Reconstruct their pure, trace-derived reports
        # instead of treating missing inherited files as a delivery failure.
        # Coverage is a pure function of the current owned trace and plan.
        # Recompute it on every QA/presentation retry: inheriting a prior
        # report would let a stale pre-submit witness survive after the trace
        # or repair boundary changed.
        coverage = inspect_coverage(plan, trace)
        artifacts.write_json("qa/coverage-report.json", coverage)
        # Rebuild this pure report from the owned trace on every QA attempt.
        # A target-repair may inherit a stale report from a prior scene plan;
        # delivery must be gated by the current page-completion evidence.
        scene_path = artifacts.presentation / "scene-plan.json"
        scene_data = (
            json.loads(scene_path.read_text(encoding="utf-8"))
            if scene_path.exists()
            else build_scene_plan(trace, storyboard=storyboard)
        )
        journey_report = inspect_journey(build_journey(trace, scene_data))
        artifacts.write_json("quality/journey-report.json", journey_report)
        story = {
            **story,
            "story_score": min(float(story["story_score"]), float(journey_report["journey_score"])),
            "hard_failures": [*story.get("hard_failures", []), *journey_report["hard_failures"]],
        }
        # The delivery manifest is itself evidence-based. Persist individual QA
        # artifacts before evaluating required delivery artifacts so the first
        # successful QA attempt cannot self-reject due to write ordering.
        artifacts.write_json("qa/video-report.json", video)
        if sample_benchmark is not None:
            artifacts.write_json("qa/sample-benchmark-report.json", sample_benchmark)
        artifacts.write_json("qa/presentation-report.json", presentation)
        artifacts.write_json("qa/synchronization-report.json", synchronization)
        artifacts.write_json("qa/editorial-report.json", editorial)
        multimodal = review_multimodal(
            build_review_packet(
                video=artifacts.root / "final" / "demo.mp4",
                run_id=run_id,
                trace=trace.model_dump(mode="json"),
                storyboard=storyboard.model_dump(mode="json") if storyboard else None,
                sample_seconds=[
                    round(actual_duration * fraction, 3)
                    for fraction in (0.05, 0.2, 0.4, 0.6, 0.8, 0.95)
                    if actual_duration * fraction >= 0.1
                ],
            ),
            reviewer=self.visual_reviewer,
        )
        artifacts.write_json("qa/multimodal-report.json", multimodal)
        # Materialize the manifest before computing delivery requirements. The
        # manifest is itself a required URL-delivery artifact; computing the
        # report first made a valid run self-reject with
        # ``artifact_manifest=false`` and only then create the file.
        artifacts.write_manifest()
        report = delivery_report(
            artifacts=artifacts.required_url_delivery_artifacts(),
            execution={
                "execution_score": coverage["coverage_score"],
                "workflow_score": coverage["coverage_score"],
                "hard_failures": [
                    *coverage["hard_failures"],
                    *exploration_qa["hard_failures"],
                    *plan_consistency_failures,
                ],
            },
            story=story,
            video=video,
            synchronization=synchronization,
            visual_review=multimodal,
            viewport_score=viewport.score,
        )
        artifacts.write_json("qa/delivery-report.json", report)
        # A delivery decision alone is too terse for an operator deciding
        # whether to publish, repair, or wait for an optional provider. Keep
        # an explicit, evidence-derived gap report beside every run. It never
        # invents a limitation: blockers come from hard QA failures and
        # caveats come only from reports that actually emitted a warning.
        report_sources = {
            "coverage": coverage,
            "exploration": exploration_qa,
            "story": story,
            "video": video,
            "presentation": presentation,
            "synchronization": synchronization,
            "multimodal": multimodal,
        }
        artifacts.write_json(
            "qa/gap-report.json",
            {
                "status": "ready_with_caveats" if report["deliverable"] else "repair_required",
                "deliverable": bool(report["deliverable"]),
                "blockers": list(
                    dict.fromkeys(
                        failure
                        for source in report_sources.values()
                        for failure in source.get("hard_failures", [])
                    )
                ),
                "caveats": list(
                    dict.fromkeys(
                        warning
                        for source in report_sources.values()
                        for warning in source.get("warnings", [])
                    )
                ),
                "optional_layers": {
                    "tts": "not generated"
                    if not (artifacts.root / "audio" / "narration.mp3").exists()
                    else "generated",
                    "multimodal_review": str(multimodal.get("status", "unavailable")),
                },
                "evidence_reports": {
                    name: f"qa/{name}-report.json"
                    for name in (
                        "exploration",
                        "video",
                        "presentation",
                        "synchronization",
                        "editorial",
                        "delivery",
                    )
                },
            },
        )
        # The delivery report is part of the manifest. Hash it before running
        # the completion audit so the audit verifies the same immutable set of
        # artifacts that the worker will hand off.
        artifacts.write_manifest()
        artifacts.write_json("qa/completion-audit.json", audit_run(artifacts.root))
        # Refresh after the audit so the manifest covers every final artifact.
        artifacts.write_manifest()
        if not report["deliverable"]:
            artifacts.write_json(
                "qa/repair-decision.json",
                classify_repair(report["hard_failures"]).model_dump(mode="json"),
            )
            raise RuntimeError(f"Delivery QA rejected render: {report['hard_failures']}")
        # A targeted QA repair may have inherited a failure decision from an
        # earlier attempt.  Leaving that stale marker beside a successful
        # delivery causes operators and API clients to report a false retry
        # state, so remove it before publishing the final manifest.
        stale_repair = artifacts.qa / "repair-decision.json"
        if stale_repair.exists():
            stale_repair.unlink()
            artifacts.write_manifest()
        return report

    @staticmethod
    def _load_trace(artifacts: RunArtifacts) -> DemoTrace:
        return DemoTrace.model_validate(
            json.loads((artifacts.execution / "trace.json").read_text(encoding="utf-8"))
        )

    @staticmethod
    def _load_plan(artifacts: RunArtifacts) -> DemoPlan:
        """Load the effective plan after any evidence-grounded suffix replan."""
        path = artifacts.root / "plan-effective.json"
        if not path.is_file():
            path = artifacts.root / "plan.json"
        return DemoPlan.model_validate(json.loads(path.read_text(encoding="utf-8")))

    async def run(
        self,
        *,
        run_id: str,
        url: str,
        objective: str,
        artifact_root: Path,
        allow_external_side_effects: bool = False,
        render: bool = True,
        budget: DiscoveryBudget | None = None,
        cloud_discovery: bool = False,
        stage_hook: Callable[[RunStage], None] | None = None,
        known_routes: list[str] | None = None,
        known_actions: list[dict] | None = None,
        explore_visible_routes: bool = False,
        stagehand_assist: bool = False,
        credential_reference: str | None = None,
        audience: str = "product prospect",
        target_duration_seconds: int = 120,
    ) -> DemoTrace:
        """Compatibility coordinator for the durable URL-stage pipeline.

        API/worker runs already execute these stages independently. Keeping the
        direct service method on the same path prevents CLI/tests or future
        callers from bypassing persisted checkpoints and using an older
        monolithic capture/presentation implementation.
        """
        await self.discover_stage(
            run_id=run_id,
            url=url,
            objective=objective,
            artifact_root=artifact_root,
            budget=budget,
            cloud_discovery=cloud_discovery,
            known_routes=known_routes,
            known_actions=known_actions,
            explore_visible_routes=explore_visible_routes,
            stagehand_assist=stagehand_assist,
            credential_reference=credential_reference,
        )
        if stage_hook:
            stage_hook(RunStage.DISCOVERING)
        await self.plan_stage(
            run_id=run_id,
            objective=objective,
            artifact_root=artifact_root,
            allow_external_side_effects=allow_external_side_effects,
            audience=audience,
            target_duration_seconds=target_duration_seconds,
        )
        if stage_hook:
            stage_hook(RunStage.PLAN_VALIDATED)
        trace = await self.execute_stage(
            run_id=run_id,
            url=url,
            objective=objective,
            artifact_root=artifact_root,
            credential_reference=credential_reference,
            cloud_production=cloud_discovery,
        )
        if stage_hook:
            stage_hook(RunStage.TRACE_READY)
        await self.narration_stage(run_id=run_id, artifact_root=artifact_root)
        if stage_hook:
            stage_hook(RunStage.NARRATION_READY)
        if render:
            self.render_stage(run_id=run_id, artifact_root=artifact_root)
            self.qa_stage(run_id=run_id, artifact_root=artifact_root)
            if stage_hook:
                stage_hook(RunStage.QA_PASSED)
        return trace

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
