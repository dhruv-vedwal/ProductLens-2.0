$ErrorActionPreference = 'Stop'

# Run once per deployment, before API/worker replicas start. API and worker
# scripts intentionally skip migrations so concurrent replicas cannot race
# schema upgrades. Alembic uses the same database settings as the application
# and fails closed when the durable database is unavailable.
python -m alembic upgrade head
if ($LASTEXITCODE -ne 0) {
  throw "ProductLens migrations failed with exit code $LASTEXITCODE"
}

# Verify migrated dependencies before handing control to supervisors.
python -m app.operations.startup --require-ready --skip-migrations
if ($LASTEXITCODE -ne 0) {
  throw "ProductLens dependency readiness check failed with exit code $LASTEXITCODE"
}
