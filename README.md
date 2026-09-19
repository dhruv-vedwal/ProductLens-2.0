# ProductLens 2.0

ProductLens 2.0 is a reliability-first product-demo generation system. It accepts a URL and a natural-language demo objective, understands the visible product, discovers a safe workflow, captures browser evidence, writes an evidence-grounded story, and renders a caption-led demo video. ElevenLabs audio is optional.

**This README is the single product documentation entry point.** It covers runtime
architecture (durable stages `DISCOVERY` → `VIDEO_QA`, rehearsal witnesses,
execution verification, Delivery QA), backend/local ops, deployment-like infra,
architecture assessment / refactor priorities, and verification. Package-local
READMEs under `frontend/packages/` stay with their packages only.

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

The live URL pipeline is coordinated by `DemoJobService` (`backend/app/services/jobs.py`).
A run is either started by `POST /runs` (worker advances stages) or by a supervised
script such as `backend/scripts/run_mvp_consecutive.py` (same stage implementations,
in-process loop). Every stage reconstructs its inputs from
`artifacts/runs/{run_id}/` — there is no in-memory pipeline after a crash.

```text
Request (URL + objective + optional secret:// credential)
  │
  ▼
DISCOVERY     explore + auth + Stagehand witness   (Browserbase session, not the film)
  │
  ▼
PLANNING      hidden rehearsal (no film) + DemoPlan + editorial enrich + preflight
  │
  ▼
EXECUTION     fresh Browserbase session → DemoTrace + browser-recording.mp4
  │
  ▼
NARRATION     reuse planning storyboard → captions (+ optional TTS)
  │
  ▼
RENDER        Remotion/FFmpeg → final/demo.mp4
  │
  ▼
VIDEO_QA      delivery aggregate → COMPLETE or classified repair child
```

No layer silently replaces another. Discovery learns; planning authorizes and
scripts; execution proves and films; narration attaches copy; rendering composes;
VIDEO_QA decides whether delivery is allowed.

**Browser sessions vs film:** a happy Lead-style run typically opens **three**
cloud browser sessions (discovery, rehearsal, production) but records the
**demo film only once**, in EXECUTION. Discovery and rehearsal spend Browserbase
time without writing the deliverable MP4 source.

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

### Durable stages (checkpoint names)

These are the six durable stage jobs advanced by `run_url` /
`run_url_stage` (`backend/app/workers/tasks.py`, `backend/app/services/stage_contracts.py`):

| Stage | Lifecycle label | Owns |
|---|---|---|
| `DISCOVERY` | `DISCOVERING` | Explore product, auth, Stagehand, product-context |
| `PLANNING` | `PLAN_VALIDATED` | Rehearsal (when create authorized) + plan + editorial enrich/preflight |
| `EXECUTION` | `PRODUCTION_EXECUTION` | Film walkthrough, DemoTrace, presentation contracts |
| `NARRATION` | `PRESENTATION_PLANNED` | Captions / optional TTS from approved storyboard |
| `RENDER` | `RENDERING` | Remotion composition → `final/demo.mp4` |
| `VIDEO_QA` | `VIDEO_QA` | Delivery aggregate; ship or repair |

Bookkeeping may also mark `FEASIBILITY_CHECK` around the attempt. Finer
lifecycle markers (`TRACE_READY`, `QA_PASSED`, …) are progress checkpoints
inside those stages, not separate Browserbase loops.

Any stage may become `FAILED`. Only classified retryable failures may resume,
and the retry boundary is chosen by the repair policy. A provider outage does
not cause the workflow to be replayed as if it had succeeded; a dispatched
mutation is never blindly repeated.

## Pipeline stages in detail

This section is the authoritative runtime walkthrough for URL demos (including
SmartSevak Lead–style authenticated create flows). Source of truth:
`backend/app/services/jobs.py` and the generation mixins under
`backend/app/services/generation/`.

### Credits at a glance

| Stage | Browserbase | OpenRouter (typical) |
|---|---|---|
| DISCOVERY | Explore session (+ Stagehand) | Objective understanding |
| PLANNING | Rehearsal session (recording off) | Planner + brief/scene enrich |
| EXECUTION | Production session (**film**) | Optional advisory Stagehand reads |
| NARRATION | — | Only on editorial refresh / missing storyboard |
| RENDER | — | — |
| VIDEO_QA | — | Multimodal visual review when required |

