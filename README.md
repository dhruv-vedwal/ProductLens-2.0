# ProductLens 2.0

ProductLens 2.0 is a reliability-first product-demo generation system. It accepts a URL and a natural-language demo objective, understands the visible product, discovers a safe workflow, captures browser evidence, writes an evidence-grounded story, and renders a caption-led demo video. ElevenLabs audio is optional.

For an implementation-focused map of the current code, coupling hotspots, and
validated refactor boundaries, see [ARCHITECTURE_ASSESSMENT.md](ARCHITECTURE_ASSESSMENT.md)
and [REFACTOR_REVIEW.md](REFACTOR_REVIEW.md). This README explains the complete
runtime architecture; those documents distinguish implemented behavior from
future decomposition work.

This document describes the implementation that exists in this repository. It intentionally distinguishes implemented safeguards from capabilities that still require live validation. A rendered MP4 is not considered proof that an interaction succeeded.

## Current status

The repository contains the complete contract, persistence, discovery, planning, execution, presentation, rendering, provider, and QA layers. The following are implemented and covered by automated tests:

- versioned objective, product, page, workflow, scene, trace, narration, and quality contracts;
- database-backed users, projects, runs, jobs, artifacts, provider attempts, knowledge versions, and retry lineage;
- bounded URL discovery and persistent page knowledge;
- Stagehand/Browserbase provider boundaries with secret redaction;
- semantic Playwright execution with visible targeting and before/after evidence;
- gradual scrolling and native-speed action footage;
- run-local recording/trace provenance checks;
- caption-led silent rendering and optional TTS timing ownership;
- layered editorial, execution, visual, synchronization, and delivery QA;
- local fixture and integration regression suites.

The interaction layer is intentionally fail-closed, but it is not a universal guarantee for every arbitrary application. Complex mutations such as canvas editing, graph construction, opaque custom widgets, remote desktops, CAPTCHA, and provider-blocked sessions need successful behavioral rehearsal and live validation before they can be claimed as supported. If a behavior is not observed and independently verified, ProductLens must not fabricate it.

## Product contract

The system accepts:

1. a public URL or an authenticated URL;
2. a natural-language objective such as “show how to create a booking” or “give a recruiter tour of this portfolio”;
3. optional audience, demo type, duration, exclusions, and safe-action authorization;
4. an optional opaque credential reference resolved only by the worker.

It produces:

- a persisted understanding/discovery report;
- a candidate workflow and validated scene plan;
- a production `DemoTrace` containing browser evidence;
- an approved scene-linked narration/caption script;
- a rendered MP4 and delivery report when every required QA layer passes;
- classified failure and repair artifacts when a stage cannot proceed.

The product promise is truthful delivery: a verified action demo when the requested workflow is proven, a verified read-only walkthrough when only inspection is possible, or a clear recoverable/provider-blocked result. ProductLens never presents a guessed action as completed.

## Architecture

```text
Request
  │
  ▼
Objective Interpreter
  │  ObjectiveSpec, audience, duration, safety policy
  ▼
Exploration Browser (isolated)
  │  DOM + accessibility + screenshots + browser state + Stagehand
  ▼
Product/Page/Feature Knowledge
  │  canonical URLs, facts, controls, relationships, freshness
  ▼
Candidate Workflow Discovery and Scoring
  │  semantic steps, evidence, risks, expected outcomes
  ▼
Validated DemoPlan + ScenePlan
  │  page contracts, dwell, camera, cursor, completion criteria
  ▼
Clean Production Browser
  │  semantic execution, re-grounding, state verification
  ▼
DemoTrace
  │  timestamps, geometry, screenshots, state transitions, errors
  ▼
Video Journey Director
  │  context → enter → explain → demonstrate → verify → close
  ▼
Narration and Captions
  │  evidence-backed script; optional measured ElevenLabs segments
  ▼
Layered Remotion Renderer
  │  browser, camera, cursor, click effects, highlights, captions, audio
  ▼
Quality Gates
  │  execution, story, visual, sync, audio, delivery
  ▼
Final delivery or owning-layer repair
```

No layer silently replaces another. Exploration discovers; planning selects; execution proves; the director creates the viewer journey; rendering composes; QA decides whether delivery is allowed.

### Implementation map

The architecture is split into explicit Python packages rather than one
monolithic generator:

