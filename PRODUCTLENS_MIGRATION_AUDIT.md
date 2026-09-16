# ProductLens Migration Audit

## Purpose

This is the dependency-safe cleanup manifest for the AI-native demo-engine migration. It records what may be removed, what must be retained, and what will be replaced. A path is not deleted merely because it is old or has a narrow test; removal requires both a dependency check and a replacement or explicit retirement decision.

## Baseline inventory

- Backend production modules: 101 Python files.
- Backend test modules: 73 `test_*.py` files.
- Repository tracked files: 228 at audit start.
- The worktree already contains substantial user/project changes from earlier iterations. They are preserved unless this migration explicitly supersedes them.

## Retain

| Area | Reason |
|---|---|
| `backend/app/persistence/`, Alembic, jobs, workers | Durable run lineage, async execution, migration and audit foundations remain required. |
| `contracts/`, `artifacts/`, `browser/`, `credentials/`, `auth/` | Evidence, trace, secret handling and safe execution are core reliability boundaries. |
| Browserbase and Playwright providers/executor | The new engine will use these as cloud browser infrastructure and deterministic action authority. |
| Remotion/FFmpeg rendering path | The target architecture still renders layered real-footage presentations. |
| Side-effect policy, synthetic data, rehearsal boundaries | Required to support form workflows without blind replay or unsafe mutations. |
| User-provided plans, `samples/`, test HTML fixtures | Reference/acceptance material; not generated clutter. |
| Generic provider, QA, and contract tests | Preserve while consolidating duplicate or obsolete tests. |

## Replace or consolidate

| Area | Migration direction |
|---|---|
| Observation-only Stagehand bridge | Replace with a bounded, safety-aware Stagehand exploration bridge supporting observe, structured extraction and explicitly authorised semantic actions. Playwright remains the evidence authority. |
| Optional `stagehand_assist` request switch | Replace with automatic cloud exploration enrichment and explicit provider-attempt artifacts. Provider failure must be visible, never silently reduce demo quality. |
| Plan-first route/operation capture | Replace with an evidence-backed conditional capture plan and actual-state trace; preserve preconditions, postconditions and side-effect policy. |
| Timestamp/heuristic-driven presentation | Replace with state-anchor-driven caption, cursor, scroll, camera and transition timing. |
| Narrow patch regressions | Consolidate into generic contracts, representative fixtures, golden traces and golden video QA. |
| Diagnostic one-off scripts | Retain only scripts that exercise a supported operational path; fold redundant inspection logic into a documented audit/benchmark command. |

### Removed in this cleanup checkpoint

- `backend/run_live_demo.py` — obsolete root runner that bypassed the durable
  staged URL pipeline and exposed the retired `stagehand_assist` switch.
- `backend/rerender_artifact.py` — obsolete rerender path that regenerated
  generic event-label narration instead of using the persisted editorial
  storyboard and actual-flow trace. Use `backend/scripts/rerender_editorial.py`.

## Generated material eligible for removal after baseline capture

These paths are already ignored and are not source of truth:

- Root `artifacts/` run output and video-inspection images.
- Backend caches: `.cache/`, `.pytest_cache/`, `.ruff_cache/`, `.remotion/`, `.stagehand-cache/`.
- Backend local databases, temporary viewport screenshots and run directories.
- Frontend `.next/`, `node_modules/` and TypeScript build info.

Before removal, retain at most one explicitly labelled baseline trace/video/report needed for before/after benchmark comparison. No artifact is retained merely because it is a failed or empty run.

## Do not remove without a later explicit audit decision

- Database migrations or repository schema code.
- User-authored documents and sample/reference media.
- `.env` files are secrets and must never be inspected, copied, committed or treated as cleanup candidates in reports.
- A test solely because its name is product-specific: first determine whether its invariant should become a generic fixture contract.

## Immediate migration finding

The previous Stagehand Node bridge called only `stagehand.observe()` and returned advisory candidates. It now also uses schema-bound `extract()` for visible sections, meaningful controls, and safe next actions. The Python discovery layer accepts only text that is present in the current Playwright-visible page before adding it to page knowledge. Cloud discovery performs this enrichment automatically whenever Stagehand is configured; local fixtures retain an explicit opt-in.

Bounded semantic action exploration is now available for cloud discovery. It can replay one pre-observed action only after ProductLens proves it is a visible same-origin link, tab, or disclosure and rejects side-effecting language. The post-action Playwright DOM is re-inspected before knowledge is updated. Free-form agent tasks, credentials, form values, and production actions remain outside this bridge.

## Audit checkpoint — 11 September 2026

