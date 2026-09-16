$ErrorActionPreference = 'Stop'
# Migrations are a one-off deployment job. Replicas only verify that the
# already-migrated dependencies are healthy, preventing concurrent API
# processes from racing schema upgrades during a rollout.
python -m app.operations.startup --skip-migrations --require-ready
uvicorn app.api.main:app --host 0.0.0.0 --port 8000