| Layer | Package | Owns |
|---|---|---|
| API/auth | `app.api`, `app.auth` | HTTP contracts, sessions, account scoping, run commands |
| Orchestration | `app.services`, `app.orchestration`, `app.workers` | stage ordering, durable jobs, leases, heartbeats, resume/retry |
| Contracts | `app.contracts` | versioned Pydantic models shared by every stage |
| Discovery | `app.discovery` | URL normalization, DOM/accessibility evidence, page knowledge, relevance |
| Providers | `app.providers` | Browserbase, Stagehand, OpenRouter, ElevenLabs boundaries |
| Planning | `app.planning` | capability extraction, candidate flows, scoring, side-effect policy, validation |
| Interaction | `app.interaction`, `app.execution` | affordances, semantic targets, browser dispatch, state witnesses |
| Persistence | `app.persistence`, `app.artifacts`, `app.storage` | database lineage, artifact manifests, knowledge versions; DDL is isolated in `persistence/schema.py` |
| Presentation | `app.presentation` | journey direction, storyboard, captions, camera/cursor plans |
| Narration | `app.narration` | grounded script, caption timing, optional audio segments |
| Video | `app.video` | source timing, native-speed cuts, Remotion/FFmpeg composition |
| Quality | `app.quality`, `app.evaluation` | execution/story/visual/sync/delivery gates and repair classification |

The frontend in `frontend/` consumes the API and displays this state. It does
not execute browser actions or make delivery decisions.

The generation coordinator delegates reusable knowledge materialization to
`services/knowledge.py` and pure URL/media/duration policies to
`services/generation_policy.py`; these modules contain no browser lifecycle or
provider calls and are independently testable.

### Durable lifecycle

The worker advances persisted stages through the following lifecycle. Every
transition is checkpointed in the database and can be resumed from its owning
boundary:

```text
QUEUED
  → FEASIBILITY_CHECK
  → DISCOVERING
  → PLAN_READY
  → PLAN_VALIDATED
  → EXPLORATORY_EXECUTION
  → WORKFLOW_VALIDATED
  → PRODUCTION_EXECUTION
  → TRACE_READY
  → PRESENTATION_PLANNED
  → NARRATION_READY
  → RENDERING
  → RENDERED
  → VIDEO_QA
  → QA_PASSED
  → COMPLETE
```

Any stage may become `FAILED`. Only classified retryable failures may resume,
and the retry boundary is chosen by the repair policy. A provider outage does
not cause the workflow to be replayed as if it had succeeded; a dispatched
mutation is never blindly repeated.

## Video creation flow

### 1. Request and objective interpretation

`ObjectiveSpec` normalizes the user request into:

- requested feature/module and demo type;
- audience and purpose;
- minimum, target, and maximum duration;
- must-show content and exclusions;
- read-only versus explicitly authorized mutation policy;
- success criteria and expected outcome.

The objective controls exploration depth and narration style. A full tour, feature walkthrough, sales demo, onboarding video, recruiter portfolio tour, and changelog video are different editorial jobs even when they use the same URL.

The API keeps compatibility with existing requests, but internally all generation runs use the normalized objective. Credentials are referenced by opaque names and never enter prompts, trace text, screenshots, captions, logs, or analytics.

### 2. Exploration and product understanding

Exploration happens in a browser context separate from production recording. It is allowed to inspect the product and perform bounded safe probes. It records:

- canonical URL and redirect state;
- page title and purpose;
- visible sections and scroll landmarks;
- cards, tables, forms, controls, labels, roles, and geometry;
- DOM and accessibility evidence;
- screenshots and loading behavior;
- safe state transitions and observed outcomes;
- authentication, CAPTCHA, iframe, shadow-root, and infrastructure blockers.

URLs are canonicalized before comparison. HTTP-to-HTTPS redirects, trailing slashes, query normalization, and SPA route state do not create duplicate opening loads.

Exploration uses bounded adaptive budgets. The default is intentionally finite; a full walkthrough can expand only when visible primary navigation or the relevance graph proves that more pages are needed. A run stops when every requested outcome has evidence, every selected page has structured knowledge, and at least one candidate flow is grounded.

