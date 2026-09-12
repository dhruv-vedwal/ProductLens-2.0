from productlens.contracts.models import ObservedElement
from productlens.planning.forms import infer_form_schema


def test_form_schema_uses_visible_dom_semantics_only():
    schema = infer_form_schema(
        [
            ObservedElement(tag="input", name="Work email", selector="#email", element_type="email", required=True, autocomplete="email"),
            ObservedElement(tag="input", name="Display name *", selector="[name=displayName]", element_type="text"),
            ObservedElement(tag="input", name="Invisible token", selector="#token", element_type="hidden"),
            ObservedElement(tag="select", name="Country", selector="#country", options=["", "India", "United States"]),
        ],
        "https://example.test/form",
    )
    assert [field.name for field in schema.fields] == ["Work email", "Display name *", "Country"]
    assert schema.fields[0].control_type == "email"
    assert schema.fields[0].required
    assert schema.fields[1].required
    assert schema.fields[2].options == ["", "India", "United States"]


def test_form_schema_rejects_anonymous_design_system_controls():
    schema = infer_form_schema(
        [
            ObservedElement(tag="input", name="element-104", selector="input", element_type="text"),
            ObservedElement(tag="input", name="On", selector="#mui-switch", element_type="checkbox"),
            ObservedElement(tag="input", name="Lead name", selector="[name=leadName]", required=True),
        ],
        "https://example.test/form",
    )

    assert [field.name for field in schema.fields] == ["Lead name"]
