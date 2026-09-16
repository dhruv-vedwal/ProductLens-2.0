from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from app.benchmark.fixture_discovery import FixtureTargetedDiscovery
from app.contracts.models import (
    DiscoveryBudget,
    FormField,
    FormSchema,
    ObjectiveSpec,
    ObservedElement,
    PageKnowledge,
    ProductContext,
)
from app.discovery.live import (
    LiveDiscovery,
    _bounded_page_navigation,
    _canonical_route,
    _derive_product_relationships,
    _focused_relationship_evidence_complete,
    _objective_spec,
    _page_knowledge,
    _relationship_child_controls,
    _relationship_page_roles,
    _relationship_supporting_routes,
    _restore_missing_page_landmarks,
    _route_depth,
    _route_objective_score,
    adaptive_exploration_budget,
)
from app.providers.stagehand import StagehandObservation, StagehandPageAnalysis


def test_invite_does_not_crawl_unrelated_sections():
    result = FixtureTargetedDiscovery().select("Invite a new teammate")
    assert result.selected_route == "section-users.html"
    assert "section-billing.html" not in result.visited_routes
    assert "section-settings.html" not in result.visited_routes


def test_export_targets_reports():
    result = FixtureTargetedDiscovery().select("Export last month's report")
    assert result.selected_route == "section-reports.html"


def test_full_walkthrough_objective_requires_thorough_exploration_contract():
    objective = _objective_spec("Create a full walkthrough of every primary tab")
    assert objective.depth == "thorough"
    assert objective.video_type == "full_tour"
    assert objective.minimum_duration_seconds >= 110
    assert "each selected page is explored before transition" in objective.success_criteria


def test_duration_words_do_not_break_full_walkthrough_detection():
    objective = _objective_spec("Create a full 2 to 3 minute walkthrough of each primary section")
    assert objective.demo_type == "full_walkthrough"


def test_route_depth_treats_mounted_spa_base_as_one_primary_section():
    assert _route_depth("https://example.test/todomvc/") == 1
    assert _route_depth("https://example.test/todomvc/tasks") == 2


def test_focused_editorial_scope_is_not_mistaken_for_primary_entity():
    objective = _objective_spec(
        "Create a focused, evidence-grounded feature walkthrough of the observed public product experience."
    )
    assert objective.primary_entity is None
    assert objective.requested_features == []


def test_page_knowledge_retains_sentence_facts_from_visible_text_when_dom_blocks_are_sparse():
    context = ProductContext(
        url="https://example.test/forms",
        title="Forms",
        application_type="demo",
        visible_text="Forms\nComplete the profile fields to review the submitted result.",
    )
    page = _page_knowledge(context)
    assert "Complete the profile fields to review the submitted result." in page.visible_facts


def test_complete_every_primary_section_is_full_walkthrough_intent():
    objective = _objective_spec(
        "Create a complete, evidence-grounded walkthrough of every safe primary section"
    )
    assert objective.demo_type == "full_walkthrough"
    assert objective.depth == "thorough"


def test_punctuated_complete_duration_request_is_a_full_walkthrough():
    objective = _objective_spec("Create a complete, evidence-backed 2 to 3 minute walkthrough")
    assert objective.demo_type == "full_walkthrough"
    assert objective.maximum_duration_seconds == 240


def test_full_tour_completeness_clause_is_not_a_fake_feature_entity():
    objective = _objective_spec(
        "Create a complete, evidence-grounded walkthrough of every safe primary section and meaningful visible content."
    )
    assert objective.demo_type == "full_walkthrough"
    assert objective.primary_entity is None
    assert objective.requested_features == []


def test_objective_spec_classifies_generic_video_intent_without_making_mode_the_feature():
    assert _objective_spec("Create a sales demo of invoice export").video_type == "sales_demo"
    assert (
        _objective_spec("Create a training walkthrough of the task board").video_type == "training"
    )
    assert _objective_spec("Create a changelog video for the new release").video_type == "changelog"
    assert (
        _objective_spec("Give a concise product overview for a product prospect").primary_entity
        is None
    )


def test_objective_spec_does_not_privilege_a_known_application_label():
    objective = _objective_spec("Demonstrate the invoice approval queue")
    assert "invoice" in objective.must_show


