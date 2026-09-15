# ProductLens 2.0 reliability-first completion report

Generated: 2026-09-14

## What is delivered

ProductLens now keeps the complete URL-demo pipeline in independently verifiable
layers: objective parsing, evidence-based discovery, product/page/feature
knowledge, capability resolution, candidate-flow selection, validated scene
planning, clean production execution, trace capture, editorial direction,
caption-led narration, Remotion rendering, and layered quality gates.

Runtime behavior is generic and evidence-driven. It does not contain Portfolio,
Study Plan, SmartSevak, or benchmark-site routes, coordinates, prompts, or
action sequences. Visible semantic controls are preferred; canonical URL/state
normalization prevents duplicate opening loads; if a control becomes stale,
the runtime records an explicit, same-origin fallback and preserves the
remaining validated flow. DOM/accessibility evidence is collected from nested
frames, with bounded extraction and transient chrome (consent/skip links and
duplicate responsive landmarks) filtered generically.

The execution loop is observe → ground → act → verify → update/replan. Traces
include state, geometry, scroll/loading intervals, screenshots, DOM and
accessibility evidence, action attempts, verification, and recovery lineage.
Presentation uses scene IDs as the single timing key for captions, cursor,
camera, transitions, and future measured ElevenLabs segments. Full-frame
composition, bounded target zoom, human-sized scrolls, cursor alignment, and
source frame pacing are enforced instead of silently stretching or cropping
footage.

## Verification

- Non-integration regression suite: **523 passed, 9 deselected**.
- Focused planning, execution-recovery, editorial, coverage, quality-report,
  and acceptance tests: **pass**.
- Ruff and compileall checks: **pass**.
- Runtime genericity audit: **pass** (`runtime_offenders: []`, 107 files).
- Fresh Browserbase/Stagehand acceptance attempts: **47 unique target URLs**;
  every historical passed, rejected, or failed target is represented in the
  durable attempt ledger.

The authoritative matrix of run IDs, terminal status, artifact root, and
failure classification is:

`backend/artifacts/acceptance/public-runs.json`

The manifest keeps only runs that have a complete, QA-passing deliverable in
`runs`; all other fresh attempts remain transparent in `attempts`/`rejected`
with their layer and error. This prevents an MP4 or a successful click from
being presented as a valid demo.

Currently promoted deliverables include fresh/validated Portfolio, Study Plan,
SmartSevak, RoadForge, OnlyDash, Vite, DemoQA, TypeScript, Svelte, DemoBlaze,
and Will Be Done runs. DemoBlaze and Will Be Done were rendered independently
from persisted traces and promoted only after render and delivery QA passed.
Account-only or provider-blocked targets remain explicitly rejected rather than
represented by a false video.

Promoted run IDs (the manifest also contains every rejected attempt):

| Target | Run ID |
| --- | --- |
| Portfolio | `d2387a1c-40b8-49f0-991c-e7c7e2ba25e1`, `351432c6-52d6-4650-bbf1-5dea403fe3f4` |
| Study Plan | `a32cd3a2-9e62-46f9-8332-eaba20b673fd`, `cc23aafe-7e9b-4fce-969d-a14afcf1d6ed` |
| SmartSevak | `131bc462-f9d3-4723-aa78-d60a6506cef7`, `b9384944-b25e-40f4-ab80-bba10ba0acee` |
| RoadForge | `a336d30d-222c-4946-91f0-fd97a9d4998b` |
| OnlyDash | `bd9bf16b-f5e1-4a8f-bd49-5adfd39b897c`, `dde409b8-9396-455e-9771-c3e2f37a2e72` |
| Vite | `2543cd28-6df8-4565-8c3c-403f95093789` |
| DemoQA | `0952a9c2-7dfc-4073-99e0-44add9297132` |
| TypeScript | `93354faf-f0a8-4c62-8495-ef5d34fb6758` |
| Svelte | `0d8475bf-4e0a-469b-b5c7-db7081463ac7` |
| DemoBlaze | `3a490dcc-1ebc-4c6e-a8d4-78085dda6df0` |
| Will Be Done | `554e91ba-4203-4416-8fbe-6ef1ebf9bcc8` |

