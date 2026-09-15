# ProductLens 2.0

## Studio accounts

The web studio is multi-user by default. Create an account at `/signup`; the API
issues an expiring, signed bearer session and every project, run, artifact, and
video request is scoped to that account. `PRODUCTLENS_AUTH_SECRET` must be set
to a private, high-entropy value in `backend/.env` for each deployment.

Public operational checks remain available at `/health` and `/readiness`; provider
configuration and all user content require authentication.

Greenfield, reliability-first demo-generation engine.  The legacy `ProductLens AI/`
application is reference-only; this service owns semantic execution, verification,
DemoTrace creation, and presentation planning.

## Development gates

```powershell
cd backend
python -m pip install -e ".[dev]"
python -m playwright install chromium
alembic upgrade head
pytest
python -m productlens.benchmark.run_gate --gate 1
```

The fixture directory remains outside the application package on purpose. Its path is
discovered relative to this repository, and the fixtures themselves are never changed.

Run gates in order. A later gate is not evidence for an earlier one. For a
live run, follow `GET /runs/{run_id}/events` as an SSE stream instead of
polling `/runs/{run_id}/stages`.

## Boundaries

- Playwright performs deterministic execution and captures evidence.
- Browserbase and Stagehand are required for cloud discovery/production and
  remain optional only for local fixture runs; neither provider owns workflow
  truth.
- Set `PRODUCTLENS_WORKER_CONCURRENCY` for durable local-worker fan-out and
  `PRODUCTLENS_BROWSERBASE_CONCURRENCY`, `PRODUCTLENS_STAGEHAND_CONCURRENCY`,
  and `PRODUCTLENS_OPENROUTER_CONCURRENCY` to apply provider backpressure.
- LLM and TTS providers are isolated behind provider interfaces.
- A browser recording is source evidence; `PresentationPlan` is the only contract a
  renderer consumes.
- Secrets are accepted only by provider adapters and are redacted from logs, traces,
  artifacts, and public schemas.
- Runtime configuration, database, workers, frontend, and renderer assets are owned
  by this folder. The legacy application is visual/reference material only.
