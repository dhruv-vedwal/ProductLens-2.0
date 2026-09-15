from productlens.contracts.models import ObservedElement, ProductContext
from productlens.planning.capability_resolution import resolve_capabilities
from productlens.planning.evidence_graph import EvidenceEdge, EvidenceGraph


def _context(elements: list[ObservedElement]) -> ProductContext:
    return ProductContext(
        url="https://app.example.test/workspace",
        title="Workspace",
        application_type="web_application",
        elements=elements,
        navigation=[item for item in elements if item.href],
    )


def test_resolver_discovers_form_and_navigation_without_domain_terms():
    context = _context(
        [
            ObservedElement(
                tag="a",
                name="Workspace",
                selector="#workspace",
                href="/workspace",
                source_url="https://app.example.test/workspace",
            ),
            ObservedElement(
                tag="input",
                name="Display name",
                selector="#display-name",
                element_type="text",
                source_url="https://app.example.test/workspace",
            ),
            ObservedElement(
                tag="button",
                name="Save",
                selector="[data-action='save']",
                source_url="https://app.example.test/workspace",
            ),
        ]
    )
    result = resolve_capabilities("configure the form and save the value", context)
    assert result.selected_capability_id
    assert "form" in result.required_capabilities
    assert any(item.kind == "form" for item in result.candidates)
    assert all("smartsevak" not in item.purpose.casefold() for item in result.candidates)


def test_resolver_prefers_visual_capability_for_draw_request():
    context = _context(
        [
            ObservedElement(
                tag="canvas",
                name="Editor surface",
                selector="[data-surface='main']",
                element_type="canvas",
                source_url="https://app.example.test/editor",
            ),
            ObservedElement(
                tag="button",
                name="Tool",
                selector="[data-tool='draw']",
                role="toolbar",
                source_url="https://app.example.test/editor",
            ),
        ]
    )
    result = resolve_capabilities("draw a shape on the editor", context)
    assert "canvas" in result.required_capabilities
    assert result.selected_capability_id
    selected = next(item for item in result.candidates if item.id == result.selected_capability_id)
    assert selected.kind in {"canvas", "pointer"}
    assert "screenshot_verification" in selected.strategies


def test_unresolved_capability_is_explicit_instead_of_guessing():
    context = _context(
        [
            ObservedElement(
                tag="h1",
                name="Overview",
                selector="h1",
                actionable=False,
                source_url="https://app.example.test/",
            ),
        ]
    )
    result = resolve_capabilities("connect nodes in an editor", context)
    assert {"graph", "drag_drop"}.issubset(result.unresolved)
    assert result.selected_capability_id is None
    assert result.confidence == 0


def test_resolver_exposes_graph_capability_from_observed_graph_evidence():
    context = _context(
        [
            ObservedElement(
                tag="canvas",
                name="Workflow canvas",
                selector="#canvas",
                element_type="canvas",
                source_url="https://app.example.test/workflow",
            ),
            ObservedElement(
                tag="button",
                name="Add node",
                selector="#add-node",
                source_url="https://app.example.test/workflow",
            ),
        ]
    )
    result = resolve_capabilities("connect nodes in the workflow", context)
    assert "graph" in result.required_capabilities
    assert any(item.kind == "graph" for item in result.candidates)
    assert "graph" not in result.unresolved


def test_evidence_graph_search_uses_only_verified_observed_transitions():
    graph = EvidenceGraph()
    for state in ("home", "editor", "result"):
        graph.add_state(state)
    graph.add_transition(EvidenceEdge("home", "editor", "open", cost=1))
    graph.add_transition(EvidenceEdge("editor", "result", "save", cost=1))
    graph.add_transition(EvidenceEdge("home", "result", "unverified", verified=False))
    path = graph.shortest_verified_path("home", "result")
    assert [edge.action_id for edge in path] == ["open", "save"]


def test_resolver_exposes_editor_and_virtualized_grid_capabilities():
    context = _context(
        [
            ObservedElement(
                tag="div",
                role="textbox",
                name="Document",
                selector="#editor",
                source_url="https://app.example.test/editor",
            ),
            ObservedElement(
                tag="div",
                role="grid",
                name="Results",
                selector="#grid",
                source_url="https://app.example.test/editor",
            ),
        ]
    )
    editor = resolve_capabilities("edit the document", context)
    assert any(item.kind == "rich_text" for item in editor.candidates)
    grid = resolve_capabilities("inspect the grid", context)
    assert any(item.kind == "virtualized_table" for item in grid.candidates)