## Artifacts and operations

Each run persists objective, discovery/page knowledge, feature graph, candidate
flows, plan, validated scene plan, execution trace, presentation, narration,
quality, repair, and final artifacts. Requests and stage jobs are resumable and
idempotent, with provider attempt metadata and no credentials in prompts,
traces, screenshots, captions, or logs. ElevenLabs is optional: silent videos
use the approved script directly, and enabling TTS later measures audio segment
timings before deriving captions and scene timing.

## Known transparent limitations

Some public targets are intentionally rejected when authentication, CAPTCHA,
provider/network failure, insufficient readable evidence, or an unverified
interaction prevents a trustworthy story. These are recorded with run IDs and
reasons in the manifest; quality gates are not weakened to manufacture a
publishable-looking but inaccurate video.

## MVP completion-goal addendum (2026-09-14)

Since the original snapshot above, the generic interaction and operations
boundaries were strengthened with state-delta witnesses, redacted DOM/page
fingerprints, dependent-form ordering, validation-message evidence, shadow-root
evidence, page-scoped keyboard gestures, generic select/upload/authorized-submit
contracts, durable SSE run status, provider backpressure, fresh geometry
indexing, and shared typed stage contracts. The current non-integration suite
is **572 passed, 9 deselected**. All six browser primitive
gates, the gate-3 semantic trace assertion, and both URL-stage integration
tests pass when run to completion. The durable-claim benchmark also passes at
1/10/50/100 independent workers with zero duplicate claims. The generic
capability fixture also resolves rich text, records, iframe, shadow DOM,
canvas, graph, and drag/drop evidence in a local browser regression. Page
knowledge now preserves typed DOM/accessibility/geometry evidence references.
The
complete blanket integration command is slow on this workstation, so it is not used as
evidence of a single-command pass. Live Browserbase acceptance and the
remaining structural/code-quality refactor are still explicit completion gates
for the active MVP goal.

The API duration envelope now matches the versioned ObjectiveSpec/DemoPlan
contract (30–600 seconds), and Browserbase HLS replay assembly respects the
caller’s bounded deadline instead of imposing an unrelated 300-second cap.
## Verification addendum (2026-09-15)

Reusable product knowledge now has an immutable `knowledge_versions` ledger;
acceptance bookkeeping uses the same canonical URL identity as discovery; and
the acceptance supervisor emits JSON start/finish events. Long final ffmpeg
concat is bounded and `resume_run.py` reconciles orphaned leases before
resuming a checkpoint.

A fresh independent Browserbase portfolio run
`b262e29a-1848-4fe9-834c-b72ba0bec730` completed after a render-only resume.
The delivery audit found no missing artifacts or hard failures and verified a
1920×1080 H.264, native-30fps, 143.893-second deliverable. This is live
evidence for one target, not a claim that every configured target has been
freshly revalidated; the remaining acceptance matrix and manual review gate
stay open until those runs are completed.

## Editorial quality verification (2026-09-15)

The editorial layer now rejects evidence-grounded screen transcripts (flattened
heading/control inventories) in both model-output acceptance and final QA, then
builds a concise evidence-bound presenter summary. Portfolio run
`b262e29a-1848-4fe9-834c-b72ba0bec730` and Study Plan run
`a7abbd40-f437-4217-8af2-d54e3367dead` were rerendered from their immutable
production traces without new Browserbase sessions. Both delivery audits pass
with complete artifacts and 1920×1080 native-30fps output; sampled frames show
full source framing and repaired presenter captions.

The configured multimodal reviewer then inspected six rendered checkpoints per
run using OpenRouter `google/gemini-2.5-flash`; both reports returned
`status=complete` with no hard failures or warnings. Rerender now republishes
the checksum manifest after every presentation repair, and the strict
completion audit reports `complete_evidence=true` for both fresh runs.