def test_relationship_graph_grounds_requested_context_to_distinct_pages():
    objective = _objective_spec(
        "Demonstrate invoice approval in the context of invoice configuration"
    )
    pages = [
        PageKnowledge(
            url="https://example.test/settings/invoice-config",
            title="Invoice Configuration",
            purpose="Invoice Configuration",
            visible_sections=["Rules"],
            visible_facts=["Approval thresholds determine who reviews invoices"],
            fingerprint="config",
        ),
        PageKnowledge(
            url="https://example.test/invoices",
            title="Invoices",
            purpose="Invoice approval",
            visible_sections=["Approval queue"],
            visible_facts=["Review and approve pending invoices"],
            fingerprint="invoices",
        ),
    ]
    relationships = _derive_product_relationships(pages, objective)
    assert len(relationships) == 1
    edge = relationships[0]
    assert edge.source_url.endswith("invoice-config")
    assert edge.target_url.endswith("invoices")
    assert edge.confidence >= 0.9
    assert "today" not in objective.must_show


def test_objective_spec_preserves_a_generic_configuration_relationship():
    objective = _objective_spec("Explain Appointment Configuration -> Appointment workflow")

    assert objective.primary_entity == "appointment"
    assert len(objective.supporting_relationships) == 1
    relationship = objective.supporting_relationships[0]
    assert relationship.source == "appointment configuration"
    assert relationship.target == "appointment workflow"
    assert relationship.relation == "context_for"


def test_objective_spec_extracts_configuration_relationship_from_prose():
    objective = _objective_spec(
        "Explore the relevant Booking Config and settings context to explain how bookings are configured"
    )

    assert objective.primary_entity == "booking"
    assert len(objective.supporting_relationships) == 1
    relationship = objective.supporting_relationships[0]
    assert relationship.source == "booking config"
    assert relationship.target == "bookings"
    assert relationship.relation == "context_for"


def test_objective_spec_does_not_turn_synthetic_value_qualifier_into_context_page():
    objective = _objective_spec(
        "Create a lead flow using realistic synthetic values only when the observed flow requires it"
    )
    assert objective.supporting_relationships == []


def test_focused_relationship_completion_requires_the_actual_context_detail_page():
    objective = _objective_spec(
        "Create a walkthrough of Invoice Management in the context of Invoice Configuration"
    )
    operational = ProductContext(
        url="https://example.test/invoices",
        title="Invoices",
        application_type="web_application",
        visible_text="Invoice Management shows current invoices and their state.",
    )
    settings_directory = ProductContext(
        url="https://example.test/settings",
        title="Settings",
        application_type="web_application",
        visible_text="Settings lists Invoice Configuration alongside unrelated account modules.",
    )
    detail = ProductContext(
        url="https://example.test/settings/invoice-config",
        title="Settings",
        application_type="web_application",
        visible_text="Invoice Configuration defines matching and approval rules for Invoice Management.",
    )

    assert not _focused_relationship_evidence_complete([operational, settings_directory], objective)
    assert _focused_relationship_evidence_complete(
        [operational, settings_directory, detail], objective
    )


def test_relationship_context_is_persisted_as_supporting_evidence_not_a_production_chapter():
    objective = _objective_spec(
        "Create a walkthrough of Invoice Management in the context of Invoice Configuration"
    )
    workspace = PageKnowledge(
        url="https://example.test/invoices",
        title="Invoices",
        purpose="Invoice Management",
        visible_sections=["Invoice queue"],
        visible_facts=["Review and progress customer invoices."],
        fingerprint="invoices",
    )
    settings = PageKnowledge(
        url="https://example.test/settings",
        title="Settings",
        purpose="Settings",
        visible_sections=["Invoice Configuration"],
        fingerprint="settings",
    )
    configuration = PageKnowledge(
        url="https://example.test/settings/invoice-configuration",
        title="Invoice Configuration",
        purpose="Invoice Configuration",
        visible_sections=["Approval rules"],
        visible_facts=["Invoice configuration defines invoice routing."],
        fingerprint="config",
    )

    operational, supporting = _relationship_page_roles(
        [workspace, settings, configuration], objective
    )

    assert [page.url for page in operational] == [workspace.url]
    assert [page.url for page in supporting] == [configuration.url]


