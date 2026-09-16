from app.observability.logging import redact_value, safe_url


def test_log_redaction_matches_secret_boundary():
    assert redact_value({"api_key": "private", "nested": {"password": "private"}}) == {
        "api_key": "***",
        "nested": {"password": "***"},
    }
    assert safe_url("https://provider.test/v1?key=private") == "https://provider.test/v1"
