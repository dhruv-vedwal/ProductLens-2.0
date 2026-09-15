from productlens.providers.limits import provider_limit


def test_provider_limits_are_bounded_and_configurable(monkeypatch):
    monkeypatch.setenv("PRODUCTLENS_STAGEHAND_CONCURRENCY", "50")
    assert provider_limit("stagehand") == 50
    monkeypatch.setenv("PRODUCTLENS_BROWSERBASE_CONCURRENCY", "1000")
    assert provider_limit("browserbase") == 100
    monkeypatch.setenv("PRODUCTLENS_OPENROUTER_CONCURRENCY", "invalid")
    assert provider_limit("openrouter") == 2
