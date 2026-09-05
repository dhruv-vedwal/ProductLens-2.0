from productlens.contracts.models import OperationKind, Postcondition, SemanticOperation, Target
from productlens.planning.production import ProductionPlanningService


def test_route_transition_discards_stale_source_page_visibility_assertion():
    operation = SemanticOperation(
        kind=OperationKind.CLICK,
        intent="Open Week 1",
        target=Target(name="Open Week 1", selector="a"),
        postconditions=[
            Postcondition(kind="url", expected="https://example.test/weeks/01"),
            Postcondition(kind="visible", expected=True, target=Target(name="Week 1", selector="a")),
        ],
    )
    # The compiler's postcondition rule is independent of provider execution;
    # call the small grounding path with a matching observed anchor.
    from productlens.contracts.models import ObservedElement, ProductContext, WorkflowProposal

    context = ProductContext(
        url="https://example.test/", title="Example", application_type="web_application",
        visible_text="", relevant_routes=["https://example.test/"], navigation=[],
        elements=[ObservedElement(tag="a", name="Open Week 1", selector="a", href="/weeks/01")],
    )
    grounded = ProductionPlanningService._ground(
        WorkflowProposal(narrative_goal="Open Week 1", selected_workflow="Week", steps=[operation], expected_outcomes=["Week opened"], important_elements=[], excluded_areas=[], risk_flags=[]),
        context,
    )[0]
    assert [condition.kind for condition in grounded.postconditions] == ["url"]