- The genericity audit scanned all 102 runtime Python modules and found no acceptance-project names, URLs, routes, or narration phrases in runtime code.
- The current durable staged runner is the migration path. The unreachable legacy monolithic block in `services/generation.py` has been removed; rollback is represented by the preserved git baseline and immutable run artifacts rather than dead runtime code.
- No user samples, HTML fixtures, persisted migration history, secrets, or ambiguous artifacts were removed.
- Generated caches/artifacts remain eligible for later deletion under the policy above, after a labelled before/after baseline is retained.
- Reusable knowledge snapshots now persist the typed `ProductKnowledge` contract with a content fingerprint; cached routes/actions are ignored when the live opening-page fingerprint changes.
- Discovery now persists `discovery/relevance-graph.json`, linking observed pages, features, and controls with evidence-backed relationships for reviewable candidate selection.
- Production traces now retain the measured source frame rate, locked viewport/browser scale, and a non-secret context identity for downstream visual QA.
- Editorial planning now filters table/grid rows, column-sort controls, sensitive record values, and anonymous discovery placeholders structurally; dense authenticated workspaces fall back to an evidence-backed page hold instead of narrating a customer row or modal residue.
- Stagehand advisory extraction failures are recorded as diagnostics while valid observed actions remain available for Playwright re-grounding; a model/schema miss no longer discards the entire observation.
- Action grounding now prefers an observed control selector over adjacent label text, preventing form labels from being mistaken for their inputs across products.
- Theme presentation now discovers a visible semantic control from its metadata instead of assuming a product-specific button label.
- Presentation retries now rebuild the camera/cursor plan from the immutable trace, so renderer safety fixes cannot be bypassed by stale persisted zoom decisions.
- Authentication observers re-ground a visible email/user control using generic input semantics; field-level redaction can preserve the real login UI and typing without retaining credential values. Render promotion also falls back to a byte-preserving copy when Windows holds the prior MP4 open.
- Browserbase session creation now receives the locked recording viewport and non-secret run metadata up front, keeping native Session Replay geometry aligned with the presentation contract. `backend/scripts/resume_run.py` provides a non-polling supervisor path over the same durable staged URL service for runs created before a root job ledger existed.
- Editorial QA now rejects route-mechanics prose such as “the next view” or “the current working view” even when it contains enough page words to pass a shallow overlap check. This prevents a click-success trace from being presented as an explanation.
- Cloud delivery now records `qa/exploration-report.json` and blocks publication when the required Stagehand observation is unavailable. Stagehand remains advisory and Playwright remains authoritative, but the AI discovery layer can no longer disappear silently behind a deterministic fallback.
- The Stagehand bridge normalizes product origins across standard ports and HTTP-to-HTTPS redirects before selecting or validating its attached page, avoiding false origin failures and duplicate opening navigation. The detached resume supervisor writes a terminal status snapshot without requiring a polling client.
- Discovery now falls back to bounded action-oriented body lines when a dashboard has no semantic `main` container or rich card nodes. This keeps page purpose evidence available for editorial narration instead of accepting date chips or route labels as the page explanation.

## Audit checkpoint — 12 September 2026

- Completion QA now validates the persisted `selected_candidate_flow` against
  the executable `workflow_steps` (canonical page URLs, outcomes, semantic
  step count, workflow name, and evidence coverage). Stale summaries are
  reported as `SELECTED_CANDIDATE_ARTIFACT_MISMATCH` and cannot count as a
  deliverable.
- A dry-run `backend/scripts/classify_artifacts.py` classifier was added for
  retention review. Its current pass identifies 63 clean deliverable runs,
  624 evidence-bearing runs, 107 stale-plan runs, 117 partial runs, and 7
  empty directory trees. It never deletes artifacts; ambiguous or
  lineage-bearing material remains for an explicit review.
- The static suite passes 428 tests (9 integration tests deselected), and the
  runtime genericity audit remains green across 104 runtime modules.
- Plan consistency is now part of the URL delivery gate itself: QA writes
  `qa/plan-consistency-report.json` before publication and includes any
  candidate/execution mismatch in the delivery hard-failure set. The suite now
  passes 432 tests after this gate and its regression coverage.

## Audit checkpoint — 12 September 2026 (completion pass)

- The SmartSevak Lead V3 run `4f79da48-21f3-4215-bbf1-9a4d850c0b77` now has a
  complete evidence audit: every discovery, plan, trace, presentation,
  narration, QA, manifest, and final-video layer is present and consistent.
  The run was repaired from its retained trace; no browser action was replayed
  for this evidence repair.
- QA retries now reconcile `qa/execution-report.json` from the immutable trace,
  preventing an earlier interrupted-attempt marker from surviving a successful
  targeted retry. Mutable `run-status.json` is excluded from the checksum
  manifest so lifecycle checkpoints cannot invalidate an otherwise immutable
  delivery.