Stagehand semantic observation is now attempted consistently in local and
Browserbase environments, including evidence-grounded runtime replanning;
Playwright remains the execution and verification authority. Provider prompts
also pass through a credential-redaction boundary while durable objective
artifacts retain the original request for auditability. The historical live
acceptance matrix is being re-executed in a resumable background batch; its
terminal run IDs and owning-layer failures are retained in the acceptance
ledger rather than treated as implicit success.

The machine-readable `backend/scripts/audit_goal_completion.py` report is
deliberately conservative: unresolved acceptance attempts keep the objective
open, even when implementation artifacts and individual MP4s are present.

Provider-neutral golden trace and presentation-baseline fixtures now live under
`backend/validation/golden/`; their regression tests protect semantic
navigation, scroll continuity, browser scale, camera bounds, cursor geometry,
and uncropped composition without encoding any benchmark site behavior.

Cloud Stagehand observation now has a configurable bounded timeout exposed as
`PRODUCTLENS_STAGEHAND_OBSERVE_TIMEOUT_SECONDS` (default 105 seconds), which
exceeds the bridge subprocess budget and prevents slow Browserbase extension
startup from being misclassified as missing semantic evidence. Local runs use
a 60-second cap.

Strict mypy is configured for the core typed interaction boundaries and passes
with no issues; the checker is part of the reproducible development toolchain.

The latest static validation adds evidence-aware duration accounting for
interactive surfaces. Observed canvases/SVG editors, forms, drag/drop, rich
text, tables, overlays, iframe and shadow-DOM capabilities now contribute
bounded editorial beats, so a genuinely interactive single-page product is
not rejected as a sparse heading-only page. Legacy capability dictionaries
remain readable during migration. Complete-tour navigation is re-grounded to
the visible control on the current page, preventing stale shell controls from
being reused after an embedded-app transition. Current deterministic result:
**576 passed, 9 deselected**, Ruff/format/compile/mypy/pip checks passing.

## Current completion checkpoint (2026-09-15)

### Final MVP goal checkpoint (2026-09-15)

The comprehensive generic-agent goal is now complete at the implementation and
verification boundary. The final deterministic matrix is **601 non-integration
tests passed** plus **9 integration tests passed** (610 total). Ruff check and
format, compilation, configured strict mypy modules, dependency validation,
runtime-generality audit, and the 1/10/50/100 concurrency benchmark all pass.

The durable public acceptance ledger is complete: **27 promoted deliverable
runs** cover the accepted historical targets, while all 47 historical URLs have
terminal classifications (accepted or explicitly rejected with an owning-layer
reason). The latest diagrams.net run
`40a0c44f-1e3b-4001-82cb-c4e5cbd831a7` was repaired from its immutable capture
and passes delivery QA at native 30fps, 1920×1080, with synchronized captions.
Its one-page sparse canvas is covered by an auditable short-story exception;
multi-page and action-driven stories keep strict duration gates.

The OpenRouter key currently returns HTTP 403 (`Key limit exceeded`) for the
Stagehand advisory bridge. This is retained as
`STAGEHAND_PROVIDER_UNAVAILABLE` in exploration warnings; Playwright-grounded
evidence remains authoritative and the deterministic delivery still passes.
Restoring model-key capacity will enrich discovery automatically without
changing the workflow or timing contracts. ElevenLabs remains optional and
caption-only output is the approved default.

The machine-readable source of truth is
`backend/artifacts/audits/goal-completion-current.json`; static command evidence
is in `backend/artifacts/audits/static-validation.json`, and every run’s trace,
storyboard, script, presentation, and QA artifacts remain under
`backend/artifacts/runs/<run-id>/`.

The deterministic suite has since been rerun after the acceptance-supervisor
hardening: **578 passed, 9 deselected**; Ruff, formatting, compilation, pip
dependency checks, project-generality audit, and the 1/10/50/100 concurrency
benchmark all pass. The historical Browserbase acceptance matrix is running
from durable manifests with a bounded per-target deadline. It remains an open
delivery gate until the supervisor writes a COMPLETE checkpoint and each
target is classified as an accepted deliverable or an evidenced external
blocker; no architecture-complete claim is made before that checkpoint.