---

### 1. DISCOVERY

**Purpose.** Learn the product enough to plan. **No deliverable recording.**

**Inputs.** Request URL/objective; optional knowledge cache; `credential_reference`;
`cloud_discovery`; discovery budget (`max_pages`, actions, model calls).

**Flow.**

1. Optional OpenRouter structured objective understanding → `ObjectiveSpec`.
2. Launch Browserbase (cloud) or local Chromium; persist
   `discovery/browserbase-session.json` (no secrets / connect URLs).
3. Navigate to URL (common hard fail: `Page.goto` timeout).
4. If a login form is visible, `CredentialService.authenticate_if_required`
   resolves `secret://…` and fills credentials (common hard fail:
   `AUTH_PAGE_CLOSED`, `AUTH_REQUIRED`). Secrets never enter traces or prompts.
5. `LiveDiscovery.discover` gathers routes, controls, forms, DOM/a11y evidence,
   page-knowledge within the adaptive budget.
6. Cloud: Stagehand observe-only enrich on the same session. Interaction
   objectives can hard-fail if Stagehand is `UNAVAILABLE`
   (`BEHAVIOR_OBSERVATION_REQUIRED`).
7. Viewport decision → `discovery/viewport-decision.json`.
8. Close session; materialize product knowledge. When
   `allow_isolated_record_creation` is set, stamp
   `permitted_mutations=["create_isolated_record"]`.

**Key artifacts.** `discovery/product-context.json`, `product-knowledge.json`,
`capabilities.json`, `behavioral-product-model.json`, `stagehand-observation.json`,
`candidate-flows.json`, `feature-graph.json`, `page-knowledge/*`.

---

### 2. PLANNING (includes hidden rehearsal for authorized creates)

**Purpose.** Prove one isolated create **without filming**, compile the immutable
`DemoPlan`, write narration that passes **editorial preflight before** production
Browserbase.

#### 2a. Hidden rehearsal

Runs inside PLANNING when `allow_isolated_record_creation` is true
(`rehearsal_stage` in `backend/app/services/generation/rehearse.py`).

1. Select a submit-capable creation capability from discovery evidence.
2. Open a **fresh** browser session with recording disabled.
3. Authenticate; hydrate synthetic field values; execute create operations.
4. Recover visible validation / dependent fields when needed.
5. Wait for an independent post-submit **witness** (see deep dive below).
6. Persist attempt/report; write `planning/certified-workflow-graph.json`;
   promote form fields with `rehearsal:include` markers for production.

Hard fails include `REHEARSAL_CAPABILITY_*`, `REHEARSAL_FORM_VALIDATION_UNRESOLVED`,
`REHEARSAL_OUTCOME_UNVERIFIED`, and auth errors.

#### 2b. Plan compile + editorial

1. Build `planning/demo-brief.json` (human-reviewable story boundary).
2. OpenRouter structured planner → `plan.json` workflow steps.
3. Require certified workflow graph for authorized creates.
4. Persist `planning/validated-state-graph.json` and capability resolutions.
5. **Editorial path:**
   ```text
   build_editorial_storyboard()      # evidence drafts (may be skeletons)
        → enrich_editorial_brief()   # OpenRouter: Welcome to…, facts
        → enrich_editorial_storyboard()  # OpenRouter: per-scene lines
        → inspect_editorial_preflight()  # fail closed before EXECUTION
   ```
6. Persist `presentation/storyboard.json`, `editorial-brief.json`,
   `narration/fact-extraction.json`, `qa/editorial-preflight.json`.

Preflight rejects missing “Welcome to…”, repetitive skeleton walls
(`REPETITIVE_EDITORIAL_NARRATION`), generic route-label chrome, and similar
ungrounded copy — so EXECUTION credits are not spent on a doomed script.

---

### 3. EXECUTION

**Purpose.** Film the real walkthrough. Largest Browserbase spend.

**Inputs.** `plan.json`, product context, validated state graph, viewport,
planning storyboard, credential reference.

**Flow.**

