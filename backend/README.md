# ProductLens 2.0 backend

The API persists generation requests, state transitions, browser evidence, plans, quality reports and artifact locations in a database-backed lineage. SQLite is the zero-configuration local default; PostgreSQL is a supported production dialect. It deliberately stores provider *references* rather than API keys.

## Local startup

```powershell
cd backend
$env:PRODUCTLENS_ARTIFACT_ROOT = "artifacts"
alembic upgrade head
# Terminal 1: consumes the SQLite-backed durable outbox in local development.
python -m app.workers.local
# Terminal 2: serves the API.
uvicorn app.api.main:app --reload --port 8000
```

The database is created at `backend/artifacts/productlens.sqlite3` by default. Override the local path with `PRODUCTLENS_DATABASE`; deploy with a real PostgreSQL URL such as `PRODUCTLENS_DATABASE_URL=postgresql+psycopg://user:password@host:5432/productlens` and run `alembic upgrade head`. SQLite WAL mode is enabled only for the local API/background-job workload. The local worker atomically claims persisted jobs, recovers expired claims, and never uses in-process FastAPI background tasks. In deployment, set `PRODUCTLENS_WORKER_MODE=dramatiq` and `PRODUCTLENS_BROKER_URL=amqp://...`, then run `dramatiq app.workers.tasks`.

## Provider configuration

Set provider values in `backend/.env` (ignored by git) or process environment variables. The new application does not read runtime configuration from the legacy application:

```text
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=<responsive paid model>
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=...
ELEVENLABS_TTS_MODEL=eleven_multilingual_v2
BROWSERBASE_API_KEY=...
STAGEHAND_MODEL=<provider/model>
STAGEHAND_NODE=node
PRODUCTLENS_WORKER_MODE=polling
PRODUCTLENS_DATABASE_URL=postgresql+psycopg://... # production only
```

Never submit provider keys through the API or commit them to the repository. `GET /providers` returns references/status only; `GET /readiness` reports which capabilities are available.

## Browser login credentials

For a supported username/password test environment, send only an opaque reference such as `secret://productlens/demo` in the generation request. Set its values as **process environment variables** on the worker, not in a request, log, artifact, or checked-in file:

```text
PRODUCTLENS_CREDENTIAL_DEMO_USERNAME=...
PRODUCTLENS_CREDENTIAL_DEMO_PASSWORD=...
```

ProductLens authenticates in an unrecorded browser context and passes storage state into the production capture. CAPTCHA, OTP, ambiguous login controls, and unsuccessful login are explicit failures; it never attempts to fake authentication.

## Offline caption-only mode

ElevenLabs is optional. When no TTS provider is configured, ProductLens derives a script directly from verified `DemoTrace` events, writes `presentation/captions.json`, and renders a silent MP4 with timed captions. Missing narration is therefore not a delivery failure in this mode.

The supplied fixtures can be run and rendered without OpenRouter, Browserbase, or ElevenLabs:

```powershell
python -m app.benchmark.run_gate 3 --render
python -m app.evaluation.run_benchmark --attempts 3 --artifact-root artifacts/benchmark
```

### Re-render retained evidence

After a presentation-only change, rebuild an existing verified delivery without
opening a browser or calling any provider:

```powershell
python -m app.video.rerender --artifact-root artifacts/<collection> --run-id <run-id> --verify
```

Use `--verify-only` to run the provider-free delivery checks for an existing MP4
without encoding it again. Rendering writes a candidate first and atomically
replaces `final/demo.mp4` only after a non-empty result is produced.

## Later provider activation

After credentials and account credit are available, set a responsive paid `OPENROUTER_MODEL`, then verify in this order:

1. `GET /readiness` reports OpenRouter, ElevenLabs, Browserbase, and Stagehand capability state without returning credentials.
2. Install the isolated Stagehand bridge once, without running a demo: `cd stagehand; npm install`. Stagehand provides semantic/visual observation and a bounded read-only rehearsal during exploration; Python/Playwright re-grounds every returned affordance and remains the production execution authority.
3. Run a non-destructive URL objective with `render: false`; inspect `/runs/{run_id}/details` for grounded planning and verified trace evidence.
4. Repeat with `render: true`; verify captions remain trace-derived and audio is present only when synthesis succeeds.
5. Use Browserbase discovery only after the local production path is proven for that target application. A cloud run creates an auditable Browserbase session record and closes that session after CDP disconnect, including discovery failures.
6. Stagehand is invoked automatically whenever the provider is configured (including cloud runs). The compatibility request field `stagehand_assist` is not required to unlock intelligence; every observation/rehearsal result is still advisory until re-grounded by ProductLens.

## Funded smoke-test checklist

Provider-backed checks are deliberately not part of ordinary regression runs. When credits are available, perform each check once and retain its run id and artifacts:

1. **OpenRouter:** a non-destructive plan-only URL run (`render: false`) with a narrow objective.
2. **Browserbase:** the same run with `cloud_discovery: true`; confirm the persisted browser-session record is `CLOSED` and the trace remains verified.
3. **Stagehand:** inspect the same scoped Browserbase run; confirm `discovery/stagehand-observation.json` contains observation/rehearsal diagnostics and only re-grounded candidates contribute to the plan.
4. **ElevenLabs:** enable `PRODUCTLENS_CAPTION_ONLY=false` for one fixture run; confirm audio duration, captions, screen timing, and the final MP4 remain synchronized.

## Public acceptance batch

The generic target inventory is kept in `validation/public-targets.json`. After
static checks, run the provider-backed acceptance set without a polling loop:

```powershell
python -m scripts.run_public_acceptance --cloud --render --limit 8
```

The command writes run IDs, artifact paths, and classified outcomes to
`artifacts/acceptance/public-runs.json`; each run retains its normal discovery,
plan, trace, presentation, narration, and QA artifacts. Once the batch exits,
audit the complete set without contacting providers:

```powershell
python -m scripts.audit_public_acceptance
```

## API workflow

1. `GET /readiness` confirms database and provider capability state.
2. `POST /understanding/preview` performs a bounded, non-recording scan from a URL and rough prompt; its evidence is advisory and the generation run re-grounds it.
3. `POST /fixture-runs` starts a supplied test-gate run.
4. `POST /runs` starts a live URL run once OpenRouter is configured.
5. Inspect `GET /runs/{run_id}/details` when needed, then retrieve `GET /runs/{run_id}/artifacts` or `GET /runs/{run_id}/video`; workers execute asynchronously so clients need not poll aggressively.
6. `GET /runs` provides a paginated run-history feed for the frontend.
