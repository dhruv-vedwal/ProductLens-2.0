from app.contracts.models import DemoPlan


def _plan(*, target: int, maximum: int | None = None) -> DemoPlan:
    return DemoPlan(
        objective="demo",
        narrative_goal="Explain the observed product",
        audience="product prospect",
        target_duration_seconds=target,
        maximum_duration_seconds=maximum or 600,
        selected_workflow="observed workflow",
        workflow_steps=[
            {
                "id": "step-opening",
                "intent": "Observe the opening page",
                "operation": {"id": "op-opening", "kind": "ReadValue", "intent": "Read the page"},
            }
        ],
        expected_outcomes=["The page is visible"],
        viewport_strategy="native",
        stop_conditions=["The story is complete"],
    )


from app.services.generation_policy import (
    canonical_url,
    normalise_observed_selector,
    production_duration_envelope,
    safe_render_error,
)


def test_normalise_observed_selector_only_closes_truncated_attribute_selector():
    assert normalise_observed_selector('[data-testid="save"') == '[data-testid="save"]'
    assert normalise_observed_selector("#save") == "#save"
    assert normalise_observed_selector(None) is None


def test_safe_render_error_redacts_secrets_and_bounds_diagnostics():
    message = "authorization: Bearer secret-token password=hunter2"

    result = safe_render_error(message)

    assert "secret-token" not in result
    assert "hunter2" not in result
    assert "[REDACTED]" in result
    assert len(safe_render_error("x" * 20, limit=8)) == 8


def test_production_duration_envelope_only_allows_thorough_accounting_window():
    short = _plan(target=120)
    thorough = _plan(target=180, maximum=240)

    assert production_duration_envelope(short) == (600, None)
    maximum, metadata = production_duration_envelope(thorough)
    assert maximum == 246
    assert metadata is not None
    assert metadata["allowance_seconds"] == 6


def test_canonical_url_ignores_incidental_redirect_spelling():
    assert canonical_url("HTTP://Example.com/path/") == "https://example.com/path"