1. Validate state-graph ↔ plan consistency.
2. Open a **new** Browserbase session (independent of discovery/rehearsal).
3. Start capture: cloud CDP screencast (or local Playwright `record_video`).
4. Navigate; authenticate with observable `auth:*` events (no secret values).
5. Opening dwell from storyboard; skip redundant first Navigate when already on URL.
6. Semantic executor (`ExecutionEngine` + `PlaywrightAdapter`) runs each
   operation: re-ground target → dispatch once → verify postconditions
   (see deep dive below). Stagehand may advise at semantic boundaries only;
   Playwright postconditions remain authoritative.
7. Adaptive replan may write `adapted-plan.json` / `plan-effective.json`.
8. Close session; assemble `execution/browser-recording.mp4` + `execution/trace.json`.
9. Build moments, sync EDL, scene/presentation plans, bind storyboard to
   successful operation IDs.
10. Hard gates: coverage, certified outcomes, story, journey QA.

**Key artifacts.** `execution/trace.json`, `browser-recording.mp4`,
`action-attempts.json`, `verification-results.json`,
`presentation/{scene-plan,presentation-plan,sync-edl,actual-flow-storyboard}.json`,
`qa/{coverage,outcome,story,execution}-report.json`.

---

### 4. NARRATION

**Purpose.** Attach captions (and optional TTS) to the immutable trace. No browser.

**First pass.** Load the planning `presentation/storyboard.json` (approved
enriched wording). Do **not** rebuild deterministic skeleton drafts — that
historically caused `REPETITIVE_EDITORIAL_NARRATION` after EXECUTION had already
spent Browserbase. Bind scenes to successful ops; build editorial script →
captions; run `inspect_editorial`; optionally synthesize audio.

**Repair pass** (`refresh_editorial=true`). Rebuild storyboard from plan evidence
and re-enrich with OpenRouter, then re-caption. Used when repair retries from
`NARRATION`.

**Key artifacts.** `presentation/captions.json`, `narration-script.json`,
`qa/editorial-report.json`, optional `audio/narration.mp3`.

---

### 5. RENDER

**Purpose.** Compose the studio MP4. No browser, no LLM required.

`render_run` loads trace + presentation + captions + source recording, runs
Remotion (chunked segments → concat), writes candidate then atomically promotes
`final/demo.mp4`. Status and intermediates live under `render/`.

Skipped when `render=false` (VIDEO_QA is also skipped).

---

### 6. VIDEO_QA (delivery)

**Purpose.** Aggregate every required gate. Only here is a run “deliverable.”

`qa_stage` reconciles trace/EDL integrity, inspects video + presentation +
synchronization + editorial + journey/coverage, runs multimodal review when
create/submit semantics require it, builds the artifact manifest, then
`delivery_report(...)`.

- If any hard failure → `qa/repair-decision.json` + raise.
- If clean → COMPLETE; repair children promote parents via `_promote_repair_success`.

See **Delivery QA scoring** below for how layers combine.

---

### Automatic repair

On stage failure, `_schedule_automatic_repair` + `classify_repair`:

1. Read/synthesize `qa/repair-decision.json` (`retry_from_stage`, category, action).
2. Stop if `fail` / `needs_input`, category attempts ≥ 3, or wall-clock budget
   exhausted → `qa/repair-exhausted.json`, often `NEEDS_INPUT`.
3. Else create a **child** `run_id`, `clone_for_targeted_retry` predecessor
   evidence only, mark prior stages COMPLETE, enqueue from the boundary.
4. `refresh_editorial=true` when retrying from `NARRATION`.
5. Supervised `run_url` detects parent `REPAIRING` and runs the child recursively.

Typical boundaries: auth → needs_input / DISCOVERY; rehearsal/Stagehand →
DISCOVERY or PLANNING; editorial/captions → NARRATION; trace/outcomes →
EXECUTION; Remotion → RENDER; multimodal → VIDEO_QA.

---

## Deep dive: rehearsal witnesses

**Why.** Planning must prove create works before production films it. A weak
witness poisons the certified workflow and wastes EXECUTION.

**Strong witnesses** (`backend/app/services/generation/rehearse.py`):

- **Post-submit detail navigation.** Same origin, canonical path changed after
  submit → evidence `rehearsal-visible-outcome:post-submit-detail-navigation`
  and `outcome_target` pointing at the new URL. Query/fragment are stripped so
  transient UI state is not persisted as truth.
- **Groundable outcome target.** Reusable witnesses need a selector, semantic
  role, or concise visible value that production can re-ground.