def test_explicitly_probed_in_page_configuration_is_valid_supporting_evidence():
    objective = _objective_spec(
        "Create a walkthrough of Invoice Management in the context of Invoice Configuration"
    )
    workspace = PageKnowledge(
        url="https://example.test/invoices",
        title="Invoices",
        purpose="Invoice Management",
        visible_sections=["Invoice queue"],
        fingerprint="invoices",
    )
    opened_panel = PageKnowledge(
        url="https://example.test/settings",
        title="Settings",
        purpose="Invoice Configuration",
        visible_sections=["Approval rules"],
        visible_facts=["Invoice Configuration routes invoices."],
        evidence_refs=["visible_relationship_control:Invoice Configuration"],
        fingerprint="config-panel",
    )

    operational, supporting = _relationship_page_roles([workspace, opened_panel], objective)

    assert [page.url for page in operational] == [workspace.url]
    assert [page.url for page in supporting] == [opened_panel.url]


def test_abbreviated_route_and_short_workspace_label_keep_context_out_of_production_story():
    objective = _objective_spec(
        "Create a walkthrough of Order Management in the context of Order Configuration"
    )
    workspace = PageKnowledge(
        url="https://example.test/orders-v2",
        title="Orders",
        purpose="Orders",
        visible_sections=["Orders"],
        fingerprint="orders",
    )
    settings = PageKnowledge(
        url="https://example.test/settings",
        title="Settings",
        purpose="Settings",
        visible_sections=["Order Configuration"],
        fingerprint="settings",
    )
    configuration = PageKnowledge(
        url="https://example.test/settings/order-config",
        title="Configuration",
        purpose="Configuration",
        visible_sections=["Rules"],
        visible_facts=["Order configuration defines routing."],
        fingerprint="config",
    )

    operational, supporting = _relationship_page_roles(
        [workspace, settings, configuration], objective
    )

    assert [page.url for page in operational] == [workspace.url]
    assert [page.url for page in supporting] == [configuration.url]


def test_objective_spec_keeps_a_walkthrough_subject_ahead_of_a_creation_verb():
    objective = _objective_spec(
        "Create a 2 minute walkthrough of Invoice Management in the context of Invoice Configuration."
    )

    assert objective.primary_entity == "invoice management"
    assert objective.must_show == ["invoice management"]
    assert len(objective.supporting_relationships) == 1
    assert objective.supporting_relationships[0].model_dump() == {
        "source": "invoice configuration",
        "target": "invoice management",
        "relation": "context_for",
        "required": True,
    }


def test_objective_spec_excludes_instruction_and_duration_prose_from_feature_terms():
    objective = _objective_spec(
        "Create a 1 to 2 minute walkthrough of Lead Management in the context of Lead Configuration; establish the relationship during exploration."
    )
    assert objective.requested_features == ["lead", "management", "configuration"]


def test_objective_spec_excludes_production_story_prose_from_feature_terms():
    objective = _objective_spec(
        "In production establish the Lead Management workspace, explain its visible state, and close with the result."
    )

    assert objective.requested_features == ["lead", "management"]


def test_configuration_relationship_prioritises_generic_settings_entry_then_visible_child():
    objective = _objective_spec(
        "Create a walkthrough of Invoice Management in the context of Invoice Configuration."
    )
    root = "https://example.test/dashboard"
    root_navigation = [
        ObservedElement(
            tag="a", name="Analytics", selector="a", href="/analytics", source_url=root
        ),
        ObservedElement(tag="a", name="Settings", selector="a", href="/settings", source_url=root),
        ObservedElement(tag="a", name="Invoices", selector="a", href="/invoices", source_url=root),
    ]

    assert _route_objective_score(
        "https://example.test/settings", root_navigation, objective
    ) > _route_objective_score("https://example.test/analytics", root_navigation, objective)
    assert _route_objective_score(
        "https://example.test/settings", root_navigation, objective
    ) > _route_objective_score("https://example.test/invoice-templates", root_navigation, objective)
    settings = ProductContext(
        url="https://example.test/settings",
        title="Settings",
        application_type="web application",
        navigation=[
            ObservedElement(
                tag="a",
                name="Invoice Configuration",
                selector="a",
                href="/settings/invoices",
                source_url="https://example.test/settings",
            ),
            ObservedElement(
                tag="a",
                name="Profile",
                selector="a",
                href="/settings/profile",
                source_url="https://example.test/settings",
            ),
        ],
        confidence=1,
    )

    assert _relationship_supporting_routes(settings, objective) == [
        "https://example.test/settings/invoices"
    ]
    settings_button = settings.model_copy(
        update={
            "elements": [
                ObservedElement(
                    tag="button",
                    name="Invoice Configuration",
                    selector="button",
                    source_url=settings.url,
                ),
                ObservedElement(
                    tag="button", name="Profile", selector="button", source_url=settings.url
                ),
            ]
        }
    )
    assert [item.name for item in _relationship_child_controls(settings_button, objective)] == [
        "Invoice Configuration"
    ]


