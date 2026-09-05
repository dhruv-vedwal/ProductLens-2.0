# ProductLens Build Log

**Last Generated:** 2026-08-30  
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

### YYYY-MM-DD

**What changed:**
- 

**Why:**
- 

**Result:**
- 

**What I learned:**
- 

**Problems discovered:**
- 

**Relevant files:**
- 

## 26. Document Metadata

**Last Generated:** 2026-08-30  
**Repository State:** Git state unavailable from this workspace; no commit is asserted.  
**Git History:** Not available / unreliable.  
**Primary Source:** Current repository.  
**Historical Completeness:** Partial — external development history is not represented here.

This document is intended as a factual development record, not marketing copy. Where information cannot be established from the repository, it is explicitly marked as unknown.