**Rejected / non-reusable witnesses** (`_rehearsal_witness_is_reusable`):

- unverified capability or missing outcome target;
- header/table transcript shaped as `#…` without selector;
- dialog transcripts that are too long / lack `rehearsal:include` field markers;
- dependent forms without promoted required non-select fields;
- broken bracket selectors;
- long free text without a selector (transcript, not a target).

**Form readiness before submit.** Count `aria-invalid` / `:invalid` controls and
whether the submit control is disabled. Validation recovery re-reads newly
enabled dependent fields, fills only observed controls, and retries — still in
the unrecorded rehearsal context.

**Promotion.** Successful rehearsal stamps `rehearsal:include` on learned fields
and writes `planning/certified-workflow-graph.json`. Production planning
hard-fails authorized creates when that graph is missing.

---

## Deep dive: execution semantic loop and outcome verification

**Closed loop** (`backend/app/execution/engine.py`):

```text
observe → propose/resolve unique visible target → safety + preconditions
  → dispatch once → capture after-state → verify independent witness
  → re-observe before next action
```

Locators are re-grounded immediately before dispatch. Focus is verified before
keyboard input. A **dispatched** side effect is never blindly replayed merely
because verification needs another locator (`GroundingError` after dispatch is
not a free retry of the mutation).

**Create / URL-changed outcomes.** When a postcondition expects a new URL
(created-record navigation), the engine:

1. Confirms navigation / expected URL.
2. Polls the settled page (body text + input/textbox values) because SPAs often
   paint chrome before entity fields hydrate.
3. Matches demonstrated synthetic values via `specific_entity_fields` (digit
   spans for phones, etc.).
4. Requires ≥2 matched fields by default, or ≥1 when another explicit entity
   witness postcondition exists.
5. On timeout → `VerificationError`: created URL changed but lacks enough
   demonstrated entity fields.
6. Writes `after.verified_outcome` with `matched_form_fields` /
   `matched_form_values` onto the trace event.

**Certified outcome QA** (`inspect_certified_outcomes`) later requires plan-level
certified outcomes to appear as verified events in the trace. Coverage QA
ensures selected workflow steps were attempted successfully.

Stagehand may be consulted only at semantic boundaries (navigate, modal,
submit/create, verify, …). Its payload is advisory; Playwright postconditions
remain the source of truth.

---

## Deep dive: Delivery QA scoring (VIDEO_QA)

`delivery_report` (`backend/app/quality/delivery.py`) is a **fail-closed union**:

```text
hard_failures =
  missing required artifacts
  ∪ execution hard_failures
  ∪ story hard_failures
  ∪ video hard_failures
  ∪ synchronization hard_failures
  ∪ visual_review (multimodal) hard_failures
```

`deliverable = (hard_failures is empty)`. `overall_score` is `1.0` only when
deliverable; otherwise `0.0`. Each failure records `owner_by_failure` so repair
classification knows which layer owns the symptom.

**Video layer** (`quality/video.py`) includes:

- duration / black / frozen / pacing probes;
- **source faithfulness:** sample rendered frames vs browser evidence.
  - max correlation below 0.25 → `SOURCE_FOOTAGE_STRUCTURALLY_UNRELATED`;
  - median / majority below floor → `SOURCE_FOOTAGE_LOW_STRUCTURAL_SIMILARITY`
    (stricter for raw sources; lower floor for already-edited editorial sources).

**Presentation / sync** commonly emit `CAPTION_COVERAGE_TOO_SHORT`,
`CAPTION_WITHOUT_PRESENTATION_BEAT`, `CAPTION_TRACE_MISMATCH`.

**Multimodal review** is mandatory for many create/submit demos; provider JSON
failures surface as `MULTIMODAL_REVIEW_UNAVAILABLE` (and typed variants).

**Repair children** must still carry planning artifacts (`demo_brief`,
`certified_demo_script`, `validated_state_graph`, outcome/certified workflow
flags, …). Missing copies show up as delivery hard failures even when the MP4
exists — clone allow-lists in `artifacts/store.py` must stay aligned with the
manifest.

Outputs: `qa/delivery-report.json`, `gap-report.json`, `completion-audit.json`,
plus per-layer reports under `qa/`.

---

## Video creation flow

The numbered subsections below expand product behavior inside the durable
stages above. Prefer the **Pipeline stages in detail** section when debugging
stage ownership, credits, or repair boundaries.

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