Stagehand is a semantic/visual observation provider. Its candidates are advisory until re-grounded against the current ProductLens Playwright page. Stagehand observations, rehearsal results, latency, and provider failures are persisted. A missing behavior witness is a classified pre-production blocker for an interaction objective, not permission to guess.

### 3. Knowledge model

The knowledge artifacts are:

- `ProductKnowledge`: product fingerprint/version, navigation, routes, entities, controls, interaction patterns, known blockers, successful actions, and freshness;
- `PageKnowledge`: canonical URL, purpose, sections, landmarks, facts, controls, forms, screenshots, DOM/accessibility evidence, and loading behavior;
- `FeatureKnowledge`: purpose, entry points, dependencies, supporting pages, expected outcomes, and evidence references;
- feature/relevance graph: relationships between pages, entities, controls, and state transitions.

Knowledge is versioned and fingerprinted. It is reused only while fresh and is re-grounded in the current browser before a production plan can consume it. Stale evidence is invalidated rather than silently trusted.

### 4. Workflow discovery and validation

The planner generates and scores candidate flows by:

- objective relevance and audience value;
- explanatory value and visible outcome clarity;
- safe demonstrability and interaction reliability;
- evidence completeness and duration fit;
- visual richness and page-local content coverage.

Every selected page has a reason for inclusion: establish context, explain a capability, configure input, reveal detail, demonstrate behavior, prove an outcome, or close the story. Navigability alone is not a reason.

Every `WorkflowStep` carries semantic intent, target evidence, preconditions, postconditions, importance, narration intent, visual intent, retry policy, fallback strategy, and completion criteria.

Visible semantic navigation is preferred. Direct route navigation is allowed only when no reliable visible control exists and is recorded as an intentional fallback.

The planner does not synthesize product-specific labels, routes, canvas nodes, connectors, or fixed grid coordinates. Unknown behavior is rejected or sent back to targeted exploration.

### 5. Interaction layer

The interaction layer is the primary reliability boundary. Its generic browser actuators are:

- click/open/close;
- type/fill/search;
- select/check/radio/date;
- key press;
- hover;
- scroll;
- drag and pointer sequence;
- upload;
- wait/read/verify.

These are browser mechanisms, not application-specific workflows. The model and observation layer decide which mechanism applies to the current visible state.

The interaction kernel stores:

- `InteractionSnapshot` observations;
- visible enabled `Affordance`s;
- proposed `ActionCandidate`s;
- `ActionAttempt` lifecycle and retry lineage;
- before/after state snapshots;
- `OutcomeVerification` records;
- bounded recovery decisions;
- capability profiles and evidence references.

The intended closed loop is:

```text
observe current state
→ propose a semantic candidate
→ resolve a unique visible target
→ check safety and preconditions
→ dispatch once
→ capture the resulting state
→ verify an independent witness
→ re-observe before choosing the next action
```

The engine re-grounds locators immediately before dispatch, verifies focus before keyboard input, verifies selected values/options for controls, checks overlays by spatial occlusion, and rejects drawing gestures that produce no observable surface change. A dispatched side effect is never blindly replayed.

The current implementation records the interaction-kernel lifecycle and enforces execution witnesses, but broad arbitrary-app action selection still requires further live behavioral rehearsal. This limitation is explicit so that a safe rejection cannot be mistaken for successful automation.

### 6. Forms and custom controls

Native inputs are clicked, cleared, and typed with visible per-character timing. Custom controls are resolved by role, label, placeholder, observed selector, accessibility state, and current visibility. For a custom combobox, ProductLens opens the control, waits for a visible option surface, selects an observed option, and verifies the selected value.

Validation recovery is generic: after a permitted isolated submit, the worker re-reads visible invalid controls, discovers newly enabled dependent fields, fills only observed controls, and retries before any production recording. The resulting form schema is promoted only when the live dependency is verified.

### 7. Canvas and graph editors

Canvas and graph workspaces require stronger evidence than a page hash. ProductLens must observe the editing tool, focus/editor state, target surface, current geometry, and object/relationship result. Pointer paths are derived from current observed geometry and interpolated at native speed.

The engine accepts a canvas mutation only when it can prove a structural object/edge change or a target-local rendered surface change. A canvas being visible is not proof that a label, shape, node, or connector was created. Fabricated labels, synthetic coordinates, and arbitrary connector paths are deliberately disallowed.

