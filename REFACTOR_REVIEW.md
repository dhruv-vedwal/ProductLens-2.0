# ProductLens 2.0 refactor review

## Scope

This review records the state of the codebase after the senior-engineering
cleanup pass. It is intentionally an implementation review, not a claim that
the product can automate every arbitrary web application without evidence or
provider access.

## Architecture confirmed

ProductLens has one request-to-delivery pipeline:

```text
API request
  -> objective normalization
  -> evidence discovery and Stagehand enrichment
  -> knowledge and relevance graph
  -> candidate workflow and production plan
  -> rehearsal and browser execution
  -> trace-backed journey, script, and captions
  -> Remotion/FFmpeg render
  -> execution, story, visual, synchronization, and delivery QA
  -> persisted artifacts and targeted retry lineage
```

The contracts in `contracts.models` are the cross-layer data boundary. Browser
primitives do not write narration, narration does not drive the browser, and
rendering consumes a trace/script rather than deciding product intent. The
frontend calls the API and does not own worker or browser state.

## Changes made

- Added `ARCHITECTURE_ASSESSMENT.md` with the actual component map, complexity
  hotspots, coupling risks, and follow-up boundaries.
- Centralized deterministic URL, selector, timing, frame-rate, and render-error
  policy in `services/generation_policy.py`.
- Centralized ProductContext → ProductKnowledge conversion in
  `services/knowledge.py`; the generation and worker paths now share one
  materializer instead of maintaining divergent copies.
- Moved render-stage orchestration (artifact loading, duration accounting,
  render status, and failure classification) into `services/render_stage.py`;
  `UrlGenerationService` now delegates that boundary.
- Moved database DDL into `persistence/schema.py` and kept the repository as a
  compatibility facade for existing callers.
- Replaced permissive environment parsing with typed, bounded settings helpers.
- Added real schema generics to provider protocols and strict typing to the
  persistence, workers, quality, presentation, security, and provider-boundary
  modules.
- Made SQLite worker claiming resilient to bounded lock contention while
  preserving the compare-and-set claim invariant.
- Applied consistent formatting and readability cleanup across backend and
  frontend modules without changing their public routes or API shapes.

## Validation evidence

- Strict mypy: **27 modules, no errors**.
- Ruff: **all backend source and tests clean**.
- Python compile check: **passed**.
- Deterministic backend suite: **624 passed, 9 live tests deselected**.
- Concurrency acceptance: **4/4 claim levels passed**, including 100 workers.
- Frontend production build: **passed** for all 11 static routes.
- Backend import audit: all discovered modules import successfully.

## Remaining complexity and risks

`services/generation.py`, `discovery/live.py`, `presentation/editorial.py`,
`planning/candidates.py`, and `video/render.py` remain large because they still
contain cohesive but broad stage algorithms. They are documented as the next
decomposition boundaries; they were not split into forwarding wrappers merely
to reduce line counts. Full-source mypy still reports legacy errors outside the
strict boundary, primarily in untouched API/discovery/planning/provider code.

Live Browserbase, Stagehand, OpenRouter, and media-tool validation remains an
environmental acceptance concern. The local suite proves contracts, persistence,
concurrency, and deterministic policy; it cannot prove credentials, remote
CAPTCHA handling, or the visual quality of a new website without a live run.

## Decision

The refactor is safe to merge as an engineering-quality improvement: behavior
is covered by the passing deterministic suite and the changed boundaries are
typed and documented. Further work should be performed as focused extractions
of the listed hotspots, each accompanied by focused tests and the same complete
validation baseline.
