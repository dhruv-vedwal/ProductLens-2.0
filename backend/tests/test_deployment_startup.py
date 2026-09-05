from pathlib import Path

from productlens.config.settings import Settings
from productlens.operations import startup


def test_replica_scripts_do_not_run_migrations():
    """API/worker replicas validate readiness; only the release job migrates."""
    root = Path(__file__).resolve().parents[1]
    for script in (root / "scripts" / "start-api.ps1", root / "scripts" / "start-worker.ps1"):
        contents = script.read_text(encoding="utf-8")
        assert "startup --skip-migrations --require-ready" in contents


def test_one_off_migration_script_runs_before_readiness_check():
    root = Path(__file__).resolve().parents[1]
    contents = (root / "scripts" / "start-migrations.ps1").read_text(encoding="utf-8")
    assert "alembic upgrade head" in contents
    assert contents.index("alembic upgrade head") < contents.index("--require-ready")


def test_deployment_readiness_reports_local_dependencies_without_network(monkeypatch, tmp_path):
    monkeypatch.setenv("PRODUCTLENS_AUTH_SECRET", "x" * 32)
    monkeypatch.setenv("PRODUCTLENS_DATABASE", str(tmp_path / "database.sqlite3"))
    monkeypatch.setenv("PRODUCTLENS_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    settings = Settings.from_environment()

    class Raw:
        def cursor(self):
            class Cursor:
                def execute(self, statement):
                    assert statement == "SELECT 1"

                def fetchone(self):
                    return (1,)

                def close(self):
                    pass

            return Cursor()

    class Connection:
        def cursor(self):
            return Raw().cursor()

        def close(self):
            pass

    class Engine:
        def raw_connection(self): return Connection()
        def dispose(self): pass

    monkeypatch.setattr(startup, "create_engine", lambda *_args, **_kwargs: Engine())
    report = startup.deployment_readiness(settings)
    assert report["database"]["ready"] is True
    assert report["broker"]["ready"] is True
    assert report["object_storage"]["ready"] is True


def test_dramatiq_readiness_requires_explicit_broker_url(monkeypatch, tmp_path):
    monkeypatch.setenv("PRODUCTLENS_AUTH_SECRET", "x" * 32)
    monkeypatch.setenv("PRODUCTLENS_WORKER_MODE", "dramatiq")
    monkeypatch.delenv("PRODUCTLENS_BROKER_URL", raising=False)
    monkeypatch.setenv("PRODUCTLENS_ARTIFACT_ROOT", str(tmp_path))
    settings = Settings.from_environment()
    monkeypatch.setattr(startup, "create_engine", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    assert startup.deployment_readiness(settings)["broker"]["ready"] is False