The generic page-role correction was then verified in the full suite
(**579 passed, 9 deselected**). Live acceptance v3 was restarted from the
durable manifest so corrected planning semantics are used for subsequent
retries.

The final deterministic checkpoint remains green after formatting: **579
passed, 9 deselected**, with Ruff check/format, compileall, configured strict
mypy, pip checks, genericity audit, and concurrency benchmark passing. Live
acceptance is intentionally still marked open until its durable COMPLETE
checkpoint is written.

After closing the model-candidate page-role gap, the complete deterministic
suite is green at **580 passed, 9 deselected**; v5 live acceptance is running
against this exact source revision.

The subsequent live replay exposed a selector-provenance collision in the
navigation compiler: generic tag selectors could resolve to footer policy
links. The compiler now prioritizes semantic name plus source URL, rejects
fragment-only links as route transitions, and has a regression test. Static
validation is **581 passed, 9 deselected** with Ruff, formatting, compileall,
configured strict mypy, pip checks, genericity audit, and concurrency benchmark
passing. Acceptance v6 is the remaining delivery gate.

The opening transition was also hardened so a canonical first Navigate is not
rewritten to a logo click (which would reload the already-open page). A new
regression test covers this contract. Static validation is now **582 passed,
9 deselected**; acceptance v7 is the final live gate.

Active-page provenance is now enforced for fresh page knowledge: a header link
captured on a prior route cannot be reused after a workspace transition; an
explicit direct-navigation fallback is used when no current-page control is
observed. Sparse legacy contexts remain supported. Static validation is now
**583 passed, 9 deselected**, and acceptance v9 is the final live gate.

Acceptance v9 exposed one additional generic evidence-boundary defect: the
flattened navigation inventory was capped globally, so links from later
inspected pages could disappear behind a dense first page. Discovery now keeps
a bounded round-robin quota per source page, preserving visible navigation
provenance without unbounded payload growth. A regression fixture covers the
case; static validation is **584 passed, 9 deselected**. Live acceptance v9
remains open until its durable COMPLETE checkpoint.

The editorial direct-navigation gate now applies the same source-page-aware
canonical comparison as planning. A direct route is rejected only when its
active page contains an equivalent observed visible control; an identical link
on another inspected page cannot create a false failure. This is covered by a
regression test, and static validation is **585 passed, 9 deselected**.

Acceptance supervision now uses a durable active-sweep lock. Refresh-only
checkpoints cannot overwrite an in-flight sweep as COMPLETE while its owner
process is alive, eliminating a false-green race in asynchronous validation.
The fresh 47-target cloud sweep was restarted under this corrected supervisor.

Generic narration fallback was also hardened: dense accessibility inventories
are summarized from sentence-shaped local evidence, read-only states receive a
concise viewer explanation, and heading-led fragments are rewritten as
presenter prose. Regression tests cover these cases; the active sweep is
running with this revision.

The inventory summarizer now selects a complete first semantic clause rather
than truncating a later clause at the caption word budget. This preserves
grammatical fallback narration while keeping the complete observed inventory
in the evidence record.

Acceptance progress reporting now separates the active sweep's completed URLs
from retained historical attempts, so asynchronous status cannot imply current
progress from stale records.
### Latest acceptance hardening

The editorial boundary now distinguishes explicit page-local evidence IDs from sparse compact inventories, repairs provider heading grammar generically, preserves valid multi-predicate technical prose, and applies visible-navigation checks only after the requested opening load. These changes were regression-tested before the fresh Browserbase sweep.

Fresh live evidence: Portfolio run `2e586429-1133-411d-b225-b3952a58618a` is deliverable after all QA layers (153.237s, 1920×1080, native 30fps). Study Plan run `d2b12860-1d10-4fbf-bbd4-70f6c4665672` is currently rendering. The full historical matrix is intentionally not marked complete while its sweep is active.
