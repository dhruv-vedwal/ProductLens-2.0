from productlens.planning.candidates import _canonical_url as candidate_canonical_url
from productlens.planning.production import _canonical_url as planning_canonical_url
from productlens.quality.consistency import _canonical_url as consistency_canonical_url
from productlens.services.generation import _canonical_url, _safe_render_error
from productlens.services.preflight import canonical_url as preflight_canonical_url
from productlens.urls import canonical_product_url


def test_canonical_url_prevents_redirect_spelling_from_replaying_opening_page():
    assert _canonical_url("http://Portfolio.TEST/") == _canonical_url("https://portfolio.test")
    assert (
        _canonical_url("https://portfolio.test/timeline/?tab=career#ignored")
        == "https://portfolio.test/timeline?tab=career"
    )


def test_all_runtime_url_identities_treat_default_documents_as_the_opening_route():
    variants = (
        _canonical_url,
        planning_canonical_url,
        consistency_canonical_url,
    )
    for canonical in variants:
        assert canonical("https://example.test/") == canonical("https://example.test/index.html")
        assert canonical("https://example.test/section") == canonical(
            "https://example.test/section/index.htm"
        )
    assert candidate_canonical_url(
        "https://example.test/", "https://example.test/"
    ) == candidate_canonical_url("https://example.test/", "https://example.test/index.html")
    assert candidate_canonical_url(
        "https://example.test/", "https://example.test/section"
    ) == candidate_canonical_url("https://example.test/", "https://example.test/section/index.htm")


def test_all_runtime_url_identities_normalize_transport_and_query_order():
    variants = (
        _canonical_url,
        planning_canonical_url,
        consistency_canonical_url,
    )
    expected = "https://example.test/app?a=1&b=2"
    for canonical in variants:
        assert canonical("http://EXAMPLE.TEST/app/?b=2&a=1#state") == expected
    assert candidate_canonical_url("http://example.test/", "/app?b=2&a=1") == expected


def test_render_diagnostics_redact_credential_like_values():
    diagnostic = _safe_render_error("authorization: Bearer abc123 api-key=secret-value code=failed")
    assert "abc123" not in diagnostic
    assert "secret-value" not in diagnostic
    assert "[REDACTED]" in diagnostic


def test_shared_url_identity_keeps_preflight_and_knowledge_cache_on_one_route():
    first = "http://Example.TEST/app/index.html?b=2&a=1#section"
    second = "https://example.test/app/?a=1&b=2"
    assert canonical_product_url(first) == "https://example.test/app?a=1&b=2"
    assert preflight_canonical_url(first) == preflight_canonical_url(second)
