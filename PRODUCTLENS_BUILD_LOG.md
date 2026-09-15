# ProductLens Build Log

**Last Generated:** 2026-09-14
**Repository State:** Git state unavailable from this workspace; no commit is asserted.  
**Git History:** Not available / unreliable.  
**Primary Source:** Current repository, configuration shape, tests, migrations, and retained artifacts.  
**Historical Completeness:** Partial — external development history is not represented here.

This document is intended as a factual development record, not marketing copy. Where information cannot be established from the repository, it is explicitly marked as unknown.

## 1. Executive Summary

ProductLens is an automated web-application demo generator. Its intended job is to accept a URL and natural-language objective, discover relevant product evidence, select an explainable workflow, execute it in a browser, and render a caption-led (optionally narrated) video.

The repository contains a serious Python backend: typed contracts, exploration, evidence-aware planning, Playwright/Browserbase execution, OpenRouter integration, Remotion rendering, quality gates, persistence, migrations, workers, and a frontend. It can run deterministic fixtures locally and has produced browser/video artifacts. It is **not proven as an MVP for unfamiliar live applications**: recent retained Browserbase attempts were interrupted by session closure, and the latest clean-session attempt received Browserbase HTTP 402 before capture. Final accepted 2–3 minute live walkthroughs for the Portfolio and Study Plan targets do not exist in the retained evidence.

The long-term reliability-first architecture is partially implemented; documentation and plans describe a larger completed system than live acceptance evidence currently supports.

## 2. Product Vision

Repository-supported vision: a user supplies a URL plus a walkthrough/workflow request. `ObjectiveSpec`, `ProductContext`, `PageKnowledge`, `FeatureKnowledge`, `CandidateDemoFlow`, `DemoPlan`, `DemoTrace`, `EditorialStoryboard`, and quality reports in `backend/src/productlens/contracts/models.py` model an evidence-grounded pipeline.

Inputs: URL, objective, audience, duration, safe-side-effect policy, optional credential reference, and cloud/Stagehand options (API request models in `api/main.py`). Expected behavior: discover only relevant pages, prefer visible navigation, produce a human-paced story, and persist artifacts. Expected output: final MP4, captions, trace, plans, and QA reports.

The ambition to work with *unfamiliar reasonably standard applications with minimal intervention* is stated by plans and contracts, but is not yet demonstrated by accepted live runs. Any broader ambitions outside repository files are unverified.

## 3. MVP Definition

Working MVP: a user can provide an unfamiliar standard application and natural-language walkthrough/workflow request and receive a genuinely good demo with minimal/no manual intervention.

| Capability | Status | Evidence | Remaining Problems |
|---|---|---|---|
| URL/objective API | Mostly working | `api/main.py`, API tests, frontend `create/page.tsx` | Live auth/UI path not audited as an end-user flow; the create page fixes `max_pages` to 4. |
| Targeted discovery | Partially working | `discovery/live.py`, discovery tests, retained Browserbase discovery artifacts | Safe non-navigation interaction probing is limited. |
| Evidence-based planning | Mostly working | `planning/candidates.py`, `planning/production.py`, tests | General story quality remains unproven live. |
| Semantic browser execution | Partially working | `execution/engine.py`, `playwright_adapter.py` | Remote duration/session constraints and target closure. |
| Local fixture rendering | Mostly working | `video/render.py`, integration tests | Passing fixture renders are intentionally not proof of showcase quality. |
| Browserbase native source recording | Experimental / partially working | `providers/browserbase.py`; retained native MP4 artifacts | Capture success is blocked by provider credit/session limits. |
| Caption-led output | Partially working | `narration/`, `presentation/`, renderer | No accepted live final validates quality/sync end-to-end. |
| ElevenLabs narration | Blocked / optional | provider and narration code | Credentials/credits unavailable; no live proof. |
| Durable operations | Mostly working | Alembic migrations, repository, jobs/workers, startup checks | Production deployment/retention at scale unproven. |

## 4. Current Architecture

| Component | Responsibility / files | Status |
|---|---|---|
| Frontend | Next 15/React 19 app in `frontend/app/`, shared studio shell and local-storage auth session in `components/` / `services/api.ts` | Present and wired to API-shaped routes, but not accepted as a complete production UX. The create page currently submits a fixed `max_pages: 4`, which conflicts with the backend's adaptive/full-walkthrough ambition. |
| API | FastAPI routes/models in `backend/src/productlens/api/main.py` | Implemented. |
| Contracts | Pydantic models in `contracts/models.py` | Central and actively used. |
| Discovery | `discovery/live.py` gathers DOM text, headings, cards, controls, screenshots, page knowledge | Implemented; limited safe interaction probing. |
| Planning | `planning/candidates.py`, `production.py` score/compile evidence-backed flows | Implemented with deterministic fallbacks. |
| Execution | `execution/engine.py`, `playwright_adapter.py` | Implemented with semantic locators, state checks, retries and trace capture. |
| Providers | `providers/openrouter.py`, `browserbase.py`, `stagehand.py`, `elevenlabs.py` | Replaceable adapters; live availability varies. |
| Presentation/video | `presentation/`, `video/render.py`, Remotion project/assets | Implemented, quality gated but not live-accepted. |
| QA/repair | `quality/`, `evaluation/`, repair classification | Implemented deterministic gates and reports. |
| Persistence/ops | `persistence/`, `storage/`, `operations/`, `workers/`, Alembic | PostgreSQL-capable with SQLite development fallback. |

Configuration is read from the new project `.env` only (`config/settings.py`). Provider secrets are intended to remain environment references, not artifacts/prompts.

## 5. End-to-End ProductLens Pipeline

`request → ObjectiveSpec → discovery → Product/Page/Feature knowledge → candidate selection → DemoPlan/storyboard → clean execution → DemoTrace → captions/narration → Remotion render → execution/editorial/visual/sync/delivery QA → targeted repair`

| Stage | Implementation / output | Known failure modes |
|---|---|---|
| Request | FastAPI + jobs payload | Auth/deployment not accepted as a full user journey. |
| Discovery | `UrlGenerationService.discover_stage`; `discovery/` artifacts | Cloud availability; incomplete interactive-state discovery. |
| Planning | `plan_stage`, `candidate-flows.json`, `plan.json`, storyboard | Generic evidence can still select weak landmarks. |
| Execution | `execute_stage`, `execution/trace.json` | Browserbase target closure, 402 credit requirement, locator/state failure. |
| Narration | `narration_stage`, captions/script | Caption quality not accepted against live samples; TTS optional. |
| Render | `render_stage`, Remotion final MP4 | Requires complete trace/source; fixture render can fail delivery QA. |
| QA/repair | `qa_stage`, `quality/`, repair decision | Quality gates exist; complete live acceptance is missing. |

The current proven breakpoints are cloud production availability and a complete, accepted live trace. Browserbase failures are now persisted as execution reports where possible.

## 6. Browser Automation

Playwright is the execution owner. `PlaywrightAdapter` resolves semantic locators from test IDs, roles, labels, text and selectors; it deliberately avoids coordinate clicks. `ExecutionEngine` records before/after snapshots, target rectangles, viewport/scroll, postconditions, retries before side effects, and event timing.

Browserbase creates separate discovery/production sessions (`providers/browserbase.py`, `services/generation.py`). Cloud capture now requests Browserbase native MP4 download; CDP screencast code in `browser/screencast.py` remains a diagnostic fallback because sparse change-only frames caused accelerated-looking recordings. Cloud scrolling uses browser-side eased scrolling to reduce remote wheel RPCs.

Architectural problems: general interaction discovery is weaker than navigation discovery; narrated scene quality is not proven; remote latency matters. Testing/service limitations: retained cloud sessions closed around five minutes and later returned HTTP 402. A paid plan may remove a quota/credit constraint, but it does **not** by itself prove locator reliability, planning quality, or visual quality.

## 7. AI / LLM System

OpenRouter is configured in `providers/openrouter.py` and runtime composition. Planning uses structured outputs; `planning/production.py` and `presentation/editorial.py` use evidence-grounded prompts/validation and deterministic fallback. Retained live logs show `google/gemini-2.5-flash` responses; this is configuration/runtime evidence, not a quality guarantee.

The LLM is used for fact/story enrichment, not for unbounded browser authority. Application code rejects unsupported/generic narration in editorial QA and retains evidence references. Deterministic code handles URL canonicalization, candidate scope, locator execution, quality checks and repair classification. Cost policy/model selection is configuration-driven; exact spend is not recoverable from the repository.

## 8. Video Generation System

Raw local browser video or Browserbase native MP4 is preserved in `execution/`. `presentation/scenes.py`, `journey.py`, `viewport.py`, and cursor/camera plans direct Remotion layers. `video/render.py` invokes the Remotion composition; quality modules inspect duration, frame pacing, black/frozen frames, structural similarity, composition, captions and synchronization.

What looks technically promising: native Browserbase captures sampled from retained artifacts are full 1920×1080 30fps source footage. What is not proven: a final edited live walkthrough matching `samples/` in narrative, pacing, cursor, zoom, and content coverage. Prior CDP-derived cloud footage was sparse/short; it is no longer the intended primary source.

## 9. Test Websites and Test Scenarios

`productlens-test-htmls/README.md` documents HTML gates/fixtures. Backend tests cover discovery, candidate scope, page completion, adapters, providers, delivery QA, video quality, operations, API, migration/worker behavior, and integration rendering. `samples/` contains reference videos/readme material; it is a quality benchmark, not copied content.

