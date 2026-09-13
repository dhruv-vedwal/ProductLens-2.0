from productlens.contracts.models import CandidateDemoFlow, ObjectiveSpec, PageKnowledge, ProductContext
from productlens.services.generation import _relevance_graph
from productlens.planning.brief import build_demo_brief


def test_demo_brief_preserves_selected_story_pages_and_supporting_context_without_execution_data():
    workspace = "https://example.test/invoices"
    configuration = "https://example.test/settings/invoice-configuration"
    context = ProductContext(
        url=workspace, title="Invoices", application_type="dashboard",
        objective=ObjectiveSpec(
            raw="Show Invoice Management in the context of Invoice Configuration",
            primary_entity="invoice management",
            supporting_relationships=[{
                "source": "invoice configuration", "target": "invoice management",
            }],
            exclusions=["billing"],
        ),
        page_knowledge=[
            PageKnowledge(url=workspace, title="Invoices", purpose="Invoice Management",
                          visible_sections=["Invoice queue"], evidence_refs=["section:Invoice queue"], fingerprint="invoices"),
            PageKnowledge(url=configuration, title="Invoice Configuration", purpose="Routing rules",
                          visible_sections=["Approval rules"], evidence_refs=["section:Approval rules"], fingerprint="config"),
        ],
        candidate_demo_flows=[CandidateDemoFlow(
            name="Invoice flow", page_urls=[workspace], supporting_page_urls=[configuration],
            expected_outcomes=["Invoice status is understandable"], risks=["read-only configuration"], score=0.9,
        )],
    )

    brief = build_demo_brief(
        context, objective=context.objective.raw, audience="operations leaders", duration_seconds=120,
    )

    assert brief.selected_flow == "Invoice flow"
    assert brief.included_pages == [workspace]
    assert brief.supporting_pages == [configuration]
    assert brief.page_story_roles[workspace] == "operational story page"
    assert brief.page_story_roles[configuration].startswith("supporting context")
    assert "section:Invoice queue" in brief.evidence_refs
    assert "billing" in brief.exclusions
    assert not hasattr(brief, "selectors")


def test_relevance_graph_links_observed_pages_features_and_controls():
    context = ProductContext(
        url="https://example.test/", title="Example", application_type="dashboard",
        page_knowledge=[PageKnowledge(
            url="https://example.test/", title="Example", purpose="Overview",
            visible_sections=["Summary"], actionable_controls=["Open reports"],
            evidence_refs=["section:Summary"], fingerprint="page-1",
        )],
        feature_knowledge=[{
            "name": "Reports", "purpose": "View reports", "entry_urls": ["https://example.test/"],
            "evidence": ["section:Summary"], "relevance_score": 0.8,
        }],
    )
    graph = _relevance_graph(context)
    assert graph["schema_version"] == 1
    assert {node["kind"] for node in graph["nodes"]} == {"page", "feature", "control"}
    assert {edge["relation"] for edge in graph["edges"]} == {"exposes", "contains"}


def test_relevance_graph_preserves_observed_relationship_edges():
    context = ProductContext(
        url="https://example.test/", title="Example", application_type="dashboard",
        relationships=[{
            "source": "configuration", "target": "approval queue",
            "relation": "configures", "evidence_refs": ["page:https://example.test/settings"],
            "confidence": 0.9,
        }],
    )
    graph = _relevance_graph(context)
    assert any(edge["relation"] == "configures" for edge in graph["edges"])
