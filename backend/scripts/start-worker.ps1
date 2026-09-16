$ErrorActionPreference = 'Stop'
# The deployment migration job runs before workers are supervised. A worker
# must never attempt its own schema upgrade while other replicas are live.
python -m app.operations.startup --skip-migrations --require-ready
if ($env:PRODUCTLENS_WORKER_MODE -eq 'dramatiq') {
  dramatiq app.workers.tasks --processes 1 --threads 1
} else {
  python -m app.workers.local
}