Live targets retained in run artifacts include the Portfolio (`dhruv-sys.vercel.app`) and Study Plan (`study-plan-red.vercel.app`). Portfolio discovery found Home, Timeline, System Designs, Engineering Notes, Contact. Study Plan discovery found dashboard/Today, weeks/detail, DSA, builds and progress. Neither has a complete accepted final run.

| Scenario | Location | What it establishes | Current limitation |
|---|---|---|---|
| Browser primitives | `productlens-test-htmls/gate1-primitives/` | Sixteen basic actions, including masked fields, modal, async wait, navigation and real scroll | Controlled DOM with stable test IDs; not evidence for arbitrary sites. |
| Dependent CRM workflow | `gate2-workflow/crm.html` | Create-lead then create-booking relationship | Synthetic data and deterministic controls. |
| Trace transitions | `gate3-trace/state-transitions.html` | Before/after state capture and observable outcomes | Does not exercise a remote browser. |
| Presentation edge cases | `gate4-presentation/dashboard.html` | Small/off-screen/transient targets and subjective camera review | Its own README says visual acceptance still needs eyeballing. |
| Targeted discovery | `gate5-discovery/hub.html` and linked pages | Relevant-route selection without broad crawling | Narrow fixture topology. |
| Golden E2E | `gate6-e2e/` | Login, state persistence, lead/booking workflow and verification | Fake CAPTCHA only; no real third-party authentication. |

Backend tests are organised as individual `test_*.py` files rather than suites in subdirectories. They cover contracts, API/auth, repository/jobs, providers, discovery, planning, execution, presentation, narration/TTS, rendering, multimodal and delivery QA, repair, benchmark gates, and startup/migrations. These tests are strong regression evidence for explicit contracts; they do not establish live-site generalization or subjective sample-video quality.

Missing/weak scenarios include broad real-world authentication/CAPTCHA, diverse dynamic apps, paid Browserbase acceptance, ElevenLabs sync, and statistically meaningful reliability benchmarks.

## 10. Implementation History That Can Be Reconstructed

Historical context is unavailable from repository beyond the following code-level evolution. There is no trustworthy commit chronology or date for each change.

| Previous/limited approach visible in code | Replacement/current approach | Evidence and result |
|---|---|---|
| Route/navigation coverage could be treated as a tour. | Candidate scoring, page contracts and scene completion phases require an establish/explore/explain/demonstrate/verify narrative role. | `planning/candidates.py`, `planning/production.py`, `presentation/journey.py`, `test_journey.py`. This is implemented; live quality remains unaccepted. |
| Sparse CDP screencast frames were usable as cloud video input. | Browserbase native MP4 is requested, asynchronously assembled, downloaded and preferred; screencast is diagnostic fallback. | `providers/browserbase.py`, `services/generation.py`, `browser/screencast.py`. Native source files exist for interrupted runs, but not a final accepted edit. |
| One monolithic URL-generation path existed. | Stage-specific jobs and durable ledger/lease migrations orchestrate discovery through QA/repair. | `services/jobs.py`, `workers/`, migrations `03` and `04`. Local and test behavior exists; scale behavior is unproven. |
| Earlier editorial fallback used over-broad label interpretation. | Grounded structured editorial pass plus deterministic evidence fallback and rejection rules. | comments and code in `presentation/editorial.py`, related tests. It reduces a known failure mode but does not prove prose quality on unfamiliar sites. |

Repository evidence shows migrations from initial reliability schema through project scoping, stage ledger/leases, authentication, and run artifact documents (`alembic/versions/`). Current code retains compatibility bridges and comments for older shallow discovery/route behavior. Historical chronology, dates, and motivations beyond those files are unavailable.

## 11. Major Failures and Dead Ends

### Failure: sparse remote compositor footage produced misleading pacing

**Initial approach:** treat CDP screencast frames from a Browserbase session as the render source.  
**Why it failed:** remote frames can be sparse/change-driven rather than a continuous video clock, so a long browser session can become short, accelerated-looking footage after rendering.  
**Evidence:** explicit design comments in `browser/screencast.py`, source-selection logic in `services/generation.py`, and the native-recording provider.  
**Replacement:** Browserbase native MP4 is primary; screenshots/screencast remain diagnostic evidence.  
**Status:** partially solved architecturally; final live render acceptance is still missing.  
**Lesson:** control telemetry and a presentation-grade video source are different artifacts.

### Failure: route sweep did not constitute a walkthrough

**Initial approach:** use navigation/route coverage as a proxy for a complete demonstration.  
**Why it failed:** opening a page without local reading, inspection, interaction or a viewer takeaway creates a tab tour rather than a coherent story.  
**Evidence:** page-completion contracts, fixed portfolio ordering and tests in `test_candidates.py`, `test_evidence_planner.py`, `test_journey.py`, `test_story_qa.py`.  
**Replacement:** semantic candidate flows, per-page scene phases, evidence-linked narration and editorial hard failures.  
**Status:** implementation present; human-quality validation remains unresolved.

### Failure: cloud production capture did not complete reliably

**Initial approach:** execute long live plans inside Browserbase and accept a session as sufficient infrastructure.  
**Why it failed:** retained runs show mid-run target/session closure; a later session creation returned HTTP 402. Exact provider-side cause for the closures is not recoverable solely from code.  
**Evidence:** retained run artifacts and `services/generation.py` failure-report persistence.  
**Replacement:** persist partial trace/execution QA, reduce redundant scene steps, use a clean production context, and separate targeted repair boundaries.  
**Status:** unresolved until a funded clean Browserbase acceptance run completes. A paid plan is only a testable hypothesis for quota-related failures.

### Failure: route-only / repeated landmark tours
**Evidence:** `planning/candidates.py`, regression tests.  
**Why it failed:** opening a route or repeatedly scrolling a heading does not prove page understanding.  
**Replacement:** page knowledge, candidate scoring, visible-navigation preference, page-completion contract.  
**Status:** partially solved; live quality unproven.

### Failure: sparse CDP screencast as cloud source
**Evidence:** `browser/screencast.py` comments and retained cloud frame artifacts.  
**Why it failed:** CDP emitted sparse compositor-change frames during holds, yielding shortened/accelerated source.  
**Replacement:** Browserbase native recording download in `providers/browserbase.py`.  
**Status:** source path proven on retained recordings; complete run blocked.

### Failure: Browserbase production interruptions
**Evidence:** retained `PRODUCTION_CAPTURE_INTERRUPTED` trace/errors and current provider code.  
**Why it failed:** target/context closed during remote production; latest session creation returned 402.  
**Replacement:** durable interruption reports, native recording artifacts, lower-RPC page contract.  
**Status:** unresolved live acceptance blocker.

## 12. Recent Advancements

- **Evidence-first contracts:** the shared Pydantic contracts now express objectives, page/feature knowledge, candidate flows, workflow steps, scene plans, trace evidence, narration and repair decisions. This makes cross-layer data inspectable rather than relying on prompt text alone.
- **Page-local story contracts:** planning adds explicit scene phases and prevents return-home repair coverage in the portfolio recipe. This is a substantive response to the route-tour failure, not a visual guarantee.
- **Provider boundaries:** Browserbase, Stagehand, OpenRouter and ElevenLabs are adapters instantiated by runtime composition; their outputs are re-grounded or validated rather than allowed to own workflow truth.
- **Durable lineage:** migrations and repository models cover users, projects, runs, artifact documents, provider attempts and staged jobs/leases. SQLite is still a valid local default; PostgreSQL is supported rather than assumed active everywhere.
- **Targeted repair/rerender:** presentation-only changes can rerender verified evidence without reopening a browser (`video/rerender.py`). Failure classification aims to prevent wasteful full reruns.

Evidence-backed planning, canonical URLs, duplicate-opening suppression, page-local knowledge, native Browserbase MP4 export, provider-failure persistence, viewport probing, source-fidelity QA, and contract-aware cloud capture are represented in the current code/tests. They matter because they prevent a rendered MP4 from being mistaken for a successful demo.

## 13. Current Reliability

| Area | Assessment | Failure modes | Biggest needed improvement |
|---|---|---|---|
| Request/API | Mostly working in code/tests | Auth/deployment/UI journey not accepted end-to-end | Real multi-user smoke test. |
| Objective understanding | Partially working | Natural-language ambiguity and model dependence | Broader objective fixtures and human rubric. |
| Discovery | Partially working | Incomplete dynamic/interactive-state knowledge; cloud dependence | Safe representative interaction probing. |
| Planning | Mostly working structurally | Weak evidence can still make a weak story | Real-site candidate-flow benchmark. |
| Navigation/targeting | Partially working | Ambiguous controls, late render, stale state | More real app traces and selector resilience tests. |
| Waiting/readiness | Partially working | Long-polling apps and remote latency | Per-site/per-state readiness validation. |
| Capture | Experimental cloud; mostly working local fixtures | Session close, 402, recording assembly | Funded repeated cloud validation. |
| Editorial/narration | Partially working | Generic/unsupported prose despite guardrails | Manual and automated acceptance corpus. |
| Rendering | Mostly working for known traces | Source format/timing/quality mismatch | Final source-faithful live renders. |
| Delivery/repair | Mostly working in code | Cannot repair missing/invalid live evidence | Exercise repair on real failures. |

| Area | Assessment | Biggest improvement needed |
|---|---|---|
| Input/API | Mostly working | End-user frontend/API acceptance. |
| Understanding/planning | Partially working | More real-app benchmarks and interaction probes. |
| Navigation/targeting | Partially working | Live cross-site evidence. |
| Capture | Blocked for final validation | Browserbase credit/session availability. |
| Rendering | Mostly working on fixtures | Live source-to-final acceptance. |
| Narration/TTS | Caption path partial; TTS blocked | Real audio timing/quality validation. |

