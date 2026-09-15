"""Runtime capability discovery for unfamiliar web interfaces.

The resolver consumes only the current evidence snapshot.  It does not know
product names, routes, or domain workflows; it turns observed affordances into
ranked, auditable capabilities that a planner can verify before dispatch.
"""

from __future__ import annotations

import re
from hashlib import sha256

from productlens.contracts.models import (
    CapabilityEvidence,
    CapabilityResolution,
    ObservedElement,
    ProductContext,
    RuntimeCapability,
    Target,
)

_WORD_RE = re.compile(r"[a-z0-9]{3,}", re.IGNORECASE)


def _words(value: str) -> set[str]:
    return {item.casefold() for item in _WORD_RE.findall(value or "")}


def _target(item: ObservedElement) -> Target:
    return Target(
        name=item.name,
        role=item.role,
        label=item.name,
        text=(item.text or None),
        selector=item.selector if item.selector.startswith(("#", "[")) else None,
        source_url=item.source_url,
    )


def _evidence(
    item: ObservedElement, kind: str, summary: str, confidence: float
) -> CapabilityEvidence:
    source = item.source_url or "unknown"
    # Selectors may contain serialized query strings or long generated
    # attributes. Keep the persisted evidence identifier bounded and stable;
    # the full selector remains in the audited Target when it is safe to use.
    identity = sha256(f"{kind}\n{source}\n{item.selector}\n{item.name}".encode()).hexdigest()[:24]
    return CapabilityEvidence(
        id=f"capability:{kind}:{identity}",
        kind=kind,
        source_url=item.source_url,
        summary=summary[:420],
        confidence=max(0.0, min(1.0, confidence)),
    )


def _required_kinds(intent: str) -> list[str]:
    """Infer generic capability classes from the request, never a product name."""
    words = _words(intent)
    required: list[str] = []
    if words & {"navigate", "open", "visit", "page", "section", "tab"}:
        required.append("navigate")
    if words & {"show", "explain", "inspect", "review", "understand", "read"}:
        required.append("inspect")
    if words & {"form", "fill", "create", "submit", "configure", "enter", "save"}:
        required.append("form")
    if words & {"upload", "attach", "import", "file"}:
        required.append("upload")
    if words & {"keyboard", "shortcut", "hotkey", "press"}:
        required.append("keyboard")
    if words & {"editor", "richtext", "contenteditable", "document"}:
        required.append("rich_text")
    if words & {"shadow", "shadowdom", "shadow-root", "webcomponent", "component"}:
        required.append("shadow_dom")
    if words & {"grid", "table", "rows", "records"}:
        required.append("table")
    if words & {"draw", "whiteboard", "shape", "sketch", "paint"}:
        required.extend(["canvas", "pointer"])
    if words & {"node", "graph", "workflow", "connect", "edge"}:
        required.extend(["graph", "drag_drop"])
    return list(dict.fromkeys(required)) or ["inspect"]