- A fresh generic gate-5 fixture render passed at 1920×1080/30fps. Short
  primitive-fixture caption windows are reported as a warning rather than
  being mistaken for URL-production editorial failures; live URL runs retain
  the strict reader-dwell gate.
- Seven verified-empty artifact trees were removed after confirming they were
  inside the run root and contained no files. Evidence-bearing, stale-plan,
  partial, and deliverable runs remain retained for lineage review.
- The final dry-run retention inventory is 71 deliverables, 645 evidence runs,
  107 stale-plan runs, and 117 partial runs; no empty run trees remain.

## Audit checkpoint — 12 September 2026 (editorial refresh)

- The deterministic evidence narrator was tightened for arbitrary dashboards:
  highlighted task cards are rewritten into presenter copy, implementation
  storage details are excluded from viewer captions, collection summaries keep
  page context, and category landmarks explain their role without reciting
  accessibility inventories.
- Study Plan trace `ee0b6692-8f8e-46e5-912d-2be69e50b349` was refreshed from
  its immutable execution trace; no browser actions were replayed. Editorial
  preflight and editorial QA both pass with no hard failures, and the persisted
  script now includes a welcome, Today, Weeks, DSA, Builds, and Progress
  explanations.
- The regression suite covering editorial contracts and narration passes 48
  tests. Rendering the refreshed script remains a separate presentation gate;
  the existing MP4 is not promoted until that render and visual review pass.

## Audit checkpoint — 12 September 2026 (caption composition)

- Manual review of the corrected Study render found a lower-page caption
  covering table evidence. Caption-safe-zone selection now accounts for the
  continuation region below scroll landmarks, and trace-only retries rebuild
  the scene plan so the policy cannot remain stale.
- Scene-plan and editorial regressions pass; the Study render was restarted
  from the existing trace with the repaired composition. The prior MP4 remains
  retained for comparison and is not treated as the final promoted artifact.

## Audit checkpoint — 12 September 2026 (trace-only renderer and full-page coverage)

- Standalone trace-only rerenders now pass the validated scene contract into
  Remotion. Previously this path rendered an empty scene list, silently
  discarding repaired caption-safe zones and camera policy.
- Narration repairs atomically refresh `validated-scene-plan.json` alongside
  `scene-plan.json`, keeping captions, camera, cursor, and journey artifacts
  aligned to the same immutable trace.
- Full-walkthrough planning now retains every meaningful local group on later
  pages (including distinct roles/experiences) while calculating a shared
  operation budget, instead of sampling only the first and last group.
- New scene/planning regressions pass; runtime genericity audit remains green.
- The refreshed Portfolio render now completes at 1920×1080/30fps for 187.989
  seconds and passes provider-free delivery/completion audit after manifest
  regeneration. This is a presentation repair of the retained trace; it does
  not claim that the old trace contained the internship interaction that it
  never recorded.

## Audit checkpoint — 12 September 2026 (acceptance continuation)

- The non-integration regression suite now passes **445 tests** (9 external/live
  tests intentionally deselected); this includes discovery, planning, Stagehand
  boundaries, execution recovery, narration grounding, scene composition,
  synchronization, rendering, repair, and persistence contracts.
- A newly executed unseen local presentation fixture gate
  (`294b7e3a-0ca3-4e02-8c62-985cb1da4f13`) completed with verified profile,
  gradual scroll, row edit, modal open, and modal close events. It exercised
  the current cursor/scroll/overlay execution path without external providers.
- Study Plan trace `ee0b6692-8f8e-46e5-912d-2be69e50b349` was atomically
  rerendered with the validated scene plan after the caption-safe-zone repair.
  The promoted MP4 is 1920x1080 at 30fps and 117.525 seconds; standalone
  verification and completion audit report no missing layers or consistency
  failures. Manual visual review of corrected frame samples remains a required
  final acceptance check.

## Audit checkpoint — 12 September 2026 (destination caption safety)

- Destination-page navigation scenes now default to the upper caption zone,
  because their narration describes the page that has just loaded rather than
  the small tab that was clicked. Geometry guards in both the scene planner
  and Remotion protect dense upper-page headings and lower result rows during
  stale-artifact recovery.
- Numbered/representative page items are narrated with their purpose and
  tracked-work context instead of a title-only “highlights” line. The new
  editorial regression and the full non-integration suite pass **448 tests**
  (9 external/live tests deselected).
- The final Study rerender is running from the refreshed script and current
  compositor; its previous valid MP4 remains retained until this encode and
  its visual/completion audits finish.

- Compact controls that introduce a dense evidence region now inherit the
  upper caption zone when required content groups are present; this covers
  filters and selectors as well as broad headings without naming any product
  or route. Remotion mirrors the same rule at render time, and its TypeScript
  build passes.

## Audit checkpoint — 12 September 2026 (planning preflight retry boundary)