## 14. Current Video Quality Assessment

**What already works visually:** composition layers, scene/camera/cursor/caption contracts and deterministic checks exist; retained native Browserbase source is 1920x1080 at 30fps and appears source-faithful when sampled. Fixture-oriented rendering and caption-only delivery are implemented.

**What is weak or unproven:** no retained final output demonstrates sample-level editorial depth, smoothness, page-local exploration, caption copy, natural cursor timing, restrained target zoom, or complete 2–3 minute pacing. Earlier sparse cloud footage is specifically not proof of quality.

**What would make it showcase quality:** a complete validated flow must use continuous source capture, retain site animations/transitions, show each page long enough to establish and explore it, explain visible evidence rather than route labels, and pass frame/caption/cursor/manual review together.

**Biggest visual risk:** a technically successful browser trace can still create a lifeless route tour or use inappropriate source timing. The highest-impact validation is one complete, independently reviewed Browserbase production run per target after provider access is restored.

Native source framing and resolution are materially better than prior sparse CDP output. Current risks are weak editorial specificity, page-local exploration, timing, and final caption/cursor/camera coherence. Showcase quality requires complete source-faithful production traces, reviewed final renders, human-like story coverage, and accepted QA—not merely smooth source footage.

## 15. Current MVP Readiness

| Requirement | Status | Biggest Blocker |
|---|---|---|
| Understand unfamiliar app/objective | Partially proven | Limited live diversity. |
| Complete relevant workflow | Partially proven | Interactive-state discovery and cloud execution. |
| Good automatic video | Not proven | No accepted live output. |
| Minimal intervention | Not proven | Provider availability and repair loop. |

Already proven: typed pipeline, fixture execution/rendering, durable artifacts, discovery evidence, and native recording retrieval. Partially proven: live discovery and partial production. Not proven: final MVP promise.

## 16. Biggest Current Risks

1. **Browserbase availability/credits** — critical; current 402 prevents new validation.  
2. **Remote execution duration** — high; prior sessions closed mid-plan.  
3. **Generalization** — high; two targets do not establish unfamiliar-app reliability.  
4. **Editorial quality** — high; final sample-level story/pacing is unproven.  
5. **Interactive state discovery** — high; navigation is stronger than safe deep interaction probing.  
6. **TTS synchronization** — medium; implemented but not live validated.  
7. **Operational complexity** — medium; database/broker/storage stack exists but production load is unproven.

## 17. External Services and Dependencies

| Dependency | Purpose and code boundary | MVP role / fallback | Limitation evidenced now |
|---|---|---|---|
| OpenRouter | Structured planning and editorial enrichment via `providers/openrouter.py` | Important for AI-assisted quality; deterministic, evidence-grounded fallbacks exist | Exact model/cost and quality vary by runtime configuration; no repository-wide spend record. |
| Browserbase | Cloud browser sessions, CDP connection and native MP4 via `providers/browserbase.py` | Required for cloud validation, not for local fixtures | Retained session closure and HTTP 402 block clean acceptance. |
| Stagehand | Optional Node observation bridge, never execution authority | Optional; Playwright re-grounds suggestions | Bridge/model installation and cloud observation are unproven as a quality improvement. |
| ElevenLabs | Optional audio synthesis provider | Not required for caption-only MVP validation | No configured/accepted live TTS output. |
| Playwright | Local/browser CDP control, DOM evidence and semantic actions | Core execution dependency | Cross-site interaction reliability remains unproven. |
| Remotion and FFmpeg | Layered composition and encode/transcode | Core final-video dependency | Works for known traces; final live story quality remains unproven. |
| PostgreSQL / SQLite | Run, artifact, job and user persistence | SQLite local default; PostgreSQL deployment dialect | Production migration/operations are not a scale proof. |
| RabbitMQ / Dramatiq | Deployment worker broker | Local durable polling/outbox path exists | Distributed-worker behavior is not live load tested. |
| Local / S3-compatible storage | Artifact persistence through `storage/` | Required for durable output | Lifecycle/retention and external storage reliability are unproven. |

Secrets are read from process environment (`config/settings.py` and backend README); provider references rather than key values are intended to be persisted. This document intentionally does not record credentials.

## 18. Important Technical Decisions

- Semantic locators over coordinate actions: reliability/cursor trade-off documented in `playwright_adapter.py`.
- Separate discovery and clean production contexts: prevents exploratory actions contaminating delivery capture.
- Evidence-linked contracts and QA: plans/artifacts are treated as insufficient without trace/visual checks.
- Native Browserbase recording over CDP as primary cloud source: avoids sparse capture pacing.
- Caption-first TTS-optional path: provider absence does not stop silent demo work.
- Provider-neutral interfaces and environment secret references: avoid hard-coding external service control into workflow truth.
- SQLite is deliberately retained for zero-configuration local work while schema migrations and SQL dialect support target PostgreSQL deployment. The trade-off is a simpler developer path without claiming SQLite is the production topology.
- Stagehand is observation-only, and every suggestion must be re-grounded in the Playwright page. This trades some agent autonomy for auditability and avoids treating an LLM-generated selector as proof.
- Retry before dispatch, never replay a dispatched side effect. The system prefers an explicit failed outcome over duplicate form submission or unsafe external action.

## 19. Engineering Lessons

- A successful click is not evidence of a good demo; page-local visible proof matters.
- Remote browser command latency can dominate a video workflow even when rendering is fast.
- Native browser recording and browser-control telemetry have different purposes; one cannot safely substitute for the other.
- Strict QA that rejects short fixture renders is valuable because it exposes false success early.

## 20. Potential Content Stories

### Native recording versus CDP frames
**Hook:** “Why our cloud demo videos looked fast even when the browser run took minutes.”  
**Facts/Evidence:** `browser/screencast.py`, Browserbase native-download provider, retained artifacts.  
**Lesson:** capture transport semantics affect editing quality.

### The route-tour trap
**Hook:** “Opening every tab is not a product walkthrough.”  
**Facts/Evidence:** candidate/page-completion logic and tests.  
**Lesson:** workflow coverage needs visible evidence and narrative role.

### Latency as an architectural input
**Hook:** “A five-stage story contract should not mean five identical remote scroll calls.”  
**Facts/Evidence:** contract-aware phases in models/planner and cloud execution comments.  
**Lesson:** preserve semantics while reducing transport work.

### A test suite can prove contracts without proving the product
**Hook:** “We had broad reliability tests and still could not call the demo generator ready.”  
**Facts/Evidence:** `backend/tests/`, fixture README, blocked live Browserbase acceptance.  
**Lesson:** synthetic correctness, infrastructure availability and human-perceived quality are separate acceptance layers.

### Why provider-neutral architecture matters
**Hook:** “The browser provider failed, but the run still had a useful failure report.”  
**Facts/Evidence:** provider adapters, `generation.py`, execution/quality artifacts and repair model.  
**Lesson:** make provider failure an explicit product state, not an unstructured crash.

## 21. Open Questions

**Technical:** Can revised 20/24-operation plans finish in a funded cloud session? Does native Browserbase MP4 preserve every required effect and timing signal? Can browser video and action trace be aligned robustly under remote latency?

**Product/UX:** What request vocabulary and configuration should be exposed to users without making every run a manual production task? Which audience profiles actually need a 2–3 minute walkthrough versus a shorter focused flow?

**AI/generalization:** How reliably can safe representative detail interactions be discovered across unfamiliar SPAs, data-heavy dashboards, virtualised lists and custom controls? What benchmark breadth and rubric demonstrate generalization rather than two successful sites?

**Video quality:** What measured thresholds should define natural cursor motion, scroll continuity, camera bounds and caption quality beyond deterministic safety checks? Does measured ElevenLabs audio improve or complicate synchronization enough to justify making it required?

**Infrastructure:** What provider budget, timeout, session-retry policy, artifact lifecycle and production observability are required for predictable paid operation?

## 22. Current Next Priorities

1. **Critical:** restore Browserbase credit/session availability, then run the planned clean acceptance captures.  
2. **High:** render and manually/QA review complete Portfolio and Study Plan outputs against samples.  
3. **High:** expand safe interaction discovery and real-site benchmark coverage.  
4. **Medium:** validate ElevenLabs measured-timing path.  
5. **Medium:** production operational/load validation and frontend end-user flow.

The reliability-first implementation plan is broader than current accepted evidence: code covers many layers, while final live delivery, broader benchmarks, TTS validation, CAPTCHA/auth cases and showcase quality remain incomplete.

The plan and code therefore disagree in one important sense: the plan describes a reliability-first end state, whereas the repository has implemented substantial mechanisms without proving the end-to-end acceptance criteria on a complete live output. Priority should remain validation and targeted repair, not a claim of architectural completion.

## 23. Important Files and Repository Map

| Path | Purpose | Status |
|---|---|---|
| `README.md` | Project overview | Documentation; verify against code. |
| `productlens_revised_reliability_first_implementation_plan.md` | Target architecture | Plan, not proof of completion. |
| `backend/src/productlens/services/generation.py` | Staged URL pipeline | Core implementation. |
| `backend/src/productlens/contracts/models.py` | Shared typed contracts | Core implementation. |
| `backend/src/productlens/discovery/` | Evidence collection | Implemented. |
| `backend/src/productlens/planning/` | Candidate/production plans | Implemented, evolving. |
| `backend/src/productlens/execution/` | Playwright semantic execution | Implemented, live constrained. |
| `backend/src/productlens/presentation/` | Story/journey/camera/captions | Implemented, live quality unproven. |
| `backend/src/productlens/quality/` | Delivery gates | Implemented. |
| `backend/alembic/` | Persistent schema evolution | Implemented. |
| `backend/tests/` | Regression/integration coverage | Extensive; not live proof. |
| `productlens-test-htmls/` | Fixture applications | Core test support. |
| `samples/` | Quality reference material | Benchmark only. |

