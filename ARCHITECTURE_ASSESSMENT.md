# ProductLens 2.0 architecture assessment

This is the implementation audit for the senior-engineering refactor goal. It
describes the code that exists in this checkout, rather than treating the
architecture described in `README.md` as proof that every boundary is already
enforced.

## Current architecture and data flow

The backend is a FastAPI application backed by SQLAlchemy. API handlers create
or load a run and hand durable work to `services.jobs` and the local/Dramatiq
worker layer. `services.runtime.build_job_service` is the composition root: it
constructs the repository, provider adapters, planner, generator, and artifact
storage.

The generation path is currently coordinated by `UrlGenerationService`.  The
coordinator still owns the stage sequencing (and remains the largest module),
but pure policy and knowledge conversion are now explicit collaborators:
`services.generation_policy` contains deterministic URL/timing/diagnostic
decisions, while `services.knowledge` is the single ProductContext →
ProductKnowledge materializer. The render-stage orchestration now lives in
`services.render_stage`, so media loading, duration policy, render status, and
render-layer failure classification are no longer embedded in the browser
generation coordinator. These collaborators keep those rules independently
testable without changing the public service API.

The generation path is:

1. Normalize the request into an `ObjectiveSpec`.
2. Discover page and product evidence with `LiveDiscovery` (and optionally
   Stagehand enrichment).
3. Compile/scorе candidate capabilities and a production plan.
4. Rehearse or recover the selected operations.
5. Create a fresh browser context and execute through `ExecutionEngine` and
   `PlaywrightAdapter`.
6. Build a presentation/journey and evidence-grounded narration.
7. Render the trace and source recording with FFmpeg/Remotion.
8. Run execution, editorial, visual, synchronization, and delivery checks,
   persisting artifacts and retry lineage throughout.

The frontend is a Next.js client. It owns account/session and run-management
screens and calls the API; browser execution remains a backend concern.

## Complexity hotspots

### `services/generation.py` (4,947 lines)

`UrlGenerationService` is a god service. It owns objective interpretation,
discovery, Stagehand calls, viewport selection, cloud/local browser lifecycle,
rehearsal recovery, execution, narration, rendering, QA, artifact writes, and
retry/session release. The stage methods are individually large (`rehearsal`
~793 lines, `execute` ~687, `qa` ~518, `narration` ~441, `discover` ~416), so
failure ownership and unit-test setup are difficult to see.

The useful boundary is the stage contract already present in the code. The
generator should become a thin coordinator over stage collaborators; provider
construction, browser lifecycle, artifact serialization, and quality policy
should not be embedded in every stage.

### `discovery/live.py` (2,802 lines)

`LiveDiscovery.discover` (~738 lines) combines page capture, URL/route
canonicalization, DOM and accessibility extraction, forms, safe probes,
relationship inference, screenshots, and Stagehand enrichment. Page evidence
collection and exploration policy are different responsibilities. Keeping them
together makes it hard to test extraction using a fixture without running a
browser and encourages broad probing when a targeted budget is required.

### `planning/candidates.py` (3,007 lines)

Candidate construction, page-content summarization, capability matching,
navigation selection, and legacy compatibility/fallback behavior are mixed in
the same module. `build_page_complete_proposal` (~728 lines) is not one
algorithm; it contains several proposal types and editorial heuristics. The
planner needs a small evidence-to-candidate pipeline with explicit scoring and
validation functions.

### `planning/production.py` (2,242 lines)

This module owns grounding, route compilation, objective validation, page
coverage, and production-plan validation. These operations all consume the
same contracts, but their error ownership is unclear. Navigation compilation
should consume already-validated steps and return operations; it should not
re-decide objective relevance.

### `presentation/editorial.py` (4,294 lines)

Storyboard construction, deterministic observed narration, OpenRouter script
generation, evidence binding, caption timing, and editorial validation are
co-located. The code has useful rules, but the writer and validator are coupled
to trace parsing and presentation layout. Facts, script generation, and
validation should be separately callable and separately testable.