def test_feature_surface_precedes_supporting_settings_and_unobserved_sibling_routes():
    objective = _objective_spec(
        "Create a walkthrough of Lead Management in the context of Lead Configuration."
    )
    root = "https://example.test/dashboard"
    navigation = [
        ObservedElement(tag="a", name="Settings", selector="a", href="/settings", source_url=root),
        ObservedElement(tag="a", name="Leads V3", selector="a", href="/leads-v3", source_url=root),
        ObservedElement(
            tag="a", name="Lead Templates", selector="a", href="/lead-templates", source_url=root
        ),
    ]

    lead_score = _route_objective_score("https://example.test/leads-v3", navigation, objective)
    settings_score = _route_objective_score("https://example.test/settings", navigation, objective)
    sibling_score = _route_objective_score(
        "https://example.test/lead-templates", navigation, objective
    )

    assert lead_score > settings_score > sibling_score


def test_page_knowledge_retains_non_secret_screenshot_evidence_reference():
    context = ProductContext(
        url="https://example.test/",
        title="Example",
        application_type="dashboard",
        elements=[ObservedElement(tag="h1", name="Overview", selector="h1")],
        evidence=["current DOM", "screenshot:discovery/screenshots/abc.png"],
        confidence=1,
    )

    page = _page_knowledge(context)

    assert page.screenshot_evidence == "discovery/screenshots/abc.png"


def test_page_knowledge_exposes_typed_dom_accessibility_and_geometry_evidence():
    context = ProductContext(
        url="https://example.test/editor",
        title="Editor",
        application_type="web_application",
        visible_text="Editor",
        elements=[
            ObservedElement(tag="canvas", name="Drawing surface", selector="canvas"),
            ObservedElement(tag="div", name="Shadow host", selector="#host", shadow_host=True),
        ],
    )
    page = _page_knowledge(context)
    assert page.dom_evidence_refs and page.accessibility_evidence_refs
    assert any(item.startswith("geometry:") for item in page.geometry_evidence_refs)


@pytest.mark.asyncio
async def test_stagehand_semantic_analysis_is_re_grounded_before_it_enriches_page_knowledge():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content("""
            <main><h1>Workspace overview</h1><section>Active invoices</section>
            <button id='new'>Create invoice</button></main>
        """)
        current = ProductContext(
            url=page.url,
            title="Workspace",
            application_type="web_application",
            visible_text="Workspace overview Active invoices Create invoice",
            page_knowledge=[
                PageKnowledge(
                    url=page.url,
                    title="Workspace",
                    purpose="Workspace overview",
                    visible_sections=["Workspace overview"],
                    actionable_controls=["Create invoice"],
                    fingerprint="fixture",
                )
            ],
        )
        observation = StagehandObservation(
            candidates=[],
            metrics={},
            observed_url=page.url,
            analysis=StagehandPageAnalysis(
                visible_sections=["Active invoices", "invented KPI dashboard"],
                meaningful_controls=["Create invoice", "Delete all invoices"],
                safe_next_actions=["Inspect invoice details"],
            ),
        )

        enriched = await LiveDiscovery().enrich_with_stagehand(page, current, observation)

        knowledge = enriched.page_knowledge[0]
        assert "Active invoices" in knowledge.visible_sections
        assert "invented KPI dashboard" not in knowledge.visible_sections
        assert "Create invoice" in knowledge.actionable_controls
        assert "Delete all invoices" not in knowledge.actionable_controls
        assert any(
            item.startswith("stagehand-grounded-section:Active invoices")
            for item in knowledge.evidence_refs
        )
        await browser.close()