- Editorial preflight evidence counting now normalizes captured DOM/accessibility
  text before measuring readable local evidence. This fixes false rejection of
  valid all-caps category landmarks (for example a visible technology-stack
  group) without weakening the title-only/generic narration gate.
- The live supervisor no longer marks a run `COMPLETE` when a failed stage was
  silently skipped; failed checkpoints now remain explicit repair boundaries.
- The regression suite passes **449 tests** (9 external/live tests deselected).
- Portfolio acceptance run `225648b6-4e77-47e3-8bc6-d638a776f5b9` remains the
  retained failed-parent evidence. Its targeted planning retry
  `476d0ffe-75b7-488c-903b-a20ebb7d109c` passed planning and is currently in
  independent Browserbase production execution; no browser actions were
  replayed during the repair.
- The added case-insensitive evidence regression raises the non-integration
  total to **450 tests** (9 external/live tests deselected). TypeScript and
  the genericity audit remain green.

## Audit checkpoint — 12 September 2026 (duration-aware evidence selection)

- Complete-page planning now distributes a bounded 24-action budget across
  discovered pages instead of allowing up to sixteen remote scroll groups per
  page. Rich pages retain the opening plus the highest-value observed groups;
  semantic role/project/detail cues outrank blind even-spacing, so secondary
  career roles and representative work are not silently skipped.
- This is a generic evidence-value policy, not a target-site recipe; all
  discovered content remains in PageKnowledge for narration and audit.
- Evidence-planner and production-planning regression tests pass after the
  change; the in-flight Portfolio capture is intentionally not mutated.
- The full non-integration suite now passes **452 tests** (9 external/live
  tests deselected). A regenerated Portfolio plan is 20 semantic operations
  with both career-role and internship evidence retained before rendering.
- Representative-group scoring now weights observed semantic role/project/
  delivery cues, so the same bounded selection keeps concrete work and career
  evidence ahead of generic spacing landmarks. The full suite remains green at
  **452 passed**.

## Audit checkpoint — 12 September 2026 (Portfolio acceptance and descriptive fallback)

- Portfolio run `8a96fe17-f30a-44ab-83c2-81f8823a3871` completed all durable
  stages. Its 153-second 1920×1080 native-speed render passed execution,
  editorial, visual, synchronization, multimodal, delivery, and completion
  audits with no hard failures.
- Manual frame review confirmed the opening greeting, full browser frame,
  Home/Timeline/System Designs/Engineering Notes/Contact coverage, and
  readable evidence-backed captions. ElevenLabs remains optional; this output
  is caption-led.
- A generic category-landmark fallback was tightened so a descriptive local
  fact (for example a challenge or caching explanation) is selected before a
  structural collection sentence. The focused editorial suite passes **48**
  tests and the full non-integration suite passes **453** tests.
- A provider-free rerender was started from the retained Portfolio trace so the
  corrected narration is reflected in the final MP4 without replaying browser
  actions.

## Audit checkpoint — 12 September 2026 (final acceptance)

- Portfolio corrected render is promoted at
  `backend/artifacts/runs/8a96fe17-f30a-44ab-83c2-81f8823a3871/final/demo.mp4`.
  It is 144.832 seconds, 1920×1080 at 30 fps, with 4,343 frames.
- Provider-free verification reports `deliverable: true`, complete evidence,
  no hard failures, and synchronized captions. Manual frame review is persisted
  in `qa/manual-review.json` and confirms the opening, complete section order,
  full frame, native site appearance, and corrected descriptive captions.
- Study Plan, authenticated SmartSevak, and the unseen generic fixture each
  retain a complete render, delivery report, and completion audit; representative
  frames were manually reviewed. The fixture is explicitly generic and carries
  only a semantic-review caveat because it has no configured visual model.
- The final non-integration regression gate remains **453 passed** (9 live
  tests intentionally deselected); compilation, TypeScript, and genericity
  audits remain green. ElevenLabs is optional and not required for the approved
  caption-led delivery.

## Audit checkpoint — 12 September 2026 (cross-run audit closure)

- Provider-free verification was rerun for Study Plan and SmartSevak after
  persisting their manual-review records. Portfolio, Study Plan, SmartSevak,
  and the unseen generic fixture now each report `deliverable: true`,
  `complete_evidence: true`, no missing layers, no hard delivery failures, and
  a persisted `qa/manual-review.json`.
- The three database-backed live runs are durably `COMPLETE` with every
  discovery, planning, execution, narration, render, and video-QA stage
  complete. The fixture's artifact-only acceptance audit is also complete.
- Final video properties are 1920×1080 at 30 fps: Portfolio 144.832s, Study
  Plan 117.547s, SmartSevak 83.925s, and unseen fixture 67.179s. ElevenLabs
  remains the only intentionally optional layer.
