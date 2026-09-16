import pytest

from app.contracts.models import FormField
from app.planning.form_dependencies import FormDependencyError, order_form_fields


def test_form_fields_follow_observed_dependencies_stably():
    fields = [
        FormField(name="Branch", selector="#branch", control_type="select", depends_on=["Clinic"]),
        FormField(name="Clinic", selector="#clinic", control_type="select"),
        FormField(name="Notes", selector="#notes", control_type="textarea"),
    ]
    assert [field.name for field in order_form_fields(fields)] == ["Clinic", "Branch", "Notes"]


def test_unknown_or_cyclic_dependencies_fail_closed():
    with pytest.raises(FormDependencyError, match="unobserved"):
        order_form_fields(
            [
                FormField(
                    name="Branch", selector="#branch", control_type="select", depends_on=["Missing"]
                )
            ]
        )
    with pytest.raises(FormDependencyError, match="cycle"):
        order_form_fields(
            [
                FormField(name="A", selector="#a", control_type="text", depends_on=["B"]),
                FormField(name="B", selector="#b", control_type="text", depends_on=["A"]),
            ]
        )