class CapabilityResolver:
    """Resolve semantic intent against one evidence-grounded page snapshot."""

    def resolve(self, intent: str, context: ProductContext) -> CapabilityResolution:
        required = _required_kinds(intent)
        elements = [item for item in context.elements if item.actionable]
        candidates: list[RuntimeCapability] = []

        nav = [
            item
            for item in (context.navigation or elements)
            if item.href or item.navigation_scope in {"primary", "secondary"}
        ]
        if nav:
            candidates.append(
                RuntimeCapability(
                    kind="navigate",
                    purpose="Navigate through an observed visible control",
                    source_url=context.url,
                    targets=[_target(item) for item in nav[:12]],
                    strategies=["dom", "accessibility", "stagehand", "playwright"],
                    evidence=[
                        _evidence(
                            item,
                            "dom",
                            "Visible navigation control with observed semantic metadata",
                            0.9,
                        )
                        for item in nav[:12]
                    ],
                    confidence=0.9,
                    reversible=True,
                    verified=True,
                )
            )

        controls = [
            item
            for item in elements
            if item.tag in {"input", "textarea", "select"}
            or item.role in {"textbox", "combobox", "checkbox", "radio"}
            or item.element_type in {"email", "tel", "date", "text"}
        ]
        if controls:
            candidates.append(
                RuntimeCapability(
                    kind="form",
                    purpose="Enter or inspect values in observed controls",
                    source_url=context.url,
                    targets=[_target(item) for item in controls[:24]],
                    strategies=[
                        "dom",
                        "accessibility",
                        "playwright",
                        "keyboard",
                        "screenshot_verification",
                    ],
                    evidence=[
                        _evidence(item, "dom", "Observed editable or selectable control", 0.86)
                        for item in controls[:24]
                    ],
                    expected_outcomes=["the targeted value or selection is visibly changed"],
                    confidence=0.86,
                    reversible=True,
                )
            )

        dialogs = [
            item
            for item in elements
            if item.role in {"dialog", "alertdialog"} or item.tag == "dialog"
        ]
        if dialogs:
            candidates.append(
                RuntimeCapability(
                    kind="modal",
                    purpose="Inspect an observed dialog or overlay state",
                    source_url=context.url,
                    targets=[_target(item) for item in dialogs[:8]],
                    strategies=[
                        "dom",
                        "accessibility",
                        "stagehand",
                        "playwright",
                        "screenshot_verification",
                    ],
                    evidence=[
                        _evidence(item, "accessibility", "Visible dialog state observed", 0.88)
                        for item in dialogs[:8]
                    ],
                    confidence=0.88,
                )
            )

        tables = [
            item
            for item in elements
            if item.tag in {"table", "tr", "td"} or item.role in {"table", "row", "gridcell"}
        ]
        if tables:
            candidates.append(
                RuntimeCapability(
                    kind="table",
                    purpose="Inspect or verify visible tabular records",
                    source_url=context.url,
                    targets=[_target(item) for item in tables[:24]],
                    strategies=["dom", "accessibility", "screenshot_verification"],
                    evidence=[
                        _evidence(item, "dom", "Observed table or row evidence", 0.82)
                        for item in tables[:24]
                    ],
                    confidence=0.82,
                )
            )

        virtualized = [item for item in elements if item.role in {"grid", "treegrid"}]
        if virtualized:
            candidates.append(
                RuntimeCapability(
                    kind="virtualized_table",
                    purpose="Inspect an observed virtualized data grid",
                    source_url=context.url,
                    targets=[_target(item) for item in virtualized[:8]],
                    strategies=["dom", "accessibility", "screenshot_verification"],
                    evidence=[
                        _evidence(item, "accessibility", "Observed grid/treegrid surface", 0.8)
                        for item in virtualized[:8]
                    ],
                    expected_outcomes=["the requested rows or columns are visibly readable"],
                    confidence=0.8,
                )
            )

        rich_text = [
            item
            for item in elements
            if item.element_type in {"contenteditable", "rich-text", "editor"}
            or (item.role == "textbox" and item.tag not in {"input", "textarea"})
        ]
        if rich_text:
            candidates.append(
                RuntimeCapability(
                    kind="rich_text",
                    purpose="Edit or inspect an observed rich-text surface",
                    source_url=context.url,
                    targets=[_target(item) for item in rich_text[:12]],
                    strategies=[
                        "dom",
                        "accessibility",
                        "keyboard",
                        "stagehand",
                        "screenshot_verification",
                    ],
                    evidence=[
                        _evidence(item, "accessibility", "Observed rich-text/editor surface", 0.76)
                        for item in rich_text[:12]
                    ],
                    expected_outcomes=["the edited text is visibly reflected"],
                    confidence=0.76,
                )
            )

        embedded = [item for item in context.elements if item.tag == "iframe"]
        if embedded:
            candidates.append(
                RuntimeCapability(
                    kind="iframe",
                    purpose="Inspect an observed embedded application frame",
                    source_url=context.url,
                    targets=[_target(item) for item in embedded[:8]],
                    strategies=["dom", "accessibility", "stagehand", "screenshot_verification"],
                    evidence=[
                        _evidence(item, "dom", "Observed embedded iframe surface", 0.72)
                        for item in embedded[:8]
                    ],
                    expected_outcomes=["the embedded content is visible and readable"],
                    confidence=0.72,
                )
            )

        shadow_hosts = [item for item in context.elements if item.shadow_host]
        if shadow_hosts:
            candidates.append(
                RuntimeCapability(
                    kind="shadow_dom",
                    purpose="Inspect an observed open shadow-root surface",
                    source_url=context.url,
                    targets=[_target(item) for item in shadow_hosts[:8]],
                    strategies=["dom", "accessibility", "playwright", "screenshot_verification"],
                    evidence=[
                        _evidence(item, "dom", "Observed host with an open shadow root", 0.78)
                        for item in shadow_hosts[:8]
                    ],
                    expected_outcomes=["the shadow-root content is visible and readable"],
                    confidence=0.78,
                )
            )

        surfaces = [
            item
            for item in elements
            if item.tag in {"canvas", "svg"}
            or item.role in {"application", "toolbar"}
            or item.element_type in {"canvas", "drawing", "editor"}
        ]
        if surfaces:
            surface_targets = [_target(item) for item in surfaces[:8]]
            surface_evidence = [
                _evidence(item, "geometry", "Observed interactive visual surface", 0.72)
                for item in surfaces[:8]
            ]
            candidates.append(
                RuntimeCapability(
                    kind="canvas",
                    purpose="Interact with an observed visual editing surface",
                    source_url=context.url,
                    targets=surface_targets,
                    strategies=[
                        "accessibility",
                        "stagehand",
                        "visual_grounding",
                        "pointer",
                        "screenshot_verification",
                    ],
                    evidence=surface_evidence,
                    expected_outcomes=["the target region visibly changes"],
                    confidence=0.72,
                )
            )
            candidates.append(
                RuntimeCapability(
                    kind="pointer",
                    purpose="Perform a grounded pointer gesture on an observed surface",
                    source_url=context.url,
                    targets=surface_targets,
                    strategies=["visual_grounding", "pointer", "screenshot_verification"],
                    evidence=surface_evidence,
                    expected_outcomes=["a pointer-driven state change is visible"],
                    confidence=0.7,
                )
            )
            graph_terms = {
                "node",
                "nodes",
                "graph",
                "workflow",
                "diagram",
                "edge",
                "connector",
                "connection",
            }
            graph_evidence = [
                item
                for item in elements
                if _words(item.name + " " + (item.text or "")) & graph_terms
            ]
            if graph_evidence:
                graph_targets = [_target(item) for item in [*graph_evidence, *surfaces[:4]][:12]]
                candidates.append(
                    RuntimeCapability(
                        kind="graph",
                        purpose="Manipulate an observed node or connection workspace",
                        source_url=context.url,
                        targets=graph_targets,
                        strategies=[
                            "dom",
                            "accessibility",
                            "stagehand",
                            "pointer",
                            "visual_grounding",
                            "screenshot_verification",
                        ],
                        evidence=[
                            _evidence(
                                item, "geometry", "Observed graph/node/connection affordance", 0.74
                            )
                            for item in graph_evidence[:12]
                        ],
                        expected_outcomes=["the requested node or connection is visibly present"],
                        confidence=0.74,
                    )
                )

        draggable = [
            item
            for item in elements
            if item.draggable
            or item.dropzone
            or item.element_type in {"draggable", "dropzone", "drag"}
            or item.role in {"listitem", "option"}
            and "drag" in _words(item.name + " " + (item.text or ""))
        ]
        if len(draggable) >= 2:
            candidates.append(
                RuntimeCapability(
                    kind="drag_drop",
                    purpose="Move an observed item between grounded regions",
                    source_url=context.url,
                    targets=[_target(item) for item in draggable[:16]],
                    strategies=[
                        "dom",
                        "accessibility",
                        "stagehand",
                        "pointer",
                        "visual_grounding",
                        "screenshot_verification",
                    ],
                    evidence=[
                        _evidence(
                            item, "geometry", "Observed draggable or drop-region evidence", 0.68
                        )
                        for item in draggable[:16]
                    ],
                    expected_outcomes=["the item appears in the destination state"],
                    confidence=0.68,
                )
            )

        uploads = [item for item in elements if item.tag == "input" and item.element_type == "file"]
        if uploads:
            candidates.append(
                RuntimeCapability(
                    kind="upload",
                    purpose="Upload a file through an observed file control",
                    source_url=context.url,
                    targets=[_target(item) for item in uploads[:8]],
                    strategies=["dom", "accessibility", "playwright", "screenshot_verification"],
                    evidence=[
                        _evidence(item, "dom", "Observed file input control", 0.84)
                        for item in uploads[:8]
                    ],
                    expected_outcomes=["the selected file is reflected in the visible state"],
                    confidence=0.84,
                )
            )

        keyboard_targets = [
            item
            for item in elements
            if item.role in {"textbox", "combobox"} or item.tag in {"input", "textarea"}
        ]
        if keyboard_targets:
            candidates.append(
                RuntimeCapability(
                    kind="keyboard",
                    purpose="Use keyboard input on an observed editable control",
                    source_url=context.url,
                    targets=[_target(item) for item in keyboard_targets[:16]],
                    strategies=["accessibility", "playwright", "keyboard"],
                    evidence=[
                        _evidence(item, "accessibility", "Observed keyboard-editable control", 0.78)
                        for item in keyboard_targets[:16]
                    ],
                    expected_outcomes=["the focused control reflects the keyboard action"],
                    confidence=0.78,
                )
            )

        selected: RuntimeCapability | None = None
        best_score = -1.0
        for capability in candidates:
            match = 1.0 if capability.kind in required else 0.0
            score = capability.confidence + match * 0.35
            if score > best_score:
                selected, best_score = capability, score
        unresolved = [
            kind for kind in required if not any(item.kind == kind for item in candidates)
        ]
        confidence = (
            0.0
            if selected is None
            else min(1.0, selected.confidence + (0.15 if not unresolved else 0))
        )
        rationale = [f"required capability classes: {', '.join(required)}"]
        if selected:
            rationale.append(
                f"selected {selected.kind} from {len(selected.evidence)} evidence items"
            )
        if unresolved:
            rationale.append(
                f"unresolved classes require further observation: {', '.join(unresolved)}"
            )
        return CapabilityResolution(
            intent=intent,
            required_capabilities=required,
            candidates=candidates,
            selected_capability_id=selected.id if selected else None,
            selected_strategy=(
                selected.strategies[0] if selected and selected.strategies else None
            ),
            unresolved=unresolved,
            rationale=rationale,
            confidence=confidence,
            source_url=context.url,
        )


def resolve_capabilities(intent: str, context: ProductContext) -> CapabilityResolution:
    """Convenience API used by planners and runtime replanners."""
    return CapabilityResolver().resolve(intent, context)
