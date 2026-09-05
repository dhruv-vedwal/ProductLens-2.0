from productlens.services.generation import _canonical_url


def test_canonical_url_prevents_redirect_spelling_from_replaying_opening_page():
    assert _canonical_url("http://Portfolio.TEST/") == _canonical_url("https://portfolio.test")
    assert _canonical_url("https://portfolio.test/timeline/?tab=career#ignored") == "https://portfolio.test/timeline?tab=career"