## 24. Known Gaps in This Build Log

Unavailable from repository: reliable Git chronology, all prior external conversations/decisions, provider spend, human user testing, exact manual review outcomes, historical videos deleted outside current artifacts, and the reason/terms behind account limits.

The repository also cannot prove the actual visual appearance of every previously rendered output, browser-provider dashboard behavior, whether environment values are currently valid, production deployment topology, or user-facing frontend usability. Assertions marked as retained-artifact evidence describe files currently available, not a promise that every external service remains configured.

## 25. Running Development Updates

### 2026-09-13 — Adaptive execution and generic gesture hardening

**What changed:**
- Added versioned, provider-neutral `ActionIntent` and `ReplanDecision` contracts.
- Added universal key, hover, pointer-path, and drag execution with observed geometry in `DemoTrace`.
- Added evidence-grounded observe → re-ground → local suffix re-planning; dispatched actions cannot be replayed.
- Persisted effective plans, replan decisions, adapted state graphs, and actual verified flow artifacts.
- Made Stagehand advisory observation automatic for Browserbase runs while Playwright remains the source of truth.
- Preserved multi-waypoint cursor/drag paths in the Remotion presentation layer.
- Applied explicit authorization to generic record creation and stricter drag destination evidence checks.
- Added durable `heartbeat_at` stage leases, SQLite upgrade logic, Alembic migration, and a supervisor heartbeat task for long Browserbase/render stages.
- Normalized semantic route depth so mounted SPAs such as `/todomvc/` are treated as primary application entry points rather than wrapper pages.
- Normalized default documents (`/index.html` and `/index.htm`) to their owning route across discovery, planning, execution, and consistency QA, preventing duplicate opening loads after static-host redirects.
- Tightened focused-page compilation to keep at most two representative local content beats per page; repeated cards no longer become a crawler-like route sweep. Editorial qualifiers such as `focused`, `observed`, and `public` are excluded from requested feature vocabulary when no feature was named.
- Excluded generic pagination mechanics (`Previous`, `Next`, and equivalent controls) from reader landmarks while preserving compound content headings such as `Previous Internship`.
- Removed latent editorial lint/runtime defects in the deterministic narrator (undefined fallback subject, unused composition state, and non-interpolated strings).
- Made Alembic provider/heartbeat upgrades idempotent online and valid for offline SQL generation; the local database is migrated to `20260913_08`.

**Why:**
- Unfamiliar products must be handled from live evidence rather than product-specific route or action handlers.
- Unexpected UI state must be recoverable locally without replaying side effects or invalidating completed scenes.
- Presentation, narration, and QA must consume the verified effective plan rather than stale pre-capture intent.

**Result:**
- 493 non-integration tests pass; 9 integration tests remain deselected.
- Changed-file Ruff checks, Python compilation, Remotion TypeScript check, diff check, and the runtime genericity audit pass.
- The retained eight-run acceptance manifest remains deliverable, but those videos predate this change; a new live complex-app acceptance capture and manual frame review are still required before claiming final goal completion.
- A new Browserbase + Stagehand exploration was attempted for Playwright TodoMVC (`b9aaec89-ccd3-4bbd-9b78-a0b10dd8001a`). Discovery artifacts were produced, then planning correctly rejected the requested 120-second full walkthrough because the site exposed insufficient evidence-backed scenes; no misleading MP4 was delivered.
- A fresh Browserbase + Stagehand DemoBlaze planning revalidation (`9e6bc389-0661-4fdb-8e9f-5e01a090c0ed`) completed discovery and planning after the canonicalization/representative-beat fixes. It no longer emitted `/index.html` as a duplicate opening page; it selected one observed product detail route plus the opening page for the explicitly focused objective. This was intentionally stopped before production capture; final live video acceptance and manual frame review remain outstanding.
- A fresh Browserbase + Stagehand DemoQA attempt (`3e6d83e9-1a92-4f24-81c6-6d94e4f910d6`) reached planning but was correctly rejected before production because a pagination control had been promoted to a scroll scene and failed readable-local-evidence QA. The pagination-landmark fix above addresses that generic failure; the rejected run remains preserved as evidence and was not misreported as a video.

**Relevant files:**
- `backend/src/productlens/contracts/models.py`
- `backend/src/productlens/execution/engine.py`
- `backend/src/productlens/execution/playwright_adapter.py`
- `backend/src/productlens/services/generation.py`
- `backend/src/productlens/presentation/director.py`
- `backend/video/remotion/src/root.tsx`
- `backend/tests/test_contracts.py`
- `backend/tests/test_execution_plan.py`
- `backend/tests/test_presentation.py`
- `backend/src/productlens/persistence/repository.py`
- `backend/src/productlens/services/jobs.py`
- `backend/alembic/versions/20260913_08_stage_heartbeats.py`

### 2026-09-13

**What changed:**
- Fixed the generic editorial post-processor so semantic repetition checks ignore connective words and do not rewrite distinct page-specific narration into identical checkpoint captions.
- Same-page repair now selects wording from the scene interaction/story phase and keeps the page's observed section/route evidence.
- Remotion now uses native sequential `<Video>` playback for browser footage, a run-scoped `--public-dir`, and a fast x264 preset; old run media can no longer be copied into every render.

**Why:**
- A live DemoQA trace had valid, different navigation chapters rejected because the old repair compared raw grammar rather than editorial payload. The repair itself was making narration less specific.

**Result:**
- The trace-only narration QA passes locally for the affected run; the run is continuing through its single render path and still requires video QA/manual review before acceptance.
- Verification: 497 tests passed, 9 deselected; Ruff, compileall, and Remotion TypeScript checks pass. A scoped-public/native-video diagnostic render completed and its sampled frame preserved the complete 1920×1080 browser frame.

**What I learned:**
- A renderable MP4 is not acceptance evidence. Story-level validation must run against the exact script that will be rendered, after all post-processing.

**Problems discovered:**
- Older polling left overlapping local resume processes; stale compositor processes were stopped and the newest render was retained. No production browser evidence was discarded.

**Relevant files:**
- `backend/src/productlens/presentation/editorial.py`
- `backend/src/productlens/quality/editorial.py`
- `backend/tests/test_editorial.py`
- `backend/src/productlens/services/generation.py`
- `backend/tests/test_generation_urls.py`
- `backend/video/remotion/src/root.tsx`
- `backend/src/productlens/video/render.py`

### 2026-09-13 â€” Shared URL identity and run acceptance audit

**What changed:**
- Added `productlens.urls.canonical_product_url`, used by preflight understanding caches and database product-knowledge keys.
- Normalized HTTPâ†’HTTPS redirects, encoded paths, query ordering, fragments, trailing slashes, and nested default documents in one provider-neutral helper.
- Added `scripts/audit_run_acceptance.py`, a read-only run-level gate that checks the complete artifact contract, every independent QA layer, final video validity, and optional manual-review evidence.
- Added regressions proving cache identity and audit failure reporting.

**Verification:**
- URL/audit focused tests: 6 passed.
- Ruff checks pass for all changed files.

### 2026-09-13 â€” Opening-caption/event alignment repair

**What changed:**
- Editorial script generation now accepts the first successful readiness event
  explicitly. When a low-level readiness witness precedes the first narrated
  page scene, the presenter welcome is attached to that witness while the
  first page scene retains its own evidence-backed explanation.
- Compatibility traces without a storyboard retain the same rebinding rule;
  no browser operation is replayed and no scene timing is invented.

**Why:**
- A valid DemoQA trace passed visual QA but failed synchronization because the
  welcome was attached to the first navigation at 18.7 seconds, leaving a
  long unexplained opening gap. This was a script/event ownership defect, not
  a provider or renderer failure.

**Verification:**
- Editorial regression suite: 60 passed.
- The existing trace is being retried from NARRATION only; discovery and
  production capture are preserved as immutable evidence.

### 2026-09-13 â€” Geometry-derived target focus

**What changed:**
- Camera direction now derives target-local zoom from observed control width/
  height and edge proximity instead of applying a fixed 1.12â€“1.18 value.
- Small fields and controls can receive up to the shared 1.36 safe ceiling;
  edge targets are automatically capped to preserve surrounding context.
- Presentation QA and bounded source-faithfulness sampling use the same ceiling
  and zoom candidates, keeping camera policy and delivery checks aligned.

**Verification:**
- Presentation and sample benchmark tests: 12 passed.
- Changed camera/QA files pass Ruff checks.

## 26. Document Metadata

### 2026-09-13 — Unfamiliar visual-app evidence and blank-content gate

**What changed:**
- Discovery now retains observed `canvas`, `svg`, `contenteditable`, application,
  and toolbar regions as semantic page landmarks. This is generic DOM evidence,
  not a diagrams.net adapter, and lets visual editors be planned from their
  actual working surface.
- Grounding treats implementation-level SVG/canvas terminology as one evidence
  ontology while still rejecting unrelated requested features.
- Video QA now samples the central product region independently of captions and
  shell chrome. A sustained near-uniform white product area fails delivery as
  `BLANK_PRODUCT_CONTENT_INTERVAL`.