Narration is authored primarily in **PLANNING**, then bound in **NARRATION**:

1. **Fact extraction / brief** — evidence-cited facts and product purpose land in
   `presentation/editorial-brief.json` and `narration/fact-extraction.json`.
2. **LLM enrich (required when a structured provider is configured)** —
   `enrich_editorial_brief` then `enrich_editorial_storyboard` write polished,
   grounded scene lines into `presentation/storyboard.json`. Deterministic
   drafts may be skeletons; skeletons are not shippable finals (preflight and
   enrich fail closed on repetitive “control in focus…” walls).
3. **Editorial preflight** — runs in PLANNING before EXECUTION.
4. **First-pass NARRATION** — reuses the planning storyboard (does not rebuild
   drafts). Builds the caption script, runs trace-aware `inspect_editorial`,
   and optionally synthesizes ElevenLabs audio.
5. **Narration repair** — `refresh_editorial=true` rebuilds + re-enriches from
   the same plan/evidence, then regenerates captions.

Every caption line is linked to a stable scene and evidence ID. It must explain
what is visible, why it matters, and the viewer takeaway. Project names alone,
company names alone, route labels, click instructions, screen transcripts,
generic filler, and unsupported claims are rejected.

The opening scene must present a product-specific introduction when evidence
supports one (preflight requires a “Welcome to…” opening). Captions, cursor
timing, scene timing, and future audio all consume the same approved script.

Caption-only mode is the default while ElevenLabs is unavailable. When TTS is
enabled, the pipeline is:

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

Quality is evaluated in independent layers, then **unioned** at VIDEO_QA by
`delivery_report` (see **Deep dive: Delivery QA scoring**).

Execution QA rejects failed or unverified critical actions, duplicate opening
loads, unexplained navigation, missing postconditions, unsafe mutations, and
incorrect page state.

Exploration/story QA rejects irrelevant route crawling, stale knowledge, pages
without local exploration, missing requested content, return-home repair loops,
title-only narration, unsupported claims, and insufficient scene dwell.

Editorial QA (preflight in PLANNING; full inspect in NARRATION/VIDEO_QA) rejects
missing welcome copy, repetitive skeleton narration, generic route labels,
unsupported scene claims, and similar ungrounded prose.

Visual QA samples rendered frames and trace evidence for composition, readable
text, source-site fidelity (structural similarity to browser footage), smooth
scrolling, cursor alignment, camera bounds, black/frozen frames, and caption
contrast/timing.

Synchronization QA checks scene IDs and timestamps across trace events,
storyboard scenes, script lines, captions, cursor actions, audio segments, and
render composition.

Delivery QA requires a complete manifest, valid MP4, non-empty duration,
expected frame rate, acceptable bitrate, final QA reports, multimodal review
when mandatory, and no unresolved hard failures.

Repairs are classified and limited to the owning layer
(`backend/app/quality/repair.py`):

```text
discovery / auth / Stagehand behavior → DISCOVERY (or needs_input)
rehearsal / workflow / certified outcome → PLANNING
trace / execution outcomes → EXECUTION
editorial / narration / caption craft / sync-EDL → NARRATION (+ refresh_editorial)
render / video encode → RENDER
multimodal unavailable → VIDEO_QA
```

Child runs clone only predecessor evidence; stale final verdicts are not copied
forward except as appropriate for VIDEO_QA-boundary retries.

### 14. Persistence and artifacts

Every run has a run-local artifact root. The normal structure is:

