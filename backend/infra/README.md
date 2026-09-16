# Deployment-like local dependencies

Start the disposable dependency stack with:

```powershell
docker compose -f infra/docker-compose.yml up -d
```

Use these local endpoints when running deployment-readiness checks:

- PostgreSQL: `postgresql://productlens:productlens_dev_only@localhost:55432/productlens`
- RabbitMQ: `amqp://guest:guest@localhost:5672/`
- S3-compatible API: `http://localhost:59000` (bucket creation is required before publishing)

The credentials are development-only and must not be used in production.

## Deployment-like validation

Set `PRODUCTLENS_DATABASE_URL`, `PRODUCTLENS_BROKER_URL`, and the S3-compatible
storage variables, then run the migration job exactly once before scaling API or
worker processes:

```powershell
python -m app.operations.startup --require-ready
```

It runs `alembic upgrade head`, verifies `SELECT 1`, declares a RabbitMQ queue,
and checks object-store bucket access without writing user artifacts. Start
supervised replicas afterwards with `scripts/start-api.ps1` and
`scripts/start-worker.ps1`. The worker has one process/thread by default so
browser and rendering capacity is controlled by the process supervisor; scale
replicas only after provider quotas and DB connection limits are set.