- Run acceptance audit accepts a UUID shorthand (`artifacts/runs/<id>`) and can
  require a signed/manual-review artifact.

**Verification:**
- Discovery + planning focused tests: 73 passed.
- Quality/audit/planning tests: 49 passed.
- Full non-integration suite: 504 passed, 9 deselected.
- Compileall and Ruff checks pass for all changed files.
- Live RoadForge and OnlyDash runs have valid 1920×1080/30fps caption-led
  outputs, all QA layers passing, and manual sampled-frame reviews recorded.
- The DemoQA rerender now passes caption synchronization but is correctly
  rejected by the new blank-content gate; the diagrams.net run is also rejected
  because its discovery produced no grounded meaningful drawing workflow. These
  are intentional non-deliverable outcomes, not silently accepted videos.
- The generic fallback planner now compiles one observed tool-to-surface drag
  for drawing/design objectives when a canvas-like DOM region and a matching
  local tool are present. It uses discovered semantic targets and runtime
  geometry, with no editor-specific selectors.

### 2026-09-13 — Gesture presentation contract

**What changed:**
- Drag and pointer-sequence scenes now keep the cursor visible and classify
  them as deliberate demonstration gestures in the journey director.
- Presentation QA validates a drag against its observed source and destination
  geometry. The source control rectangle is no longer mistaken for the final
  canvas/drop endpoint.
- Added regressions covering drag cursor visibility, observed source hover,
  destination alignment, and malformed drag paths.

**Verification:**
- Focused presentation/journey/candidate suite: 40 passed.
- Full non-integration suite: 507 passed, 9 deselected.
- Changed-file Ruff and compileall checks pass.
- A fresh Excalidraw live retry is running from planning through production QA
  to verify the contract against an unfamiliar canvas application.

### 2026-09-13 — Generic canvas result gesture

**What changed:**
- The visual-editor fallback no longer treats moving a toolbar control over a
  canvas as a drawing demo. It now activates the observed tool and emits a
  semantic `short_reversible_stroke` operation against the observed surface.
- Playwright resolves the current surface bounding box immediately before the
  gesture and derives a bounded pointer path, so responsive layouts and
  different editors do not require stored coordinates.
- Production planning accepts this pattern only with grounded surface
  evidence; arbitrary coordinate or product-specific stroke data is rejected.

**Verification:**
- Candidate, execution-plan, presentation, and QA tests pass (40 focused;
  507 non-integration total).
- A fresh live Excalidraw retry is validating the visible result and the
  source/destination cursor evidence end-to-end.

### 2026-09-13 — Target-bound editorial repair and visual-result verification

**What changed:**
- Deterministic click and pointer narration now derives a viewer-facing state
  transition from the observed operation intent instead of reading a noisy DOM
  inventory. Semantic `element:` evidence is retained in the scene source even
  when the compact element table has no matching record.
- Native canvas surfaces are preferred over SVG overlays when both are observed
  for a visual-editor gesture; SVG remains a generic fallback.
- Reversible drawing gestures fingerprint the grounded canvas/SVG before and
  after dispatch. A gesture with no observable surface change now fails during
  execution rather than producing a misleading deliverable.
- Visible postcondition verification rechecks the selected semantic witness
  directly, avoiding stale `.first` races in responsive compositing layers.
- Added regressions for noisy visual-editor narration, semantic target evidence,
  canvas preference, and no-change gesture rejection.

**Verification:**
- Editorial tests: 63 passed; candidate and execution tests: 23 passed.
- Full non-integration suite before the final live-only changes: 509 passed,
  9 deselected; changed-file Ruff and compileall checks pass.
- The live Excalidraw trace was re-planned against the native canvas and its
  render/QA completed. Manual frame review showed that the site did not visibly
  commit the stroke, so the new result fingerprint gate intentionally rejects
  that class of false-positive run on future captures.
- RoadForge and OnlyDash remain accepted unseen live deliveries with complete
  artifacts, valid 1920×1080/30fps MP4s, all deterministic QA layers passing,
  and manual sampled-frame review.

### 2026-09-13 — Continuous pointer dispatch and target-region outcome proof

**What changed:**
- Pointer sequences now move to the first observed point and settle before
  pressing, then interpolate timed intermediate events between observed points.
  This keeps cursor, click, and gesture timing aligned and supports editors that
  require continuous movement rather than a three-point teleport.
- Reversible-stroke verification compares a padded screenshot region around the
  actual path, rather than the full frame, so opening a tool panel cannot count
  as a drawing result.

**Verification:**
- Focused regression suite: 87 passed; changed-file Ruff and compileall pass.
- Final Excalidraw cloud validation now fails explicitly at execution when the
  target-region proof shows no committed stroke. No invalid MP4 is promoted.
- The implementation report records accepted unseen deliveries and this
  intentional visual-editor rejection with its owning layer and recovery path.

### 2026-09-14 - Runtime capability resolution boundary

**What changed:**
- Added typed `CapabilityEvidence`, `RuntimeCapability`, and
  `CapabilityResolution` contracts for product-neutral affordances and
  runtime strategy selection.
- Added an evidence-only resolver that ranks navigation, forms, dialogs,
  tables, canvas/pointer surfaces, and drag/drop affordances from the current
  DOM/accessibility/geometry snapshot. Unresolved classes are persisted rather
  than guessed.
- Discovery and production planning persist
  `planning/capability-resolutions.json`; live delivery acceptance and the DB
  artifact mirror now include it.
- Added generic resolver regressions for forms/navigation, visual editors, and
  explicit unresolved capability handling.

**Verification:**
- Full non-integration suite: 516 passed, 9 deselected.
- Focused capability/contracts/planning/repository suite: 38 passed.
- Ruff, compileall, and runtime genericity audit pass (`runtime_offenders: []`).

### 2026-09-14 - Generic editorial and capability ordering hardening

**What changed:**
- Expanded the sentence predicate validator to accept grounded editorial verbs
  such as `highlights`, `presents`, `focuses`, and `describes`, preventing valid
  evidence-backed scroll narration from being rejected as unreadable.
- Navigation narration now introduces the destination page's observed purpose
  and facts instead of emitting route mechanics such as “next part of the
  walkthrough.”
- Verified form/record capability steps are inserted at the first natural visit
  to their source page, before leaving it; the planner no longer appends a
  return-home coverage repair.
- Acceptance runner now supports `--all-targets`, merging configured and
  historical URLs as data-only fresh retry candidates while preserving old run
  records and credentials out of runtime recipes.

**Verification:**
- Editorial, production-planning, and acceptance regressions: 106 passed.
- Changed-file Ruff checks pass.
- Fresh Portfolio run `351432c6-52d6-4650-bbf1-5dea403fe3f4` and fresh OnlyDash
  run `dde409b8-9396-455e-9771-c3e2f37a2e72` completed with deliverable MP4s and
  all delivery hard gates passing.

### 2026-09-14 - Discovery resilience and evidence-safe presentation

**What changed:**
- Bounded in-page DOM/accessibility discovery now inventories sections,
  cards, headings, controls, and same-origin semantic links without allowing a
  single locator or page-side query to stall an exploration run.
- Unnamed/unstable links remain diagnostic evidence but are excluded from
  route planning; this prevents crawler-like jumps to non-semantic targets.
- Full walkthrough budgets now expand based on page count and meaningful
  content so a complete tour is not forced through the short default budget.
- Capability evidence IDs are stable bounded hashes, preserving traceability
  without violating contract limits.
- Approved narration is synchronized back into storyboard scenes before
  editorial QA, making the approved script the timing/caption source of truth.
- Generic placeholder and noisy accessibility text are sanitized before
  narration validation; site-specific names remain evidence-derived.
- Visual blank-content QA samples a grid across the full product frame instead
  of a center crop, avoiding false failures for left-aligned white interfaces.

**Verification:**
- Discovery, editorial, capability, quality, and delivery regressions pass;
  compileall, Ruff, and the runtime genericity audit pass.
- Fresh repaired Study Plan and SmartSevak traces now have complete delivery
  artifacts and accepted caption-led MP4s. The remaining public-target matrix
  is being re-run in bounded detached batches with every run ID persisted.

### 2026-09-14 - Generic stale-control recovery

**What changed:**
- Runtime replanning now recognizes when a previously observed same-origin
  navigation control disappears after a SPA/page transition. It records an
  explicit, evidence-linked direct-navigation fallback to the already observed
  destination instead of asking the model to invent a replacement target.
- The fallback is limited to pre-dispatch navigation failures, preserves the
  no-replay side-effect rule, and remains visible in the trace/QA evidence.

**Verification:**
- Generation, execution-recovery, grounding, and production-planning tests:
  47 passed; compileall and Ruff pass.

### 2026-09-14 - Opening-navigation coverage accounting

**What changed:**
- Coverage QA no longer treats a synthetic opening `Navigate` (which has no
  editorial phase) as an empty page chapter. It now maps expected outcomes to
  actual establish/explore/explain/demonstrate/verify contracts, preventing a
  false rejection when the first content chapter executed completely.

**Verification:**
- Coverage, quality-report, and acceptance-audit regressions: 12 passed;
  Ruff passes.

### 2026-09-14 - Transient consent UI filtering

**What changed:**
- Generic candidate selection now recognizes actionable consent-banner labels
  as transient control chrome, so buttons such as “Necessary only” cannot become
  fake reading/scroll chapters. Real non-actionable policy content remains
  eligible for a walkthrough.

**Verification:**
- Production-planning and candidate-selection tests: 52 passed; Ruff passes.