```text
<artifact-root>/<run-id>/
├── objective.json
├── plan.json
├── discovery/
│   ├── product-context.json
│   ├── product-knowledge.json
│   ├── capabilities.json
│   ├── behavioral-product-model.json
│   ├── relevance-graph.json
│   ├── stagehand-observation.json
│   ├── viewport-decision.json
│   ├── browserbase-session.json
│   ├── mutation-authorization.json
│   ├── rehearsal-attempt.json
│   ├── rehearsal-report.json
│   └── page-knowledge/ (also mirrored at repo root page-knowledge/)
├── planning/
│   ├── demo-brief.json
│   ├── certified-workflow-graph.json
│   ├── certified-demo-script.json
│   ├── validated-state-graph.json
│   └── capability-resolutions.json
├── execution/
│   ├── trace.json
│   ├── interaction-trace.json
│   ├── browser-recording.mp4
│   ├── browserbase-session.json
│   ├── action-attempts.json
│   ├── state-snapshots.json
│   ├── verification-results.json
│   └── cloud-screencast-frames/   # cloud CDP capture
├── presentation/
│   ├── storyboard.json
│   ├── editorial-brief.json
│   ├── scene-plan.json
│   ├── validated-scene-plan.json
│   ├── presentation-plan.json
│   ├── sync-edl.json
│   ├── semantic-moments.json
│   ├── actual-flow-storyboard.json
│   ├── captions.json
│   └── remotion-props.json
├── narration/
│   ├── fact-extraction.json
│   └── editorial-script.json
├── qa/
│   ├── editorial-preflight.json
│   ├── editorial-report.json
│   ├── coverage-report.json
│   ├── outcome-report.json
│   ├── story-report.json
│   ├── video-report.json
│   ├── presentation-report.json
│   ├── synchronization-report.json
│   ├── multimodal-report.json
│   ├── delivery-report.json
│   ├── gap-report.json
│   ├── completion-audit.json
│   ├── repair-decision.json
│   └── repair-scheduled.json
├── render/
│   ├── status.json
│   ├── segments/
│   └── demo.candidate.mp4
├── repair/                 # child lineage when applicable
└── final/
    ├── demo.mp4
    └── poster.jpg
```

Database records point to these artifacts and preserve run lineage, stage
transitions, provider attempts, retry boundaries, and knowledge versions. A
trace and recording from different runs are rejected.

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

The local default is SQLite with WAL mode (`backend/artifacts/productlens.sqlite3`
by default; override with `PRODUCTLENS_DATABASE` or
`PRODUCTLENS_DATABASE_URL`). Production deployments should use PostgreSQL and
run Alembic migrations. The database stores users, projects, requests, runs,
stage jobs, artifacts, provider calls, delivery leases, knowledge versions,
heartbeats, and retry lineage.

Local development: Terminal 1 runs `python -m app.workers.local` (atomic outbox
claims, expired-claim recovery; never in-process FastAPI background tasks).
Terminal 2 runs the API. Deployment: set `PRODUCTLENS_WORKER_MODE=dramatiq` and
`PRODUCTLENS_BROKER_URL=amqp://...`, then `dramatiq app.workers.tasks`. Provider
concurrency limits are controlled independently for Browserbase, Stagehand,
OpenRouter, and workers. Clients should consume the run event stream rather than
repeatedly polling stage status.

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

For credentials, use an opaque reference such as `secret://productlens/demo` in
the generation request. Configure matching **worker process environment
variables** (never request body, prompts, or checked-in files):

```text
PRODUCTLENS_CREDENTIAL_DEMO_USERNAME=...
PRODUCTLENS_CREDENTIAL_DEMO_PASSWORD=...
```

CAPTCHA, OTP, ambiguous login controls, and unsuccessful login are explicit
failures; ProductLens never fakes authentication.

### 20. Local setup

```powershell
cd "ProductLensAI 2.0/backend"
python -m pip install -e ".[dev]"
python -m playwright install chromium
$env:PRODUCTLENS_ARTIFACT_ROOT = "artifacts"
alembic upgrade head
python -m app.workers.local
```

In another terminal:

```powershell
cd "ProductLensAI 2.0/backend"
uvicorn app.api.main:app --reload --port 8000
```

The default artifact root is `backend/artifacts`. Runtime artifacts, recordings, databases, samples, and test HTMLs are ignored by Git. The legacy `ProductLens AI/` project is reference material only; this application does not read its runtime configuration.

#### Deployment-like local dependencies

```powershell
docker compose -f backend/infra/docker-compose.yml up -d
```

Local endpoints for deployment-readiness checks:

- PostgreSQL: `postgresql://productlens:productlens_dev_only@localhost:55432/productlens`
- RabbitMQ: `amqp://guest:guest@localhost:5672/`
- S3-compatible API: `http://localhost:59000` (create the bucket before publishing)

These credentials are development-only. Set `PRODUCTLENS_DATABASE_URL`,
`PRODUCTLENS_BROKER_URL`, and S3-compatible storage vars, then run migrations
and readiness once before scaling API/worker processes:

