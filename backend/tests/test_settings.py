from productlens.config.settings import Settings


def test_settings_can_import_only_provider_values_from_new_project_environment(
    monkeypatch, tmp_path
):
    source = tmp_path / ".env"
    source.write_text(
        "OPENROUTER_API_KEY=provider-key\nELEVENLABS_API_KEY=tts-key\n"
        "PRODUCTLENS_AUTH_SECRET=0123456789abcdef0123456789abcdef\nIGNORE=value\n"
    )
    monkeypatch.setenv("PRODUCTLENS_ENV_FILE", str(source))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    settings = Settings.from_environment()
    assert settings.openrouter_api_key == "provider-key"
    assert settings.elevenlabs_api_key == "tts-key"


def test_settings_preserves_a_postgres_database_url(monkeypatch, tmp_path):
    source = tmp_path / ".env"
    source.write_text("PRODUCTLENS_AUTH_SECRET=0123456789abcdef0123456789abcdef\n")
    monkeypatch.setenv("PRODUCTLENS_ENV_FILE", str(source))
    monkeypatch.setenv(
        "PRODUCTLENS_DATABASE_URL", "postgresql+psycopg://user:pass@db:5432/productlens"
    )

    settings = Settings.from_environment()

    assert settings.database_url.startswith("postgresql+psycopg://")


def test_settings_reserves_browserbase_finalization_time(monkeypatch, tmp_path):
    source = tmp_path / ".env"
    source.write_text(
        "PRODUCTLENS_AUTH_SECRET=0123456789abcdef0123456789abcdef\n"
        "PRODUCTLENS_CLOUD_CAPTURE_TIMEOUT_SECONDS=12\n"
    )
    monkeypatch.setenv("PRODUCTLENS_ENV_FILE", str(source))

    settings = Settings.from_environment()

    assert settings.cloud_capture_timeout_seconds == 30


def test_settings_requires_explicit_multimodal_review_opt_in(monkeypatch, tmp_path):
    source = tmp_path / ".env"
    source.write_text(
        "PRODUCTLENS_AUTH_SECRET=0123456789abcdef0123456789abcdef\n"
        "OPENROUTER_VISION_MODEL=provider/vision\n"
        "PRODUCTLENS_MULTIMODAL_REVIEW_ENABLED=true\n"
    )
    monkeypatch.setenv("PRODUCTLENS_ENV_FILE", str(source))

    settings = Settings.from_environment()

    assert settings.multimodal_review_enabled is True
    assert settings.openrouter_vision_model == "provider/vision"