### 2026-09-14 - Frame-aware semantic grounding

**What changed:**
- Playwright semantic grounding now searches embedded frame contexts after the
  top-level page for the same observed role, label, selector, and text. This
  supports generic hosted editors, widgets, and dashboard surfaces without
  adding iframe-specific routes or coordinate logic.
- Detached frames are treated as observational misses and flow through the
  existing recovery/replanning boundary.

**Verification:**
- Browser reliability, visibility grounding, and execution recovery tests: 9
  passed; Ruff passes.

### 2026-09-14 - Embedded-frame runtime evidence

**What changed:**
- Runtime replanning evidence now includes bounded visible text and semantic
  controls from embedded frames, while retaining the top-level page as the
  product route. This gives the adaptive loop enough context for hosted
  editors/widgets without treating frames as hardcoded application modules.

**Verification:**
- Browser reliability, visibility grounding, execution recovery, Ruff, and
  compileall checks pass.

### 2026-09-14 - Replan suffix continuity

**What changed:**
- Same-origin navigation recovery now preserves the validated continuation
  after the failed step. Adaptive replacement previously could retain only the
  fallback navigation and silently truncate later chapters; the repaired
  suffix keeps all unexecuted scenes while never replaying the failed action.

**Verification:**
- Execution-recovery and URL-generation regressions: 9 passed; Ruff and
  compileall pass.

### 2026-09-14 - Duplicate landmark suppression

**What changed:**
- Page planning now removes repeated landmark groups caused by responsive
  duplicate DOM containers or sticky campaign cards. The first grounded
  occurrence is retained and later duplicates cannot consume scene budget or
  generate repetitive narration.

**Verification:**
- Candidate, production-planning, and editorial tests: 117 passed; Ruff passes.

### 2026-09-14 - Accessibility skip-link filtering

**What changed:**
- Generic landmark selection now excludes actionable “skip to content” links
  from reader chapters. Accessibility aids remain captured as evidence, but
  they cannot trigger an off-viewport click or pollute a narrated walkthrough.

**Verification:**
- Candidate and production-planning tests: 52 passed; Ruff passes.

**Last Generated:** 2026-08-30
**Repository State:** Git state unavailable from this workspace; no commit is asserted.  
**Git History:** Not available / unreliable.  
**Primary Source:** Current repository.  
**Historical Completeness:** Partial — external development history is not represented here.

This document is intended as a factual development record, not marketing copy. Where information cannot be established from the repository, it is explicitly marked as unknown.
## 2026-09-14 — Generic acceptance completion and render promotion

- Freshly exercised the complete historical acceptance matrix: 47 unique URLs,
  with every attempt retained by run ID and owning failure layer.
- Added an acceptance `--refresh-only` path so persisted out-of-band renders
  can be promoted only after delivery QA, without spending another cloud run.
- Rendered and promoted DemoBlaze (`3a490dcc-1ebc-4c6e-a8d4-78085dda6df0`) and
  Will Be Done (`554e91ba-4203-4416-8fbe-6ef1ebf9bcc8`) from their validated
  traces; both passed artifact, synchronization, visual, and delivery gates.
- Re-ran the full non-integration suite (523 passed, 9 deselected), Ruff,
  compileall, and the runtime genericity audit (107 files, no offenders).

This entry supersedes earlier snapshot statements in this historical log that
predated the 2026-09-14 acceptance sweep; the durable manifest and
`PRODUCTLENS_FINAL_IMPLEMENTATION_REPORT.md` are authoritative for current
status.

## 2026-09-14 — MVP goal: concurrent worker and capability kernel hardening

- Local durable workers now run independently claimed stages concurrently up
  to `PRODUCTLENS_WORKER_CONCURRENCY` (bounded to 100), while retaining the
  same idempotent stage handler used by broker workers.
- Capability resolution now exposes evidence-backed graph, upload, and
  keyboard capabilities only when corresponding observed affordances exist;
  unresolved intent remains explicit rather than guessed.
- Added regression coverage for worker bounds and graph-capability resolution.

- Added a provider-neutral evidence graph with verified-transition-only Dijkstra
  search; unverified or unobserved transitions cannot enter a selected path.
- Added the generic `Upload` interaction primitive end-to-end (contract,
  Playwright file-control actuator, lifecycle/narration mapping) with path
  validation and regression coverage; upload capability resolution now maps to
  an executable operation rather than a detection-only result.
- Added provider-neutral `state_delta` witnesses to every interaction event,
  normalising away screenshot/grounding noise while recording URL, content,
  control, and scroll changes for verification and presentation QA.
- Added evidence-driven form dependency contracts and stable topological
  ordering; unknown or cyclic dependencies now fail closed instead of causing
  guessed field interactions.
- Hardened browser snapshots to omit password/token/OTP/API-key values and
  removed event-log text from persisted target evidence, keeping credentials
  out of traces while retaining non-secret state verification.
- Extended live page evidence to merge bounded controls/text from open shadow
  roots (alongside existing iframe evidence), so custom-element surfaces remain
  discoverable without product-specific selectors.
- Added page-scoped keyboard gestures for generic overlay dismissal and
  application shortcuts; they are semantically scoped and no longer require a
  guessed DOM target.
- Expanded the runtime gesture contract with evidence-grounded `select` and
  explicitly authorized `submit` gestures, keeping domain actions out of the
  interaction vocabulary.
- Submit intents now fail validation unless they carry an evidence-backed
  expected state and explicit mutation authorization, preventing an
  unverified click from being treated as a completed workflow.
- Verification after these changes: 536 non-integration tests passed,
  compileall and Ruff passed, and the runtime genericity audit found zero
  offenders. Live Browserbase acceptance remains a later gate, not silently
  counted as complete.
- Added a database-backed Server-Sent Events run-status stream. Clients can
  follow stage transitions and terminal errors without polling, while the
  stream exposes only non-secret run/stage metadata.
- Interaction events now persist their exact postconditions, allowing the
  lifecycle projection and repair tooling to preserve authorized submit
  witnesses instead of downgrading them to generic clicks.
- Form discovery now preserves observed validation messages and dependency
  hints from accessible/live control state, enabling generic validation-aware
  planning without route or product-specific rules.
- Added configurable provider backpressure for Stagehand, Browserbase, and
  OpenRouter requests. Durable worker leases still coordinate processes, while
  per-provider semaphores prevent a single worker from flooding an upstream.
- Latest verification: 539 non-integration tests passed; compileall, Ruff, and
  the runtime genericity audit pass with zero offenders. Live acceptance is
  intentionally still an explicit remaining gate.
- Added a first-class `changed` postcondition backed by provider-neutral state
  deltas. Generic clicks, editor gestures, uploads, and dependent controls can
  now require an observed state transition rather than relying on a product
  test flag or a successful dispatch alone.
- Regression verification after the changed-postcondition addition: 541
  non-integration tests passed; Ruff and compileall remain clean.
- Updated the greenfield README with the cloud Stagehand contract, SSE status
  usage, and deployment backpressure settings.
- Added redacted page-text and DOM fingerprints to target snapshots. Actions
  whose clicked control keeps the same label but changes application state now
  produce an observable evidence delta; the gate-3 semantic transition test
  passes again.
- Integration verification: all six browser primitive gates plus the dedicated
  gate-3 trace assertion and both persisted-evidence URL-stage tests passed.
  The complete integration command exceeded its blanket timeout because the
  URL-stage tests are long-running; they were run individually to completion.
- Added a reusable provider-neutral spatial index for fresh observed geometry.
  Duplicate responsive controls are now ranked by viewport proximity and a
  deterministic tie-break while Playwright semantic locators remain the only
  execution mechanism; no click coordinates are invented. Focused grounding
  and spatial-index tests pass.
- Latest verification: 543 non-integration tests passed (9 integration tests
  deselected); Ruff and compile checks remain clean. Live multi-site acceptance
  and final golden-video review are still explicit completion gates.
- Added a provider-free durable-claim concurrency benchmark using independent
  SQLite connections. The acceptance levels 1, 10, 50, and 100 each claimed
  every queued run exactly once with zero duplicate claims; this validates the
  CAS boundary without consuming Browserbase/OpenRouter credits. Provider
  throughput remains separately limited by configured semaphores.
- Full static verification after the benchmark: 547 non-integration tests
  passed (9 integration tests deselected), Ruff passed across source/tests,
  compileall passed, and the runtime genericity audit scanned 113 files with
  zero offenders.
- Unified Stagehand observation and observed-action subprocesses behind the
  same provider semaphore. Concurrent discovery can no longer bypass the
  configured Stagehand backpressure by using the observation path; existing
  timeout, origin, and response-validation behavior remains covered.
- Centralized persisted queue-stage to lifecycle-stage mappings in a shared
  stage contract. Fixture, URL, local-worker, and broker paths now resolve the
  same typed boundaries and fail closed for unknown stage names.
- Added `scripts/benchmark_concurrency.py`, which writes a provider-free JSON
  benchmark for the four supported worker levels. The command was executed
  successfully for 1/10/50/100 runs and reported `passed: true`.
- Added the product-neutral `gate8-capabilities` fixture and a live local
  discovery regression. It now exercises rich text, scrollable records,
  iframe, open shadow DOM, canvas pointer gestures, and graph drag/drop; live
  discovery resolves all six capability classes with no unresolved classes.
- Full static verification after typed evidence coverage: 552
  non-integration tests passed (9 integration tests deselected); Ruff and
  compileall remain clean.