```powershell
cd "ProductLensAI 2.0/backend"
python -m app.operations.startup --require-ready
```

That command runs `alembic upgrade head`, verifies `SELECT 1`, declares a
RabbitMQ queue, and checks object-store bucket access without writing user
artifacts. Start supervised replicas afterwards with `backend/scripts/start-api.ps1`
and `backend/scripts/start-worker.ps1`. The worker defaults to one
process/thread so browser and rendering capacity stay under the supervisor;
scale replicas only after provider quotas and DB connection limits are set.

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

For a fixture gate (no OpenRouter / Browserbase / ElevenLabs required):

```powershell
python -m app.benchmark.run_gate 3 --render
python -m app.evaluation.run_benchmark --attempts 3 --artifact-root artifacts/benchmark
```

**Golden fixtures** live in `backend/validation/golden/`. They are provider-neutral
regression contracts for execution and presentation (semantic navigation,
scroll witnesses, geometry, native scale, verified outcomes). Rendered golden
media is generated in CI from the trace; binary artifacts are not committed.

#### Re-render retained evidence

After a presentation-only change, rebuild an existing verified delivery without
opening a browser or calling any provider:

```powershell
python -m app.video.rerender --artifact-root artifacts/<collection> --run-id <run-id> --verify
```

Use `--verify-only` for provider-free delivery checks on an existing MP4.
Rendering writes a candidate first and atomically replaces `final/demo.mp4`
only after a non-empty result.

#### Provider activation order

After credentials and account credit are available:

1. `GET /readiness` reports OpenRouter, ElevenLabs, Browserbase, and Stagehand
   without returning secrets.
2. Install the Stagehand bridge once: `cd backend/stagehand; npm install`.
3. Non-destructive URL run with `render: false`; inspect grounded plan/trace.
4. Repeat with `render: true`; confirm captions stay trace-derived.
5. Enable `cloud_discovery: true` only after the local path is proven for that app.
6. Stagehand runs automatically when configured; observations stay advisory until
   Playwright re-grounds them. `stagehand_assist` is not required to unlock it.

#### Funded smoke-test checklist

Keep these out of ordinary regression; run once when credits are available and
retain run ids:

1. **OpenRouter** — plan-only URL run (`render: false`).
2. **Browserbase** — same with `cloud_discovery: true`; session record `CLOSED`.
3. **Stagehand** — `discovery/stagehand-observation.json` present; only re-grounded
   candidates affect the plan.
4. **ElevenLabs** — `PRODUCTLENS_CAPTION_ONLY=false` on one fixture; sync holds.

#### Public acceptance batch

```powershell
python -m scripts.run_public_acceptance --cloud --render --limit 8
python -m scripts.audit_public_acceptance
```

Inventory: `backend/validation/public-targets.json`. Results:
`artifacts/acceptance/public-runs.json`.

For a provider-backed URL, run discovery/plan first with `render: false`, inspect
the persisted plan and trace, and only then render. Use the SSE endpoint for
progress instead of an aggressive polling loop.

Consecutive MVP acceptance for locked scenarios (Lead / Booking / Excalidraw)
lives in `backend/scripts/run_mvp_consecutive.py` with ledger
`backend/validation/mvp-acceptance-ledger.json`. Prefer watching the runner
terminal: AUTH/`Page.goto` loops burn Browserbase before EXECUTION; Delivery QA
failures after RENDER are not fixed by blindly starting another full discovery.

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

---

## Architecture assessment (implementation audit)

This section describes the code that exists in this checkout. It is an
engineering audit of ownership and coupling, not proof that every boundary in
the pipeline narrative above is already perfectly enforced in every module.

### Composition root and stage coordination

The backend is a FastAPI application backed by SQLAlchemy. API handlers create
or load a run and hand durable work to `services.jobs` and the local/Dramatiq
worker layer. `services.runtime.build_job_service` is the composition root: it
constructs the repository, provider adapters, planner, generator, and artifact
storage.

Generation is coordinated by stage mixins on `UrlGenerationService`, driven by
`DemoJobService.run_url` / `run_url_stage`. Pure policy lives in
`services.generation_policy`; ProductContext → ProductKnowledge materialization
lives in `services.knowledge`; render-stage orchestration lives in
`services.render_stage`. The frontend is a Next.js API client; it does not
execute browser actions.