This is the main remaining high-complexity capability: a live Stagehand rehearsal must learn the editor's actual behavior in an isolated context before production can claim it.

### 8. Production capture

Production uses a fresh browser context after exploration and planning pass. Authenticated storage is seeded only when required. The opening page is loaded once, allowed to settle, and held before the first gesture.

Before each scene the executor checks:

- document and transition readiness;
- target visibility and uniqueness;
- relevant content readability;
- overlay/chooser state;
- expected route and application state.

Each trace event includes action and occurrence timestamps, target geometry, viewport, URL, scroll state, screenshots, DOM/accessibility references, loading intervals, action result, state transition, errors, and recovery information.

Execution scrolling uses semantic targets for reliability, while presentation scrolling uses gradual, human-sized increments with intermediate positions, easing, and settle time. Browser zoom remains at native scale unless a bounded, target-specific presentation zoom is proven safe.

### 9. Video Journey Director

The director converts a verified trace into a coherent viewer journey. Every page follows:

```text
Establish → explore → explain → demonstrate/inspect → verify takeaway → transition
```

Scenes answer:

- what is visible;
- why it matters;
- what changed from the previous scene;
- what the viewer should understand next.

Trace segments are classified as `ESSENTIAL`, `TRANSITIONAL`, or `DEAD_TIME`. Essential actions and their visible result are retained at native speed. Only proven transport/dead time can be cut. Typing, dropdowns, dragging, drawing, scrolling, and transitions are never reduced to unexplained micro-windows.

The director does not return to an already completed opening page merely to repair coverage. A page that was only opened but not explored fails story QA.

### 10. Camera, cursor, and composition

Presentation state is independent of browser zoom:

- `CameraState`: position, scale, target, duration, easing, safety bounds;
- `CursorState`: source/destination, hover pause, click point, ripple, visibility, easing;
- `ScrollState`: source/destination, landmarks, speed, duration, settle time, narration alignment.

The default composition preserves the complete application frame and browser context. Small bounded cinematic zooms are allowed only when the target is meaningful, sufficiently sized, and remains in frame. Edge/header targets cannot cause critical chrome or page context to be cropped.

Cursor movement and click effects are derived from the same trace event that drives the scene. They are not independently guessed by the renderer.

### 11. Narration and captions

Narration is generated in two passes:

1. Fact extraction produces page/section purpose, visible descriptions, project/company/feature names, roles, contributions, outcomes, and exact evidence references.
2. Editorial writing turns approved facts and the validated scene plan into concise audience-specific copy.

Every caption line is linked to a stable scene and evidence ID. It must explain what is visible, why it matters, and the viewer takeaway. Project names alone, company names alone, route labels, click instructions, screen transcripts, generic filler, and unsupported claims are rejected.

The opening scene includes a product-specific introduction when the evidence supports one. Captions, cursor timing, scene timing, and future audio all consume the same approved script.

Caption-only mode is the default while ElevenLabs is unavailable. When TTS is enabled, the pipeline is:

```text
approved script → generated audio → measured segment timing → captions → scene timing → render
```

Timing is never estimated from character count.

### 12. Remotion rendering

The reusable composition is layered:

```text
BackgroundLayer
BrowserLayer
CameraLayer
CursorLayer
ClickEffectLayer
HighlightLayer
CaptionLayer
AudioLayer
BrandLayer
TransitionLayer
```

Browserbase native recordings are the primary source. CDP screenshots are diagnostic/fallback evidence, not a substitute for continuous browser footage. The renderer validates that the source recording belongs to the same run and matches the trace/media manifest.

Rendering uses native source speed. It does not manufacture duration with freezes, excessive slowdown, or arbitrary slideshow frames. Human-visible action windows retain causal movement and result dwell; only proven remote transport gaps are removed. Caption-only output strips incidental source audio.

The source frame rate is preserved where possible; the renderer reports the source rate and rejects material pacing degradation. Final output also has resolution and bitrate checks.

### 13. Quality gates and repair

Quality is evaluated in independent layers.

Execution QA rejects failed or unverified critical actions, duplicate opening loads, unexplained navigation, missing postconditions, unsafe mutations, and incorrect page state.

Exploration/story QA rejects irrelevant route crawling, stale knowledge, pages without local exploration, missing requested content, return-home repair loops, title-only narration, unsupported claims, and insufficient scene dwell.