- Shadow-root capability is now explicit: discovery persists shadow-host
  evidence and capability resolution emits a `shadow_dom` capability with
  grounded targets and expected readable-state outcomes. The advanced fixture
  regression now requires and verifies this class alongside the other six.
- Target snapshots now include a bounded accessibility-tree fingerprint in
  addition to redacted text and DOM fingerprints. State-delta verification can
  therefore prove ARIA/menu/dialog/selection transitions even when visible
  labels remain unchanged.
- `PageKnowledge` now exposes typed DOM, accessibility, and geometry evidence
  references rather than collapsing all observations into one undifferentiated
  list. Discovery populates those references from the actual page inventory,
  including canvas/iframe/shadow surfaces.
## 2026-09-14 — Editorial duration and Browserbase replay deadline alignment

- Raised the API and retry duration envelope from 300s to the contract-supported 600s.
- Removed the unrelated 300s HLS replay assembly ceiling; the caller's bounded deadline is now respected (up to the configured Browserbase/session policy).
- Added a regression test proving a 900s replay deadline is passed to ffmpeg assembly unchanged.
- This prevents valid long walkthroughs from being rejected or truncated by provider-local limits; it does not manufacture duration or bypass session safety bounds.

## 2026-09-14 — Side-effect-safe adaptive replanning

- Hardened the observe/act/verify/replan kernel so a dispatched submit or explicitly authorised mutation cannot be replayed under a newly generated operation ID.
- Read-only verification/recovery remains allowed after a dispatch; mutation retries now fail explicitly and are handed to the owning repair boundary.
- Added an execution regression test for the new-ID mutation replay case.

## 2026-09-14 — Unified ActionIntent execution boundary

- Added `ExecutionEngine.run_intent`, compiling Stagehand/model-produced provider-neutral `ActionIntent` values into the existing observe/act/verify trace kernel.
- This removes the risk of a second, divergent executor for unfamiliar controls and keeps semantic intents and validated plan operations on one safety path.
- Added a regression test proving intent compilation produces the normal traced event.

Verification: `ruff check src tests`, `python -m compileall -q src`, and
`python -m pytest -m 'not integration' -q` pass; latest suite result is **562
passed, 9 deselected**. The runtime genericity audit scans 114 files and
reports no acceptance-project coupling.

- Cloud production now passes the configured capture deadline into Browserbase
  replay publication/assembly, preventing long valid captures from failing at
  the provider method's short default.

- Added an executable capability regression covering rich text, iframe,
  shadow-DOM, canvas drawing, scrolling, and graph drag/drop. It exposed and
  fixed a real pointer bug: the generated reversible-stroke pattern had points
  but did not dispatch pointerdown/pointerup, so canvas actions silently did
  nothing. The pattern now defaults to a complete press/move/release gesture.

- Fixed a local-worker concurrency race: a stage claimed by the async worker is
  no longer requeued before execution. The claimed lease is passed directly to
  the stage executor, preventing a second worker from claiming duplicate
  provider work. Added a preclaimed-stage regression test.

- API URL runs now select cloud Browserbase+Stagehand discovery automatically
  when the provider is configured; callers can explicitly set
  `cloud_discovery=false` for local fixture work. This prevents the normal API
  path from silently bypassing semantic cloud exploration.

- Browserbase replay/download deadlines are now bounded by the configured
  isolated-session timeout. This removes the old arbitrary 300s cap while
  retaining a finite provider safety boundary.

- Persisted the approved fact-extraction checkpoint for every URL narration
  stage and made it part of the URL delivery/completion artifact contract.
  Editorial copy can now be audited and repaired from the exact evidence set
  rather than only from the final prose.

- Added the durable `knowledge_versions` ledger and Alembic revision
  `20260915_09`. Every refreshed product snapshot now records its version,
  fingerprint, confidence, evidence payload, and capture time; older versions
  remain available for freshness and re-grounding audits instead of being
  silently overwritten. Acceptance URL deduplication now uses the same
  canonical identity as discovery (scheme upgrades, default documents,
  trailing slashes, and sorted queries), and the acceptance supervisor emits
  JSON start/finish events so asynchronous runs are observable without
  repeated status polling.

Verification after this change: `ruff check src tests scripts` and
`python -m pytest -m 'not integration' -q` pass (**567 passed, 9 deselected**).

- Hardened long-render recovery: the final ffmpeg CFR/concat pass now has the
  same bounded compositor timeout as Remotion, and `resume_run.py` reconciles
  orphaned root/stage leases before selecting a checkpoint. A killed render
  can therefore be resumed from completed segments instead of leaving an
  unbounded child process and a permanently RUNNING stage.

- Fresh live acceptance: portfolio run `b262e29a-1848-4fe9-834c-b72ba0bec730`
  completed from its existing cloud exploration/production trace after a
  render-only resume. `audit_run_acceptance.py` reports all required evidence,
  1920×1080 H.264 at native 30fps, 143.893 seconds, and `deliverable: true`.
  The Browserbase session was not reopened during the repair.

- Fresh live acceptance: Study Plan run `a7abbd40-f437-4217-8af2-d54e3367dead`
  completed through independent Browserbase exploration, production capture,
  Remotion rendering, and video QA. The delivery audit reports all required
  artifacts, no hard failures, and a 1920×1080 H.264 native-30fps,
  117.547-second deliverable.

- Editorial quality repair: added a generic screen-transcript detector and
  evidence-bound summarizer. Grounded accessibility inventories that merely
  concatenate headings or card labels are now rejected before rendering and
  again during final editorial QA. Existing portfolio and Study Plan traces
  were rerendered without reopening Browserbase; both now pass delivery QA,
  retain full 1920×1080 native-30fps framing, and use presenter-oriented
  captions for dense category/week content.

- Configured multimodal verification was run against six rendered checkpoints
  for each fresh run using OpenRouter `google/gemini-2.5-flash`. Both returned
  `status=complete` with no hard failures or warnings. The repaired delivery
  manifests were regenerated after this review and the strict completion audit
  now reports `complete_evidence=true` for both runs.

- Applied the repository-wide Ruff formatter to backend source, tests, and
  scripts (213 files now pass `ruff format --check`). The broad exception in
  deployment readiness was kept explicitly suppressed and remains lint-clean.
  The post-format non-integration suite remains **572 passed, 9 deselected**.

- Unified semantic assistance across environments: Stagehand is now created
  for local and Browserbase workers and is attempted for local discovery and
  runtime replanning as well as cloud discovery. Its suggestions remain
  advisory and are re-grounded by the Playwright safety kernel; the legacy
  opt-in flag remains API-compatible but no longer creates a local/cloud
  intelligence gap.

- Added a provider-prompt redaction boundary. User objectives and observed
  evidence are sanitized for emails, phone numbers, credential assignments,
  and common provider-key prefixes before any OpenRouter or Stagehand prompt;
  the original objective remains unchanged in the local audit artifact.

  Verification after this change: `ruff check src tests scripts`, compileall,
  and `python -m pytest -m 'not integration' -q` pass (**567 passed, 9
  deselected**). A fresh historical acceptance sweep is running as a
  resumable background batch; its JSON start/finish events and per-target
  failure ownership are written to `backend/artifacts/acceptance/`.

- Added `scripts/audit_goal_completion.py` plus regression coverage. The audit
  is deliberately conservative: unresolved acceptance attempts keep the goal
  in progress instead of treating a single MP4 or a file-presence check as
  proof of completion.

- Acceptance supervision now reconciles each finished target against its
  durable repository stage and delivery report before classifying it. This
  closes the race where a render completed just as the supervisor deadline
  fired and would otherwise be recorded as a false failure.

- Added a durable `public-sweep-status.json` checkpoint. The completion audit
  now remains open while a sweep is running or lacks a terminal checkpoint,
  even if older records already exist for every URL; this prevents historical
  evidence from being mistaken for a fresh retest.

- Added provider-neutral golden trace and presentation-baseline fixtures under
  `validation/golden/`, with regression tests covering semantic navigation,
  smooth-scroll witnesses, native browser scale, bounded camera state, cursor
  geometry, and no-crop invariants. No product-specific choreography is used.

- Added strict mypy configuration for the core typed interaction boundaries
  (`state_diff`, spatial indexing, form dependencies, and stage contracts).
  `mypy` now passes with no issues, and it is included in the development
  dependency set so the check is reproducible in CI.

- Applied Alembic migration `20260915_09` to the configured development
  database and verified the database is now at head. Migration and repository
  regression checks pass (30 tests).

- Corrected complete-walkthrough transition compilation so evidence-backed
  route steps are re-grounded to visible same-origin navigation controls before
  validation. Tightened page-state validation so a control observed only on a
  prior route cannot be reused after entering an embedded/app workspace. Added
  regressions for both cases; planner/candidate tests pass.

- Duration feasibility now accounts for observed interaction surfaces (visual
  canvases/SVG editors, forms, drag-drop, rich text, tables, overlays, and
  embedded/shadow content) as bounded editorial beats. This prevents a rich
  editor from being rejected as a one-heading page while keeping the estimate
  evidence-derived and capped; legacy dictionary capability snapshots remain
  readable during migration. Added a canvas regression and reran the suite
  (`578 passed, 9 deselected`).

- Unified planning, execution, and consistency URL identity with the shared
  canonicalizer. HTTP/HTTPS redirects, default documents, duplicate slashes,
  fragment state, and query-parameter order now resolve to one route identity
  in every layer; added cross-layer regression coverage.

- Story QA now distinguishes a stale target that was never dispatched and was
  fully replaced by a verified replan from a real failed browser event. The
  original miss remains in the trace for auditability, while successful
  replacement steps can complete the story; dispatched or incomplete failures
  still hard-fail. Added a regression for this recovery contract.