### Complexity hotspots

| Area | Issue | Desired boundary |
|---|---|---|
| Generation coordinator / stage mixins | Still large stage algorithms (discover, rehearse, execute, narrate, qa) | Thin coordinator over collaborators that own browser lifecycle, persistence, QA aggregation |
| `discovery/live.py` | Capture + exploration policy mixed | Split extraction vs policy; keep one `LiveDiscovery` facade |
| `planning/candidates.py` | Proposal types + scoring + legacy fallbacks | Evidence → candidate pipeline with explicit validation |
| `planning/production*` | Grounding, routes, objective validation overlap | Navigation compile consumes validated steps only |
| `presentation/editorial/*` | Draft, enrich, captions, validation co-located | Separately callable facts / writer / validator |
| `execution/playwright_adapter.py` + `engine.py` | Adapter breadth vs engine lifecycle | Explicit target resolution + verification; no LLM in primitives |
| `persistence/repository.py` | Broad facade over many tables | Group by bounded context; centralize transactions |
| `video/render.py` | Probe, cut policy, Remotion process mixed | Media mechanics vs composition policy |
| Frontend routes / `api.ts` | Dense one-line modules | Typed API + feature components; no backend contract change |

### Cross-cutting findings

- `contracts` is the correct shared schema boundary.
- Backend modules import cleanly (no observed import cycle from the audit).
- `Settings` uses typed, bounded env parsers; malformed values are unit-tested.
- Strict mypy covers a deliberate module set; full-source mypy still has legacy
  errors outside that set (explicit follow-up).
- Ruff is expected clean for backend source/tests after formatting passes.
- Runtime artifacts and the local DB stay out of source control.

### Simplification priorities

1. Keep the validation baseline repeatable (tests, compile, strict mypy, frontend build).
2. Continue extracting real collaborators from the generation coordinator (not forwarding wrappers).
3. Split discovery capture from exploration policy.
4. Split editorial facts, writing, and validation; keep evidence/scene IDs as truth.
5. Clarify adapter vs engine ownership for resolution and verification.
6. Group repository ops by bounded context before public API changes.
7. Separate render media mechanics from cut/composition policy.
8. Readable frontend modules after backend contracts stabilize.

Success criterion: a senior engineer can name ownership, inputs, outputs, and
failure behavior without tracing unrelated concerns. Line-count reduction alone
is not success.

### Explicit non-goals

Do not add website-specific recipes, relax evidence gates, replace deterministic
browser capabilities with LLM guesses, or claim universal canvas/remote-desktop
automation as a refactor outcome.

---

## Refactor review (engineering quality)

### Confirmed architecture invariants

```text
API request
  → objective normalization
  → evidence discovery (+ Stagehand enrichment)
  → knowledge / relevance graph
  → candidate workflow + production plan (+ rehearsal when authorized)
  → browser execution → DemoTrace
  → journey / script / captions
  → Remotion/FFmpeg render
  → layered QA → delivery or targeted repair
```

Contracts in `app.contracts` are the cross-layer data boundary. Browser
primitives do not write narration; narration does not drive the browser;
rendering consumes a trace/script rather than deciding product intent.

### Cleanup already landed (historical)

- Centralized URL/timing/render-error policy in `generation_policy.py`.
- Single ProductKnowledge materializer in `knowledge.py`.
- Render-stage orchestration in `render_stage.py`.
- DDL isolated in `persistence/schema.py`; repository remains a compatibility facade.
- Typed bounded settings parsers; stricter typing on persistence/workers/quality/
  presentation/security/provider boundaries.
- SQLite claim resilience under lock contention (CAS invariant preserved).
- Formatting/readability cleanup without public route changes.

### Validation baseline (as of the recorded cleanup pass)

Numbers drift; re-run locally before treating them as current:

- Strict mypy on the designated module set
- Ruff clean for backend source/tests
- `python -m compileall`
- Deterministic pytest suite (`-m "not integration"`)
- Concurrency claim acceptance
- Frontend production build

Live Browserbase / Stagehand / OpenRouter / media-tool proof remains an
environmental acceptance concern — the local suite does not replace a funded
smoke run.

### Decision stance

Further work should be focused extractions of the hotspots above, each with
focused tests plus the same complete validation baseline. Do not split modules
into empty forwarding wrappers solely to reduce line counts.