@pytest.mark.asyncio
async def test_scoped_form_extraction_preserves_labelled_component_controls_and_local_requiredness():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content("""
            <form>
              <div role="group"><label>Full name <input placeholder="Enter name"></label><span>Required field</span></div>
              <div role="group"><label>Work phone <input type="tel" placeholder="Enter phone"></label><span>Required field</span></div>
              <div role="group"><label>Notes <div role="textbox" contenteditable="true"></div></label></div>
            </form>
        """)
        schema = await LiveDiscovery()._form_schema_from_scope(page.locator("form"), page.url)
        await context.close()
        await browser.close()

    by_name = {field.name: field for field in schema.fields}
    assert by_name["Full name"].required
    assert by_name["Work phone"].required
    assert "Notes" in by_name
    assert not schema.unresolved_required_fields


@pytest.mark.asyncio
async def test_scoped_form_extraction_reads_component_label_and_described_validation():
    """Requiredness must survive generated design-system input markup.

    The browser only exposes the editable combobox input, while the visible
    label and error text live in an enclosing form-control.  Treating the
    input's immediate parent as the field boundary would miss that requirement
    and authorise an incomplete submit.
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content("""
            <form>
              <div class="MuiFormControl-root">
                <label for="source">Source *</label>
                <div class="MuiInputBase-root">
                  <input id="source" role="combobox" aria-describedby="source-error" />
                </div>
                <p id="source-error">Choose where this record originated</p>
              </div>
              <div class="MuiFormControl-root">
                <label for="branch">Branch (optional)</label>
                <input id="branch" role="combobox" />
              </div>
            </form>
        """)
        schema = await LiveDiscovery()._form_schema_from_scope(page.locator("form"), page.url)
        await context.close()
        await browser.close()

    by_name = {field.name: field for field in schema.fields}
    assert by_name["Source"].required
    assert not by_name["Branch (optional)"].required
    assert not schema.unresolved_required_fields


@pytest.mark.asyncio
async def test_form_extraction_prefers_readable_selected_label_over_short_value_code():
    """Locale/value codes must not become opaque production targets."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content("""
            <form>
              <label>Language
                <select aria-label="en" name="language">
                  <option value="en" selected>English</option>
                  <option value="hi">Hindi</option>
                </select>
              </label>
            </form>
        """)
        schema = await LiveDiscovery()._form_schema_from_scope(page.locator("form"), page.url)
        await context.close()
        await browser.close()

    assert schema.fields
    assert schema.fields[0].name == "English"


@pytest.mark.asyncio
async def test_choice_probe_uses_unique_nearest_visible_field_context_when_accessible_label_is_absent():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content("""
            <form>
              <div><span>Source</span><div><input role="combobox" onclick="document.querySelector('[role=listbox]').hidden=false"></div></div>
              <div><span>Branch</span><div><input role="combobox"></div></div>
              <ul role="listbox" hidden><li role="option">Referral</li><li role="option">Website</li></ul>
            </form>
        """)
        schema = FormSchema(
            source_url=page.url,
            fields=[FormField(name="Source", selector="label:Source", control_type="combobox")],
        )
        enriched = await LiveDiscovery()._enrich_choice_options(page, schema)
        await context.close()
        await browser.close()

    assert enriched.fields[0].options == ["Referral", "Website"]


def test_discovery_canonical_route_does_not_reinspect_transport_or_trailing_slash_variants():
    assert _canonical_route("http://Example.Test/") == _canonical_route("https://example.test")
    assert (
        _canonical_route("https://example.test/overview/?ignored=1#section")
        == "https://example.test/overview"
    )


def test_discovery_canonical_route_collapses_default_documents():
    assert _canonical_route("https://example.test/") == _canonical_route(
        "https://example.test/index.html"
    )
    assert _canonical_route("https://example.test/overview") == _canonical_route(
        "https://example.test/overview/index.htm"
    )


def test_full_walkthrough_adapts_budget_to_visible_primary_sections():
    budget = DiscoveryBudget(max_pages=6, max_actions=24, max_model_calls=3, max_time_seconds=60)
    objective = ObjectiveSpec(raw="complete walkthrough", demo_type="full_walkthrough")
    expanded = adaptive_exploration_budget(budget, objective, primary_route_count=8)
    assert expanded.max_pages == 9
    assert expanded.max_actions >= 36
    assert expanded.max_time_seconds >= 180


def test_full_walkthrough_gets_page_local_time_even_when_page_count_fits_default():
    budget = DiscoveryBudget(max_pages=6, max_actions=24, max_model_calls=3, max_time_seconds=60)
    objective = ObjectiveSpec(raw="complete walkthrough", demo_type="full_walkthrough")
    expanded = adaptive_exploration_budget(budget, objective, primary_route_count=3)
    assert expanded.max_pages == 6
    assert expanded.max_time_seconds >= 180


def test_narrow_objective_never_expands_discovery_budget():
    budget = DiscoveryBudget(max_pages=6, max_actions=24, max_model_calls=3, max_time_seconds=60)
    objective = ObjectiveSpec(raw="show one feature", demo_type="workflow_demo")
    assert adaptive_exploration_budget(budget, objective, primary_route_count=8) == budget


def test_interactive_workflow_gets_page_local_probe_budget_without_full_tour():
    budget = DiscoveryBudget(max_pages=6, max_actions=24, max_model_calls=3, max_time_seconds=60)
    objective = ObjectiveSpec(
        raw="create a record and verify the resulting detail view",
        demo_type="workflow_demo",
    )
    expanded = adaptive_exploration_budget(budget, objective, primary_route_count=4)
    assert expanded.max_pages == 6
    assert expanded.max_actions >= 48
    assert expanded.max_time_seconds >= 240


def test_interactive_budget_uses_declared_must_show_terms():
    budget = DiscoveryBudget(max_pages=3, max_actions=12, max_model_calls=3, max_time_seconds=30)
    objective = ObjectiveSpec(
        raw="walk through this feature",
        demo_type="feature_walkthrough",
        must_show=["draw and connect the nodes"],
    )
    expanded = adaptive_exploration_budget(budget, objective, primary_route_count=1)
    assert expanded.max_actions >= 48
    assert expanded.max_time_seconds >= 240


def test_discovery_does_not_replay_the_opening_url_after_collecting_evidence():
    from tests.source_utils import package_source

    source = package_source("app/discovery/live")

    assert 'page.goto(entry_url, wait_until="domcontentloaded")' not in source


def test_discovery_restores_role_grounded_page_landmarks_when_dense_dom_truncates_them():
    page = PageKnowledge(
        url="https://example.test/reports",
        title="Reports",
        purpose="Reports",
        scroll_landmarks=["Monthly report", "Export history"],
        fingerprint="reports",
    )
    restored = _restore_missing_page_landmarks([], [page])

    assert [item.name for item in restored] == ["Monthly report", "Export history"]
    assert all(item.role == "heading" and item.source_url == page.url for item in restored)
    assert all(item.selector.startswith("observed-heading:") for item in restored)


def test_navigation_evidence_keeps_controls_from_each_inspected_page():
    """A dense page must not evict a later page's visible transition control."""
    opening = "https://example.test/"
    later = "https://example.test/section"
    elements = [
        ObservedElement(
            tag="a",
            name=f"Opening link {index}",
            selector=f"a-opening-{index}",
            href=f"/opening-{index}",
            source_url=opening,
        )
        for index in range(40)
    ]
    elements.extend(
        [
            ObservedElement(
                tag="a",
                name="Next section",
                selector="a-next",
                href="/next",
                source_url=later,
            ),
            ObservedElement(
                tag="a",
                name="Section home",
                selector="a-home",
                href="/",
                source_url=later,
            ),
        ]
    )

    retained = _bounded_page_navigation(elements, per_page=3, maximum=20)

    assert {item.source_url for item in retained} == {opening, later}
    assert "Next section" in {item.name for item in retained}