- Hardened the historical acceptance supervisor with an independent
  per-target deadline derived from the Browserbase lease plus render/QA
  allowance (capped at a bounded hour-scale window). A hung provider can no
  longer keep the entire matrix alive indefinitely; durable run artifacts are
  retained and the batch can resume safely from its manifest.

- Added a generic page-role guard so shallow footer legal/consent documents
  are not mistaken for primary product chapters in full tours. A policy page
  remains eligible when the objective explicitly requests it or a visible
  non-footer navigation control establishes it as a product destination; the
  guard contains no benchmark-site routes.

- Re-ran the complete deterministic suite after the page-role correction:
  **579 passed, 9 deselected**. Genericity, concurrency (1/10/50/100),
  Ruff, formatting, compilation, dependency, and configured strict-typing
  checks remain green. Live acceptance v3 is using the corrected source.

- Tightened the page-role guard to ignore in-page fragment links such as
  “skip to content” when deciding whether a policy document has a real
  primary entry. A fixture replay against the Webkio evidence now selects
  product sections only; privacy/cookie pages are excluded unless explicitly
  requested.

- Final static checkpoint after formatting: **579 passed, 9 deselected**;
  Ruff check/format, compileall, configured strict mypy, pip check, genericity
  audit, and concurrency benchmark all pass. The remaining gate is the
  background live acceptance matrix.

- After closing the model-candidate sanitization gap, the full deterministic
  suite is green at **580 passed, 9 deselected**. Strict core typing and all
  static checks remain green; v5 live acceptance is running against this
  exact source revision.

- Hardened semantic navigation re-grounding after the live Webkio sweep found
  a generic-selector collision: bare `a`/`button` selectors are now resolved
  by observed name and source-page provenance before selector fallback, and
  fragment links cannot be treated as page transitions. This prevents footer
  policy links from replacing the requested navigation target or injecting an
  unobserved route. Added a regression fixture; deterministic suite is now
  **581 passed, 9 deselected** with all static checks green. Acceptance v6 is
  running from the durable manifest with this revision.

- Closed the opening-navigation refresh variant: the compiler preserves the
  first canonical Navigate for the executor's no-op strip instead of turning
  it into a logo click. The complete deterministic suite now reports **582
  passed, 9 deselected**; all static, genericity, and concurrency checks pass.
  Acceptance v7 is running with the final compiler behavior.

- Hardened active-page navigation provenance: fresh page knowledge no longer
  reuses a primary link observed on a prior route, while sparse legacy
  contexts retain their compatibility fallback. Added regression coverage for
  stale headers; the deterministic suite is now **583 passed, 9 deselected**.
  Acceptance v9 is running with this revision.

- Fixed a multi-page evidence truncation defect found during acceptance v9:
  flattened navigation was globally capped, allowing a dense page to evict
  the visible control needed to reach a later page. Discovery now retains a
  bounded round-robin navigation quota per inspected source page, with a
  regression fixture. Static validation is now **584 passed, 9 deselected**;
  acceptance v9 continues as the live gate.

- Made the direct-route editorial gate source-page aware and canonical: it now
  rejects a direct URL only when an equivalent visible control was observed on
  the active page immediately before the transition. Unrelated controls on
  other pages no longer cause false failures. Added regression coverage; the
  deterministic suite is now **585 passed, 9 deselected**.

- Hardened acceptance supervision with a durable active-sweep lock. A
  refresh-only watcher now refuses to mark a sweep complete while its owning
  process is alive, preventing a stale manifest from producing a false-green
  completion audit. Added process-liveness regression coverage and restarted
  the fresh 47-target cloud sweep with the corrected supervisor.

- Hardened generic narration fallback: dense accessibility inventories are
  summarized from sentence-shaped local evidence, read-only states receive a
  concise viewer explanation, and heading-led fragments are rewritten as
  presenter prose. Added regression coverage; the active sweep is running with
  this revision.

- Corrected inventory summarization to prefer a complete first semantic clause
  instead of truncating a later clause at the caption budget. This keeps
  fallback captions grammatical while retaining the full observed inventory
  as evidence; targeted editorial tests remain green.

- Corrected acceptance progress reporting so ``completed_urls`` reflects only
  targets processed by the active sweep, not retained historical records. The
  durable manifest still retains historical attempts for audit and retry.
### Editorial gate hardening (2026-09-15)

- Scroll preflight now accepts explicit page-local section/fact evidence even when a provider omits compact prose, while still requiring reader-ready narration.
- Provider grammar fragments (`option includes ...`, doubled determiners) are normalized generically before QA; multi-predicate technical explanations are no longer misclassified as screen transcripts.
- Direct-route QA exempts only the initial bootstrap load and continues to enforce visible-control navigation for every subsequent transition.

### Fresh acceptance evidence (2026-09-15)

- Portfolio fresh run `2e586429-1133-411d-b225-b3952a58618a` passed all delivery layers: 153.237 seconds, 1920×1080 H.264 at 30fps, full frame/chrome, presenter greeting, and deliverable=true.
- Study Plan fresh run `d2b12860-1d10-4fbf-bbd4-70f6c4665672` has passed discovery, planning, execution, and narration and is in Remotion rendering.
- The complete historical acceptance matrix remains in progress; no completion claim is made until its durable sweep status and final audit pass.
### Query-aware navigation grounding (2026-09-15)

- Fixed generic planning compilation so an observed route with transient query
  state (filters/default SPA parameters) is matched by its route identity and
  never rewritten from a non-unique selector on the following page.
- Visible same-origin navigation remains preferred; target provenance accepts
  an equivalent path/query state only when the route was actually observed.
- Added a regression fixture covering the analytics-style query route and
  validated it against the full planning validator.
### Cloud scroll timing alignment (2026-09-15)

- Updated source-timing alignment to treat a scroll dispatch's recorder clock
  as authoritative when the exact pre-motion pixel witness is sparse. Click,
  form, and state-change events remain perceptually strict.
- Added a regression fixture proving a temporally grounded scroll is accepted
  even when its dispatch frame is transitional, without synthesizing playback
  timing.

### Placeholder landmark grounding (2026-09-15)

- Prevented template filler headings from becoming editorial story subjects
  when the same page evidence identifies lorem-ipsum starter content. Real
  headings remain eligible; the filter is evidence-gated and product-agnostic.
- Added a production-planning regression fixture and validated query-route and
  placeholder selection behavior.

### Route-only workflow repair (2026-09-15)

- Navigation compilation now re-grounds both direct route and route-only
  `OpenNavigationItem` proposals against the active page's observed visible
  control. A model response missing a target can no longer fail late or cause
  coordinate/selector guessing when an equivalent link was captured.
- Added a regression fixture for route-only navigation proposals.

### Placeholder editorial evidence (2026-09-15)

- Editorial evidence sanitization now drops starter lorem-ipsum filler while
  retaining legitimate documentation that discusses the term. This prevents
  template text from becoming a false product introduction or caption.
- Added regression coverage; editorial suite (73 tests) and configured strict
  typing remain green.

### Opening-section label hygiene (2026-09-15)

- Opening introductions now omit bare accessibility/template labels such as
  `Heading`, `Title`, and `Description`, while retaining real observed section
  names. This keeps generic products from receiving a misleading opening
  summary without adding site-specific rules.
- Added a regression test for the evidence-gated label filter; editorial suite
  now passes 74 tests.

### Stale navigation destination recovery (2026-09-15)

- Navigation compilation now uses an observed same-origin control when a
  model-supplied destination is stale or normalized to a different path. It
  preserves intentional query state when the route is the same and otherwise
  records the observed href as the postcondition.
- Semantic intent matching is conservative (a unique visible control must be
  identified), and regression coverage verifies both query preservation and
  contradictory-route repair.

### Safe Windows supervisor liveness probe (2026-09-15)

- The acceptance supervisor now checks Windows process handles instead of
  using `os.kill(pid, 0)`, which is not consistently non-destructive in an
  embedded Python host. POSIX retains the signal-0 probe. The lock/liveness
  regression now passes without destabilizing the test runner.

### Same-page semantic navigation recovery (2026-09-15)

- Runtime replanning now permits an auditable same-origin direct-navigation
  fallback when a previously observed visible control disappears before
  dispatch, including responsive or hydrated navigation changes. It skips
  only true no-op destinations and preserves the validated continuation
  suffix, so a missing control does not silently truncate the walkthrough or
  replay a side effect.
- Generation, execution, and URL regression suites remain green.
### Bounded replay cleanup after interrupted cloud capture

The Browserbase replay endpoint retains the configured long timeout for valid
captures, but an execution that has already failed no longer waits through a
second full lease while fetching an undeliverable recording. Interrupted
captures use a 120-second cleanup bound, preserve the trace/error evidence, and
remain explicitly rejected by delivery QA. This keeps retries responsive
without changing successful-video quality or duration.
## 2026-09-15 — Single-state duration and advisory Stagehand resilience

- A real one-page product with no additional viable interaction scenes now records at native speed and is evaluated against its evidence-backed scene dwell, rather than being stretched to the generic 110-second thorough-tour floor.
- The exception is persisted in delivery QA (`duration_exception`) with scene/page counts and the original requested minimum; multi-page and action-driven stories retain strict duration gates.
- Stagehand quota/model/network outages are retained as explicit exploration warnings while grounded Playwright evidence remains eligible for delivery. A missing or malformed Stagehand artifact is still a hard failure.
