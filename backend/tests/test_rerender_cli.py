from app.video import rerender


def test_rerender_cli_requires_retained_artifact_identity(monkeypatch):
    monkeypatch.setattr("sys.argv", ["rerender"])
    try:
        rerender.main()
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("rerender CLI accepted a request without retained artifact identity")


def test_rerender_cli_exposes_provider_free_verification_flag(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["rerender", "--help"])
    try:
        rerender.main()
    except SystemExit as error:
        assert error.code == 0
    help_text = capsys.readouterr().out
    assert "--verify" in help_text
    assert "--verify-only" in help_text