Visual QA samples rendered frames and trace evidence for full-frame composition, readable text, source-site fidelity, smooth scrolling, cursor alignment, camera bounds, effects/transitions, black or frozen frames, lag/stutter, crop, and caption contrast/timing.

Synchronization QA checks scene IDs and timestamps across trace events, storyboard scenes, script lines, captions, cursor actions, audio segments, and render composition.

Delivery QA requires a complete manifest, valid MP4, non-empty duration, expected frame rate, acceptable bitrate, final QA reports, and no unresolved hard failures.

Repairs are classified and limited to the owning layer:

```text
discovery/relevance → targeted re-exploration
workflow → candidate-flow regeneration
execution → affected scene/page re-execution
narration → script regeneration from approved facts
presentation → rerender from existing trace
video QA → repair only the failed visual/editorial layer
```

### 14. Persistence and artifacts

Every run has a run-local artifact root. The normal structure is:

```text
<artifact-root>/<run-id>/
├── objective.json
├── discovery/
│   ├── product-context.json
│   ├── product-knowledge.json
│   ├── page-knowledge/
│   ├── screenshots/
│   ├── capabilities.json
│   ├── relevance-graph.json
│   ├── stagehand-observation.json
│   └── viewport-decision.json
├── page-knowledge/
├── feature-graph.json
├── candidate-flows.json
├── plan.json
├── validated-scene-plan.json
├── execution/
│   ├── trace.json
│   ├── interaction-trace.json
│   ├── action-attempts.json
│   ├── state-snapshots.json
│   ├── screenshots/
│   ├── observations/
│   └── browserbase-recording.json
├── presentation/
│   ├── presentation-plan.json
│   ├── storyboard.json
│   ├── cursor-plan.json
│   └── editorial-brief.json
├── narration/
│   ├── script.json
│   ├── captions.json
│   └── audio/
├── quality/
│   ├── execution-report.json
│   ├── editorial-report.json
│   ├── visual-report.json
│   ├── synchronization-report.json
│   └── delivery-report.json
├── repair/
└── final/
    ├── demo.mp4
    └── manifest.json
```

Database records point to these artifacts and preserve run lineage, stage transitions, provider attempts, retry boundaries, and knowledge versions. A trace and recording from different runs are rejected.

### 15. Providers and security

Providers are replaceable boundaries. ProductLens owns workflow truth.

- Playwright: semantic browser execution, state witnesses, local video capture, and tracing.
- Browserbase: cloud browser sessions and provider-native session recording.
- Stagehand: semantic/visual observation, candidate actions, and isolated rehearsal.
- OpenRouter: structured fact extraction and editorial writing.
- ElevenLabs: optional TTS only; absence does not lower caption quality.
- Remotion/FFmpeg: deterministic composition, encoding, and media probes.
- SQLite: zero-configuration local database.
- PostgreSQL: supported production database dialect.
- Local worker/Dramatiq: asynchronous stage execution and provider backpressure.

Secrets are loaded from environment/provider references. They are never accepted as ordinary request fields and are redacted from traces, prompts, screenshots, logs, analytics, and public API responses.

### 16. Database and workers

The local default is SQLite with WAL mode. Production deployments should use PostgreSQL and run Alembic migrations. The database stores users, projects, requests, runs, stage jobs, artifacts, provider calls, delivery leases, knowledge versions, heartbeats, and retry lineage.

The local worker claims persisted outbox jobs atomically and recovers expired claims. Production can use Dramatiq with a broker URL. Provider concurrency limits are controlled independently for Browserbase, Stagehand, OpenRouter, and workers. Clients should consume the run event stream rather than repeatedly polling stage status.

### 17. API surface

Important endpoints include:

```text
GET  /health
GET  /readiness
POST /auth/signup
POST /auth/login
GET  /auth/me
GET  /providers
POST /understanding/preview
GET  /projects
POST /projects
POST /fixture-runs
POST /runs
GET  /runs
GET  /runs/{run_id}
GET  /runs/{run_id}/details
GET  /runs/{run_id}/events       # SSE
GET  /runs/{run_id}/stages
GET  /runs/{run_id}/artifacts
GET  /runs/{run_id}/video
POST /runs/{run_id}/retry
POST /runs/{run_id}/resume
POST /runs/{run_id}/cancel
DELETE /runs/{run_id}
POST /knowledge/invalidate
```