### `execution/playwright_adapter.py` (1,428 lines) and `execution/engine.py`

The adapter mixes locator resolution, overlays, snapshots, forms, scrolling,
pointer/canvas gestures, and operation dispatch. The engine controls lifecycle,
retries, event recording, and recovery. The separation is directionally right,
but the interaction kernel currently records the action lifecycle rather than
being the authoritative candidate-selection boundary. That distinction must be
made explicit without making the adapter depend on LLM reasoning.

### `persistence/repository.py` (1,571 lines)

One repository contains runs, jobs, projects, users, artifacts, provider
attempts, knowledge versions, leases, and previews, including dialect details.
It is a stable compatibility facade, but its SQL operations should be grouped
behind focused persistence modules or at least private table-specific helpers.
The first safe step is reducing repeated transaction/dialect handling while
preserving the public repository API.

### `video/render.py` (1,728 lines)

Media probing, source-cut policy, caption/audio timing, Remotion process
management, provenance checks, promotion, and render diagnostics are mixed.
Source timing and composition policy are separate concepts and should not share
filesystem/process code. Render-stage orchestration has been moved to
`services.render_stage`; the remaining module is the media/composition boundary.

### Frontend

The Next.js routes build successfully, but page components and `api.ts` are
compressed into very dense one-line implementations. This obscures state
ownership and error handling even though it does not currently break the build.
Readability refactoring should extract typed API calls and feature components,
without changing the backend contract.

## Cross-cutting findings

- The contract module is the correct shared boundary; most internal imports
  converge there. It is large but represents genuinely shared schemas.
- Importing all 116 backend modules succeeds, so no import cycle is currently
  observed.
- Config is centralized in `Settings`; numeric and boolean environment values
  now go through typed, bounded parsers with safe defaults.  Malformed values
  are covered by unit tests rather than leaking conversion exceptions from the
  composition root.
- Strict mypy now covers the settings, provider, contract, knowledge,
  persistence, worker, execution-boundary, presentation, quality, and security
  modules.  The intentionally narrow strict set passes.  A full-source mypy
  run still reports legacy errors in untouched API/discovery/planning code; that
  remains an explicit follow-up rather than weakening the passing strict set.
- Ruff is clean for all backend source and tests after formatting and small
  simplification fixes.
- Runtime artifacts, recordings, caches, and the local database are generated
  state and are correctly kept out of source control by the project ignore
  rules.
- There is no evidence of a circular dependency or unused third-party package
  from the import audit alone; dependency removal requires usage-level review.

## Simplification and refactor priorities

1. Keep the validation baseline repeatable (tests, compile, strict mypy,
   frontend build); it is now established in CI-style local commands.
2. Continue making `UrlGenerationService` a coordinator by moving browser lifecycle,
   stage persistence, and QA aggregation behind cohesive collaborators that
   own real behavior (not forwarding wrappers).
3. Split discovery capture from exploration policy, preserving one public
   `LiveDiscovery` facade for compatibility.
4. Split editorial facts, script writing, and script validation; retain the
   evidence IDs and scene IDs as the single source of truth.
5. Reduce adapter/engine overlap by making target resolution and verification
   explicit execution responsibilities, while keeping LLM/provider calls out
   of browser primitives.
6. Group repository operations by bounded context and centralize transaction
   handling before considering a public API change.
7. Separate render media mechanics from presentation cut/composition policy.
8. Rewrite the densest frontend modules into readable typed components after
   backend contracts are stable.

Each extraction must be followed by focused tests and the full deterministic
suite. A smaller file is not itself a success; the success criterion is that a
senior engineer can identify ownership, inputs, outputs, and failure behavior
without tracing unrelated concerns.

## Explicit non-goals

This refactor does not add website-specific recipes, relax evidence gates,
replace deterministic browser capabilities with LLM guesses, or claim universal
canvas/remote-desktop automation. Those are product capability questions, not
just code-organization problems.
