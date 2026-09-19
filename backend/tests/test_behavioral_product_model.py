from app.contracts.models import PageKnowledge, ProductContext
from app.discovery.behavioral import build_behavioral_product_model


def test_behavioral_model_persists_custom_controls_and_dependencies():
    url = "https://example.test/bookings"
    context = ProductContext(
        url=url,
        title="Bookings",
        application_type="web_application",
        page_knowledge=[
            PageKnowledge(
                url=url,
                title="Bookings",
                purpose="Create booking",
                fingerprint="f" * 64,
                evidence_refs=["dom:booking"],
            )
        ],
        capabilities=[
            {
                "kind": "form",
                "purpose": "Create booking",
                "source_url": url,
                "form_schema": {
                    "source_url": url,
                    "fields": [
                        {
                            "name": "Branch",
                            "selector": "#branch",
                            "control_type": "combobox",
                            "options": ["Main"],
                        },
                        {
                            "name": "Doctor",
                            "selector": "#doctor",
                            "control_type": "combobox",
                            "options": ["Dr Rao"],
                            "depends_on": ["Branch"],
                        },
                    ],
                },
            }
        ],
    )

    model = build_behavioral_product_model(context)

    assert {item.behavior_class for item in model.controls} == {"combobox"}
    assert len(model.dependencies) == 1
    assert model.dependencies[0].parent_control_id != model.dependencies[0].child_control_id
    assert model.states[0].fingerprint


def test_behavioral_model_truncates_oversized_page_evidence_refs():
    url = "https://example.test/leads"
    context = ProductContext(
        url=url,
        title="Leads",
        application_type="web_application",
        page_knowledge=[
            PageKnowledge(
                url=url,
                title="Leads",
                purpose="Create lead",
                fingerprint="a" * 64,
                evidence_refs=[f"named-control:field-{index}" for index in range(40)],
            )
        ],
        capabilities=[
            {
                "kind": "form",
                "purpose": "Create lead",
                "source_url": url,
                "form_schema": {
                    "source_url": url,
                    "fields": [
                        {
                            "name": "Name",
                            "selector": "#name",
                            "control_type": "text",
                        }
                    ],
                },
            }
        ],
    )
    model = build_behavioral_product_model(context)
    assert len(model.surfaces[0].evidence_refs) <= 32
    assert len(model.states[0].evidence_refs) <= 32