`POST /understanding/preview` is advisory and non-recording. A generation run always re-grounds its own evidence. `POST /fixture-runs` is provider-free for deterministic gates. `POST /runs` starts the asynchronous live pipeline.

### 18. Studio/frontend boundary

The Next.js frontend in `frontend/` is a client of the API, not a second video engine. Its responsibilities are account/session handling, project and run creation, objective input, provider/readiness display, run-stage/event display, artifact/video access, and retry/resume/cancel controls. It must never decide which browser action to execute or rewrite a trace. The backend remains the source of truth for objective, plan, trace, narration, QA, and delivery state.

The current engineering focus is the backend pipeline and interaction reliability. Frontend screens can consume the existing authenticated API, but frontend polish is not evidence that a live interaction workflow is supported.

### 19. Configuration

Create `backend/.env` from the example and keep it out of version control:

```text
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=...
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=...
BROWSERBASE_API_KEY=...
BROWSERBASE_PROJECT_ID=...
STAGEHAND_MODEL=...
STAGEHAND_NODE=node
PRODUCTLENS_DATABASE_URL=sqlite:///./artifacts/productlens.sqlite3
PRODUCTLENS_WORKER_MODE=polling
PRODUCTLENS_WORKER_CONCURRENCY=...
PRODUCTLENS_BROWSERBASE_CONCURRENCY=...
PRODUCTLENS_STAGEHAND_CONCURRENCY=...
PRODUCTLENS_OPENROUTER_CONCURRENCY=...
```

For credentials, use an opaque reference such as `secret://productlens/demo`; configure the corresponding worker environment variables. Do not put usernames/passwords in a JSON request, prompt, or artifact.

### 20. Local setup

```powershell
cd "ProductLensAI 2.0/backend"
python -m pip install -e ".[dev]"
python -m playwright install chromium
alembic upgrade head
python -m app.workers.local
```

In another terminal:

```powershell
cd "ProductLensAI 2.0/backend"
uvicorn app.api.main:app --reload --port 8000
```

The default artifact root is `backend/artifacts`. Runtime artifacts, recordings, databases, samples, and test HTMLs are ignored by Git. The legacy `ProductLens AI/` project is reference material only; this application does not read its runtime configuration.

### 21. Verification commands

Run deterministic tests first:

```powershell
cd "ProductLensAI 2.0/backend"
python -m pytest -m "not integration" -q --disable-warnings
python -m compileall -q app
```

Run the longer integration suite when browser/provider resources are available:

```powershell
python -m pytest -m integration -q --disable-warnings
```

For a fixture gate:

```powershell
python -m app.benchmark.run_gate 3 --render
```

For a provider-backed URL, run discovery/plan first with `render: false`, inspect the persisted plan and trace, and only then render. Use the SSE endpoint for progress instead of an aggressive polling loop.

### 22. Genericity policy

The generic implementation must not contain Portfolio, SmartSevak, Excalidraw, n8n, route names, field names, project names, or fixed coordinates. Product-specific behavior belongs in:

- fresh page evidence;
- objective text;
- Stagehand observations/rehearsal;
- validated run-local workflow data;
- explicit user authorization.

Browser-level primitives and evidence validators are generic. They do not constitute product hardcoding. A special-case branch is acceptable only when it is a provider/browser safety boundary and does not encode an application workflow.

### 23. Known limitations and honest acceptance boundary

ProductLens can reliably execute actions when the application exposes observable, groundable UI state and an independent result witness. It cannot honestly guarantee arbitrary behavior when:

- the UI is inaccessible or visually ambiguous;
- the application is a remote desktop or opaque embedded surface;
- a canvas/graph editor does not expose any structural or pixel witness;
- CAPTCHA, OTP, authentication, or provider infrastructure blocks the browser;
- a mutation is not explicitly authorized;
- the site changes behavior between rehearsal and production.

For those cases the system must classify the limitation, preserve evidence, and offer a read-only or human-assisted path where possible. “No false demo” is a required reliability property.

MVP readiness requires more than passing unit tests: each supported workflow class must pass isolated rehearsal, independent production execution, complete trace, narration evidence checks, render QA, and manual review of the final video. The live SmartSevak and Excalidraw workflows remain the proving ground for the interaction layer.
