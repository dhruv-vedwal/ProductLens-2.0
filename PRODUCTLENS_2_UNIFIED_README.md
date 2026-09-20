# ProductLens 2.0 — Unified Architecture, Reliability & Implementation README

> **Purpose:** This is the single implementation document for ProductLens 2.0.
> It consolidates the existing ProductLens architecture with the engineering
> decisions and problems discussed during the recent interaction-engine,
> progressive-discovery, verification, scene-processing, synchronization, and
> whole-pipeline reviews.
>
> **Primary audience:** the engineer/agent implementing and refactoring the
> ProductLens codebase.
>
> **Important:** This document defines the target engineering architecture and
> implementation priorities. Some pieces already exist in the repository;
> others are improvements that must be implemented or strengthened. Do not
> assume that an existing module is correct merely because it exists.

---

# 1. ProductLens in one sentence

**ProductLens turns a web application URL plus a natural-language objective into a truthful, evidence-grounded product demo by discovering the application, planning a workflow, reliably interacting with the browser, independently verifying the outcome, converting the verified workflow into production scenes, synchronizing narration/captions/audio with those scenes, rendering the video, and running layered QA.**

The core product is not the video renderer.

The core technical capability is:

```text
URL + Objective
      ↓
Understand the application
      ↓
Discover the relevant workflow
      ↓
Interact with the application
      ↓
Verify that the intended state actually happened
      ↓
Produce a Verified Trace
      ↓
Turn the Verified Trace into a high-quality demo
```

The most important engineering principle is:

> **ProductLens must never present an action as successful unless the application state provides sufficient independent evidence that it actually succeeded.**

---

# 2. Why this README exists

The ProductLens codebase has grown substantially and now contains:

- discovery
- product/page knowledge
- planning
- rehearsal
- Stagehand integration
- Playwright execution
- target grounding
- outcome verification
- DemoTrace
- presentation planning
- narration
- captions
- optional ElevenLabs audio
- Remotion/FFmpeg rendering
- synchronization QA
- visual QA
- delivery QA
- persistence
- durable workers
- repair/retry logic
- frontend/API layers

The architecture is conceptually strong, but the main remaining difficulty is that the **browser interaction capability is still probabilistic**, especially for unfamiliar applications and state-dependent interfaces.

At the same time, downstream presentation processing can fail independently:

- captions can drift from actions;
- narration can describe the wrong thing;
- a large video can fail late in rendering;
- a single bad scene can invalidate an otherwise valid workflow;
- ElevenLabs may be unavailable;
- rendering the entire video as one monolithic operation makes repair expensive.

Therefore ProductLens must be treated as **two related systems**:

## Truth pipeline

```text
Discovery
→ Planning
→ Interaction
→ Verification
→ Verified Trace
```

## Presentation pipeline

```text
Verified Trace
→ Scene Production
→ Narration
→ Audio Timing
→ Sync Engine
→ Scene Rendering
→ Scene QA
→ Final Composition
→ Video QA
```

The truth pipeline must be stable before the presentation pipeline can be trusted.

---

# 3. Core product principles

## 3.1 No false demos

A rendered MP4 is not proof that an interaction succeeded.

Evidence must flow from the browser state into the trace and then into presentation.

```text
Browser evidence
    ↓
Verified event
    ↓
Verified trace
    ↓
Scene
    ↓
Narration/caption
    ↓
Rendered video
```

Never:

```text
LLM assumption
    ↓
Narration
    ↓
Video
```

---

## 3.2 Current browser state beats stored knowledge

Stored product knowledge is useful for guidance, but it is not authoritative.

The priority is:

```text
Current browser observation
    >
Fresh rehearsal evidence
    >
Fresh discovery evidence
    >
Stored knowledge
    >
LLM assumption
```

If the current browser differs from historical knowledge, the current browser wins.

---

## 3.3 Probabilistic components propose; deterministic components validate

LLMs, Stagehand, semantic planners, and editorial models can:

- propose actions;
- interpret intent;
- rank candidates;
- generate narration;
- suggest targets;
- propose recovery.

They must not be the final authority for:

- whether a click occurred;
- whether a mutation succeeded;
- whether a field was actually filled;
- whether a workflow outcome exists;
- whether a trace event is valid;
- whether a video artifact belongs to a run;
- whether captions are synchronized;
- whether delivery is allowed.

Those decisions require deterministic evidence and explicit gates.

---

## 3.4 No website-specific recipes

The generic engine must not contain:

- SmartSevak-specific workflows;
- Portfolio-specific selectors;
- Excalidraw-specific coordinates;
- n8n-specific node names;
- hardcoded route names;
- hardcoded field names;
- fixed grid coordinates;
- application-specific workflow branches.

Product-specific behavior must come from:

- current page evidence;
- accessibility evidence;
- DOM evidence;
- screenshots/geometry;
- objective text;
- Stagehand observations;
- rehearsal;
- validated run-local workflow data;
- explicit user authorization.

A special-case branch is acceptable only when it is a generic browser/provider safety boundary.

---

## 3.5 Fail closed

If ProductLens cannot prove an action or outcome, it should:

1. classify the failure;
2. preserve the evidence;
3. stop or recover within explicit bounds;
4. offer a read-only or human-assisted path where appropriate.

It must not guess.

---

# 4. Target architecture

The target pipeline is:

```text
REQUEST
  │
  ▼
OBJECTIVE NORMALIZATION
  │
  ▼
DISCOVERY
  │
  ├── product/page knowledge
  ├── accessibility/DOM evidence
  ├── state-dependent UI exploration
  ├── safe probes
  └── blockers/auth state
  │
  ▼
PLANNING
  │
  ├── candidate workflows
  ├── validated state graph
  ├── rehearsal for authorized mutations
  └── storyboard/editorial brief
  │
  ▼
INTERACTION / EXECUTION
  │
  ├── observe
  ├── resolve target OR explore
  ├── safety/preconditions
  ├── dispatch once
  ├── observe after-state
  └── independently verify
  │
  ▼
VERIFIED TRACE
  │
  ▼
SCENE SEGMENTATION
  │
  ├── verified scene units
  ├── evidence
  ├── timing
  └── retry boundaries
  │
  ▼
NARRATION
  │
  ├── evidence-grounded script
  └── captions
  │
  ▼
AUDIO / TTS
  │
  └── optional ElevenLabs
  │
  ▼
SYNC ENGINE
  │
  ├── trace timing
  ├── audio timing
  ├── caption timing
  ├── scene timing
  ├── cursor/action timing
  └── visual beat timing
  │
  ▼
SCENE RENDERING
  │
  ├── Browser
  ├── Camera
  ├── Cursor
  ├── Click effects
  ├── Highlights
  ├── Captions
  └── Audio
  │
  ▼
SCENE QA
  │
  ▼
FINAL COMPOSITION
  │
  ▼
VIDEO QA / DELIVERY
```

---

# 5. Existing durable stage architecture

The existing ProductLens architecture uses durable stages:

```text
DISCOVERY
PLANNING
EXECUTION
NARRATION
RENDER
VIDEO_QA
```

These remain the durable orchestration boundaries.

The new concepts introduced by this README do **not** require creating an
unnecessary dozen durable jobs.

Instead:

```text
PLANNING
  └── rehearsal + workflow certification

EXECUTION
  ├── interaction engine
  ├── progressive exploration
  ├── verification
  ├── verified trace
  └── scene segmentation

NARRATION
  ├── editorial script
  ├── captions
  └── optional TTS

RENDER
  ├── scene-level rendering
  ├── sync composition
  └── final composition

VIDEO_QA
  ├── scene QA
  ├── synchronization QA
  ├── visual QA
  └── delivery aggregation
```

Do not create durable stages merely to represent internal functions.

---

# 6. The most important change: isolate the interaction engine

The biggest current engineering problem is that too much of the system can fail before we know whether the browser agent itself is capable of completing the requested workflow.

Therefore ProductLens needs a development/test mode that stops at:

```text
URL
+
Objective
↓
Interaction Agent
↓
Verified Trace
```

No:

- ElevenLabs
- narration
- Remotion
- final MP4
- multimodal video review

is required for interaction development.

## Interaction harness

Create/maintain a dedicated interaction harness capable of:

1. opening an application;
2. authenticating;
3. discovering the current state;
4. executing the objective;
5. verifying each critical action;
6. producing a complete trace;
7. stopping.

Example:

```text
SmartSevak
→ Leads
→ Add Lead
→ reveal form
→ fill observed fields
→ submit
→ verify created entity
→ VERIFIED TRACE
```

The harness should answer:

> **Can ProductLens reliably accomplish this task?**

before the video pipeline is involved.

## 6.1 Interaction harness is an executable contract

The interaction harness is not a shortened video job and it is not a browser
recording with a different flag. It is an independently runnable capability test
whose only success output is a verified interaction trace. Narration, captions,
Remotion, audio, scene composition, and video QA must not participate in this
mode.

The same interaction engine must be used by exploration, rehearsal, and
production. The difference between those modes is the purpose and the recording
policy, not a second implementation of browser actions.

### Harness request

Expose one stable API/CLI contract. The HTTP form is illustrative; an equivalent
worker or command-line entry point is acceptable, but it must produce the same
schemas and artifacts.

```json
{
  "run_id": "uuid",
  "url": "https://example.test",
  "objective": "Create one representative lead and show the resulting details",
  "mode": "capability | certification | production_trace",
  "audience": "optional audience description",
  "auth_reference": "secret reference only; never credentials",
  "side_effect_policy": "read_only | reversible | explicitly_authorized",
  "limits": {
    "max_steps": 24,
    "max_pages": 6,
    "max_exploration_steps": 12,
    "max_model_calls": 3,
    "max_duration_seconds": 900
  },
  "required_outcomes": [
    "a created lead is visible with the requested values"
  ],
  "excluded_actions": [
    "send external messages"
  ]
}
```

`limits` are configurable run policy, not application-specific behavior. A
workflow may request a larger budget only when the objective and relevance
evidence justify it. The harness must stop with a typed budget failure rather
than silently crawling or looping.

Required entry points:

```text
POST /api/v2/interaction-harness/runs       start a trace-only run
GET  /api/v2/interaction-harness/runs/:id   status and current stage
GET  /api/v2/interaction-harness/runs/:id/trace
POST /api/v2/interaction-harness/runs/:id/cancel
```

The CLI/worker equivalent must support the same request and result contracts.
Starting a harness run must be idempotent for the same request fingerprint; a
retry must resume from a safe observation checkpoint or create a new lineage,
never blindly repeat a dispatched mutation.

The supervised local entry point is:

```text
python -m scripts.run_interaction_harness --url https://example.test \
  --objective "Show the requested workflow" --mode capability
```

It uses the same durable worker stages and stops after the trace certificate;
it does not provide a shortcut around the API safety or verification gates.

### Harness result

```json
{
  "run_id": "uuid",
  "status": "VERIFIED | BLOCKED | FAILED | CANCELLED",
  "objective_fingerprint": "sha256",
  "trace_artifact": "execution/trace.json",
  "outcome": {
    "verified": true,
    "evidence_ids": ["ev-004", "ev-005"],
    "summary": "The created lead is visible with the requested values"
  },
  "metrics": {
    "steps": 9,
    "pages": 2,
    "model_calls": 2,
    "duration_seconds": 84.2
  },
  "failure": null
}
```

`VERIFIED` is impossible without an independent outcome witness. A run that
loaded the URL, clicked buttons, changed the URL, or produced a recording but
did not prove the requested outcome is not successful.

### Canonical interaction state

Every decision is made from an immutable `InteractionSnapshot`. At minimum it
contains:

```text
snapshot_id
timestamp
canonical_url and route state
viewport and browser scale
document readiness/loading state
visible text and semantic regions
accessibility tree
DOM affordances and stable attributes
target geometry and occlusion information
open overlays, dialogs, menus, drawers, and focus
form values, validation state, and dependent-field state
relevant screenshot reference
previous snapshot id and state fingerprint
```

Stored product/page knowledge may propose context, but the current snapshot is
authoritative. A stale selector, route, form schema, or canvas coordinate must be
re-grounded against the current snapshot before use.

### Action protocol

The model proposes a semantic intent, not a selector or coordinate. The
deterministic resolver converts that intent into a currently visible target and
the dispatcher performs the lowest-level safe operation available from the
provider.

```text
ActionCandidate
  intent                 human-readable goal
  target_semantics       role, name, label, relation, or visual description
  action_kind            click, type, select, scroll, hover, drag, draw, keypress, wait
  expected_precondition
  expected_postcondition
  evidence_ids
  confidence and ambiguity
  side_effect_class
```

These action kinds are provider-neutral dispatch capabilities, not website
recipes. New UI behavior must be discovered and verified through the current
state; it must not be added as an `if website == ...` branch. A provider may
implement a capability differently, but the harness contract and verification
semantics stay stable.

### Mandatory state machine

Each action follows this exact lifecycle:

```text
OBSERVE
  -> UNDERSTAND current state
  -> RESOLVE a unique visible target
  -> CHECK safety and preconditions
  -> DISPATCH exactly once
  -> CAPTURE after-state
  -> VERIFY with an independent witness
  -> RECORD attempt, result, and evidence
  -> RE-OBSERVE
  -> choose the next intent
```

Invariants:

1. No target resolution means no dispatch.
2. Ambiguous or occluded targets fail closed; coordinates are never guessed.
3. A dispatched side effect is never replayed automatically.
4. A successful dispatch is not a successful action until its postcondition is
   independently verified.
5. Every critical action has pre-state, dispatch result, post-state, and
   verification evidence.
6. Every next decision uses a new observation after a meaningful state change.
7. Exploration actions and objective actions are separately labelled in the
   trace; exploration cannot be narrated as completed product behavior.

### Progressive discovery policy

When the intended target is not visible, the harness may perform bounded,
relevant exploration: open a visible navigation control, expand a relevant
menu, dismiss a blocking overlay, scroll to a semantic landmark, or inspect a
nearby detail state. It must then re-observe and re-plan.

Exploration is allowed only when:

- it has a stated reason connected to the objective;
- it is safe under the side-effect policy;
- it stays within the configured budget;
- it records the action and resulting state;
- it does not mutate data merely to discover a control.

If the target remains ambiguous after the budget is exhausted, return
`TARGET_UNRESOLVED` or `EXPLORATION_BUDGET_EXCEEDED`; do not improvise.

### Independent verification requirements

Verification must use a witness different from the dispatch mechanism where
possible. Examples:

| Intended result | Acceptable witness | Insufficient alone |
|---|---|---|
| navigation | canonical route plus visible page landmark | URL change only |
| dialog opened | visible dialog role/title and expected fields | click returned successfully |
| field filled | value observed in the intended field plus valid state | `fill()` completed |
| dropdown selection | selected value plus dependent state/update | option click only |
| record created | visible entity/detail row with requested values or success state | toast alone |
| booking submitted | resulting booking/status evidence with selected slot | submit click or spinner |
| canvas drawing | expected object/relationship visible in canvas state or exported model | pointer movement |
| close/dismiss | overlay absent and underlying target readable | Escape dispatched |

The verifier must return `PASS`, `FAIL`, or `INCONCLUSIVE` with evidence IDs.
`INCONCLUSIVE` blocks certification and production recording.

### Trace schema and artifact contract

Each harness run persists the following, independently of video artifacts:

```text
interaction-run.json
observations/000001.json ...
observations/screenshots/*
observations/accessibility/*
actions.jsonl
action-attempts.json
verification-results.json
state-transitions.json
failure.json                 (only for BLOCKED/FAILED/CANCELLED)
execution/trace.json
artifact-manifest.json
```

Every trace event contains:

```text
event_id, scene_candidate_id, observation_before, observation_after,
intent, action_kind, resolved_target, geometry, dispatch timestamps,
loading interval, result, verification, evidence_ids, provider metadata,
error classification, and lineage/retry reference
```

Secrets, cookies, passwords, tokens, and raw authorization headers must never
appear in snapshots, screenshots, prompts, traces, logs, or analytics.

### Failure taxonomy and ownership

The harness must emit a specific failure instead of `GENERATION_FAILED`:

```text
URL_LOAD_FAILED
AUTH_REQUIRED / AUTH_EXPIRED / CAPTCHA_BLOCKED / OTP_REQUIRED
TARGET_NOT_VISIBLE
TARGET_AMBIGUOUS
TARGET_OCCLUDED
UNSAFE_ACTION
PRECONDITION_FAILED
DISPATCH_FAILED
STATE_DID_NOT_CHANGE
POSTCONDITION_FAILED
OUTCOME_UNVERIFIED
EXPLORATION_BUDGET_EXCEEDED
STEP_BUDGET_EXCEEDED
PROVIDER_TIMEOUT / PROVIDER_UNAVAILABLE
TRACE_INCOMPLETE
```

Each failure names the owning boundary, last verified event, safe retry point,
and whether a dispatched side effect makes retry unsafe. The repair system may
retry only from that safe boundary.

### Capability modes and promotion gates

The harness has three modes:

1. `capability`: local or mocked provider, trace only; used during development.
2. `certification`: fresh sessions and repeated runs; proves the objective is
   reproducible before production recording.
3. `production_trace`: fresh clean session, authoritative trace capture, and
   optional browser video; still stops before narration/rendering if the trace
   is not verified.

Certification requires:

- all critical steps verified;
- the required outcome witnessed;
- no unresolved ambiguity or inconclusive verification;
- no unsafe side effects;
- complete trace and artifact manifest;
- repeated fresh-session attempts meeting the configured reliability policy;
- a failure report for every rejected attempt.

Only a certified trace may enter the presentation pipeline. A video render can
never promote an uncertified interaction.

### Harness test matrix

The next implementation goal must test the harness independently in this order:

```text
1. Observation and canonical URL/state fingerprints
2. Target resolution and ambiguity rejection
3. Dispatch-once and retry safety
4. Independent verification for navigation, forms, selects, dialogs, and results
5. Progressive discovery of hidden menus, drawers, dependent fields, and overlays
6. Dynamic/custom controls and canvas/graph state witnesses
7. Authentication, CAPTCHA, timeout, and blocked-state classification
8. Repeated fresh-session certification on known fixtures
9. Changed-layout and interrupted-session fixtures
10. Live capability runs on representative unfamiliar applications
```

The harness test command must not invoke narration, ElevenLabs, Remotion, final
composition, or video QA. Full-pipeline tests begin only after the harness has
produced a verified trace and a certification report.

### Relationship to the rest of ProductLens

Discovery and planning may propose objectives, facts, candidate workflows, and
semantic intents. The interaction harness is the authority that proves whether
those proposals work in the current browser state. The verified trace then feeds
scene segmentation, narration, synchronization, rendering, and delivery QA.

No downstream layer may repair, reinterpret, or narrate a failed harness run.

```text
Objective / discovery / planning
              |
              v
      Interaction harness
              |
       VERIFIED TRACE only
              |
       scene + narration + sync
              |
          final video
```

---

# 7. Interaction engine

The interaction engine is the core technical capability of ProductLens.

Its fundamental loop is:

```text
OBSERVE
   ↓
UNDERSTAND CURRENT STATE
   ↓
TARGET VISIBLE?
   ├── YES → RESOLVE TARGET
   │
   └── NO  → PROGRESSIVE UI EXPLORATION
                ↓
             RE-OBSERVE
   ↓
SAFETY + PRECONDITIONS
   ↓
DISPATCH ONCE
   ↓
CAPTURE AFTER-STATE
   ↓
INDEPENDENT VERIFICATION
   ↓
RECORD RESULT
   ↓
RE-OBSERVE
   ↓
NEXT ACTION
```

Never implement the executor as:

```text
LLM says click X
→ click X
→ assume success
→ continue
```

---

# 8. Interaction state model

The interaction subsystem should have explicit representations for:

## InteractionSnapshot

Contains the currently observable state:

- URL;
- route;
- page title;
- visible text;
- headings;
- dialogs;
- menus;
- dropdowns;
- forms;
- inputs;
- buttons;
- links;
- accessibility tree;
- DOM evidence;
- bounding boxes;
- viewport;
- scroll position;
- focus;
- enabled/disabled state;
- selected values;
- loading indicators;
- relevant screenshot;
- iframe/shadow-root information;
- observed application state;
- recent interaction history.

---

## Affordance

Represents something the current UI allows the agent to do.

Examples:

```text
Button: "Add Lead"
Menu trigger: "More"
Combobox: "Status"
Tab: "Settings"
Input: "Email"
Canvas tool: "Rectangle"
Node handle
```

Affordances must be grounded in current evidence.

---

## ActionCandidate

Represents a proposed action:

```text
intent
target
action_type
evidence
preconditions
expected_state_change
expected_witness
risk
confidence
rationale
```

---

## ActionAttempt

Tracks the actual lifecycle:

```text
attempt_id
parent_action_id
before_state
target_resolution
dispatch
after_state
verification
result
error
recovery
timestamps
```

---

## OutcomeVerification

Contains:

```text
expected outcome
observed witness
witness type
matched evidence
confidence/strength
verification status
```

A verification record must explain **why** ProductLens believes the action succeeded.

---

# 9. Progressive UI discovery

One of the most important requirements is the ability to find controls that are not initially visible.

Example:

```text
User objective:
"Export the leads"
```

Initial page:

```text
Leads
├── Search
├── Add Lead
├── Filter
└── ⋮
```

Export is not visible.

The agent must not fail immediately.

It should reason:

```text
Target "Export" not visible
        ↓
Find relevant state-expanding affordances
        ↓
"⋮" menu is likely relevant
        ↓
Open menu
        ↓
Re-observe
        ↓
Export now visible
        ↓
Resolve Export
        ↓
Execute
        ↓
Verify
```

This is **goal-directed exploration**, not blind crawling.

---

# 10. UI state graph

The browser should be modeled as a state graph.

Example:

```text
Leads page
   │
   └── click More
          ↓
      More menu open
          │
          └── click Export
                 ↓
             Export dialog
                 │
                 └── choose CSV
                        ↓
                    export complete
```

The agent does not need to know this graph beforehand.

It discovers it incrementally.

Each state is identified from evidence such as:

- URL;
- visible text;
- accessibility tree;
- active dialog/menu;
- visible controls;
- relevant DOM;
- focus;
- screenshot;
- known application state.

---

# 11. Exploration versus target action

The system must distinguish:

## Target action

An action that directly advances the objective.

Example:

```text
Click "Export"
```

## Exploration action

An action whose purpose is to reveal the target or understand the current state.

Example:

```text
Open "More" menu
```

Exploration actions still require:

- semantic relevance;
- safety;
- boundedness;
- verification;
- re-observation.

This distinction is important for debugging and trace semantics.

---

# 12. Bounded exploration

The agent must never explore indefinitely.

Introduce explicit budgets:

```text
max exploration depth
max exploration actions
max candidate affordances per state
max repeated-state visits
max exploration time
max model calls
```

Stop exploration when:

- the target becomes grounded;
- the objective is completed;
- the state is blocked;
- exploration becomes irrelevant;
- the budget is exhausted;
- the same state repeats without progress.

The system should record:

```text
why exploration started
what candidates were considered
why a candidate was selected
what state resulted
whether the target became visible
```

This makes exploration debuggable rather than mysterious.

---

# 13. Re-observation is mandatory

After any state-changing action:

```text
OLD AFFORDANCES
      ↓
INVALID
      ↓
NEW OBSERVATION
      ↓
NEW AFFORDANCES
```

Do not reuse stale element assumptions.

This is especially important for:

- menus;
- dropdowns;
- modals;
- dynamic forms;
- SPA navigation;
- React components;
- virtualized lists;
- canvas tools;
- changing overlays.

---

# 14. Target resolution

Target resolution should combine:

1. accessibility role;
2. visible text;
3. label;
4. placeholder;
5. DOM attributes;
6. semantic relationship;
7. geometry;
8. current state;
9. screenshot evidence;
10. interaction history.

The executor should seek a **unique visible target**.

Ambiguity should produce:

```text
TARGET_AMBIGUOUS
```

rather than a random click.

Locators are re-grounded immediately before dispatch.

---

# 15. Browser primitives

The browser execution layer should provide generic primitives:

```text
click
type
fill
select
check
radio
keypress
hover
scroll
drag
pointer sequence
upload
wait
read
verify
```

These are not application workflows.

The intelligence layer decides:

```text
which primitive
+
which target
+
when
+
why
```

The deterministic browser layer performs the action.

---

# 16. Dispatch-once safety rule

For mutations:

```text
resolve
→ safety check
→ dispatch once
→ verify
```

If verification later needs a new locator, do **not** blindly replay the mutation.

Example:

```text
Submit clicked
↓
URL changed
↓
verification target cannot be located
```

This is not permission to click Submit again.

The system must classify the verification failure separately.

This protects against duplicate creates.

---

# 17. Outcome verification

The strongest possible outcome witness should be preferred.

For a create operation, possible witnesses include:

- navigation to a new entity;
- entity identifier;
- visible entity fields;
- success state;
- table row;
- detail page;
- URL transition;
- explicit confirmation.

A URL change alone may not be sufficient.

For example:

```text
POST /leads/123
```

does not prove the correct lead exists.

The system should inspect the settled page and look for demonstrated values.

For synthetic creation, the verifier can require multiple matching fields where appropriate.

Example:

```text
Name: Test User
Email: test@example.com
Phone: 9876543210
```

If two independent demonstrated values appear on the created entity, confidence is much stronger than merely observing `/leads/123`.

---

# 18. Rehearsal

Authorized mutation workflows require hidden rehearsal before production recording.

Rehearsal:

- uses a fresh browser session;
- does not record the deliverable;
- uses synthetic values;
- exercises the intended workflow;
- handles observed validation/dependencies;
- verifies the result independently;
- writes a certified workflow graph.

```text
Discovery
   ↓
Candidate create workflow
   ↓
Fresh rehearsal
   ↓
Successful interaction
   ↓
Independent outcome witness
   ↓
Certified workflow
   ↓
Production execution
```

A missing certified workflow should hard-fail an authorized create workflow.

---

# 19. Why rehearsal is expensive

If an interaction succeeds with probability `p`, requiring both rehearsal and production success gives approximately:

```text
p²
```

for two independent attempts.

Examples:

```text
70% interaction reliability → ~49% two-stage success
80% → ~64%
90% → ~81%
95% → ~90%
```

This is why the interaction engine must be stabilized independently.

Rehearsal should not be used to hide an unreliable executor.

---

# 20. Verified Trace

The `DemoTrace` / interaction trace is the bridge between truth and presentation.

It should contain enough information to reconstruct what happened without reopening the browser.

At minimum:

```text
run_id
scene/action id
action type
intent
target evidence
before timestamp
dispatch timestamp
after timestamp
URL
viewport
geometry
scroll state
before evidence
after evidence
verification evidence
success/failure
error/recovery
screenshot references
recording references
```

A trace event is only marked verified when its verification gate passes.

---

# 21. Scene-level production units

Do not treat the entire video as one monolithic processing unit.

Instead, convert the verified workflow into **independently processable scenes**.

Example:

```text
Workflow
│
├── Scene 1 — Open Leads
├── Scene 2 — Open Add Lead
├── Scene 3 — Fill Lead
├── Scene 4 — Submit Lead
└── Scene 5 — Verify Lead
```

Each scene should have:

```text
scene_id
objective
trace events
evidence
source media range
start/end timing
narration
caption units
cursor events
camera state
audio segment
sync metadata
QA status
retry boundary
```

This is the intended meaning of “batch processing”.

Do not simply chop an arbitrary MP4 into equal-duration chunks.

Batch by **verified semantic production unit**.

---

# 22. Why scene-level processing matters

Suppose:

```text
Scene 1 ✓
Scene 2 ✓
Scene 3 ✓
Scene 4 ✗
Scene 5 not attempted
```

The system should not have to regenerate everything.

It should be possible to:

```text
repair Scene 4
→ regenerate affected downstream scenes
→ recompute synchronization
→ recomposite final video
```

This gives ProductLens:

- smaller failure domains;
- faster iteration;
- cheaper rendering;
- clearer debugging;
- better retry boundaries;
- deterministic re-rendering;
- easier scene QA.

---

# 23. Scene contract

A scene must not exist unless it has a verified source event or a clearly defined transitional purpose.

A scene should declare:

```text
ScenePurpose
Evidence
TraceEventIds
SourceMedia
Narration
CaptionUnits
AudioSegment
VisualBeats
CameraPlan
CursorPlan
Timing
QA
```

Scene types can include:

```text
INTRO
CONTEXT
NAVIGATION
INTERACTION
RESULT
TRANSITION
OUTCOME
```

But the actual scene classification must remain evidence-driven.

---

# 24. Video Journey Director

The verified trace should be transformed into a viewer journey:

```text
ESTABLISH
→ EXPLORE
→ EXPLAIN
→ DEMONSTRATE / INSPECT
→ VERIFY TAKEAWAY
→ TRANSITION
```

Each scene should answer:

- What am I looking at?
- Why does it matter?
- What changed?
- What should the viewer understand next?

The director must not fabricate scenes to fill duration.

---

# 25. Essential, transitional, and dead-time events

Trace segments should be classified as:

```text
ESSENTIAL
TRANSITIONAL
DEAD_TIME
```

## Essential

The action or result directly supports the objective.

Retain it.

## Transitional

Navigation/scrolling that makes the story understandable.

Retain enough to preserve causal context.

## Dead time

Proven transport or waiting that adds no viewer value.

Can be removed when causality remains intact.

Do not reduce meaningful interaction to unexplained micro-cuts.

Typing, dropdown selection, dragging, drawing, scrolling, and result dwell must remain understandable.

---

# 26. Narration architecture

Narration is evidence-grounded.

The preferred lifecycle is:

```text
Plan evidence
    ↓
Editorial brief
    ↓
Storyboard
    ↓
Narration script
    ↓
Captions
    ↓
Optional TTS
```

Narration must explain:

- what the viewer sees;
- why it matters;
- what the action accomplishes;
- what result was achieved.

Reject:

- unsupported claims;
- generic filler;
- route-label narration;
- raw screen transcripts;
- “click this” narration with no explanation;
- claims that are not backed by the trace.

---

# 27. ElevenLabs and caption-only mode

ElevenLabs is optional.

When credentials are unavailable:

```text
Verified Trace
→ Narration Script
→ Captions
→ Scene Render
```

This is valid for testing the majority of the pipeline.

However, a full production acceptance must eventually test:

```text
Verified Trace
→ Script
→ ElevenLabs Audio
→ measured timing
→ Sync Engine
→ Captions
→ Render
```

Do not treat missing ElevenLabs credentials as an interaction-engine failure.

---

# 28. Dedicated Sync Engine

A dedicated synchronization layer is required.

The Sync Engine owns the temporal relationship between:

- trace events;
- scene boundaries;
- narration;
- audio;
- captions;
- cursor actions;
- click effects;
- camera movement;
- visual beats;
- result dwell.

The conceptual flow is:

```text
Verified Trace
      ↓
Scene Timeline
      ↓
Approved Narration
      ↓
Generated Audio
      ↓
Measured Audio Timing
      ↓
SYNC ENGINE
      ↓
Synced EDL / Timeline
      ↓
Renderer
```

The Sync Engine must not invent product events.

It only synchronizes verified inputs.

---

# 29. Sync timing rules

Never estimate narration timing from:

- character count;
- word count alone;
- fixed characters-per-second assumptions.

When audio exists, use measured audio timing.

The preferred flow is:

```text
approved script
→ generated audio
→ measured segment/word timing
→ caption timing
→ scene timing
→ render
```

Where word-level timestamps are unavailable, the system may use measured segment timing with explicit uncertainty, but must not pretend to have word precision it does not possess.

---

# 30. Sync Engine data model

A sync unit should contain:

```text
sync_id
scene_id
trace_event_ids
script_segment_id
audio_segment_id
caption_ids
visual_beat_ids
start_time
end_time
source_media_start
source_media_end
cursor_events
camera_events
confidence
```

The Sync Engine should output a deterministic synchronization manifest / EDL.

Example:

```text
Scene 03
source: execution/browser-recording.mp4
source range: 18.2s–27.8s

audio:
  0.0s–4.2s

caption:
  0.0s–1.8s
  1.8s–4.2s

cursor:
  2.1s click Add Lead

camera:
  1.5s–3.5s zoom target=form

result dwell:
  7.2s–9.0s
```

---

# 31. Sync must be trace-grounded

If the trace says:

```text
Click Add Lead = 18.4s
```

but the presentation timeline says:

```text
Click Add Lead = 14.2s
```

the system must identify the discrepancy.

Do not silently shift timestamps until they appear plausible.

Possible causes:

- source media trimming;
- scene offset;
- incorrect trace-to-media mapping;
- render timing;
- stale EDL;
- wrong run ID.

These should be classified.

---

# 32. Rendering architecture

Rendering should be deterministic.

Conceptual layers:

```text
Background
Browser
Camera
Cursor
Click Effects
Highlights
Captions
Audio
Brand
Transitions
```

Remotion/FFmpeg should consume:

```text
Verified Trace
+
Scene Manifest
+
Sync Manifest
+
Source Media
+
Presentation Props
```

The renderer should not decide:

- whether an action succeeded;
- what the user intended;
- which application control was correct;
- whether a workflow is valid.

---

# 33. Scene rendering

Each scene should be renderable independently.

```text
Scene 1
→ render
→ validate

Scene 2
→ render
→ validate

Scene 3
→ render
→ validate
```

Scene rendering should write candidates first and promote successful artifacts atomically.

A scene artifact should be associated with:

```text
run_id
scene_id
trace_version
sync_version
renderer_version
source media fingerprint
```

This prevents stale scene artifacts from being combined with unrelated traces.

---

# 34. Final composition

After all scenes pass:

```text
validated scenes
+
transitions
+
global audio policy
+
global branding
+
final timeline
↓
Final Composition
↓
demo.mp4
```

Final composition should be deterministic.

It should not reopen the browser.

It should not call an LLM.

It should not reinterpret the workflow.

---

# 35. Quality architecture

QA is layered.

```text
Discovery QA
Planning QA
Interaction QA
Outcome QA
Trace QA
Scene QA
Editorial QA
Synchronization QA
Visual QA
Render QA
Delivery QA
```

VIDEO_QA aggregates these results.

---

# 36. Interaction QA

Must detect:

- failed action;
- ambiguous target;
- stale target;
- unsafe mutation;
- missing precondition;
- unexpected navigation;
- incorrect state;
- missing verification;
- duplicate mutation;
- unexplained recovery;
- incomplete workflow.

---

# 37. Scene QA

Must detect:

- scene without verified evidence;
- missing source media;
- wrong trace events;
- wrong scene timing;
- missing narration;
- caption coverage gaps;
- cursor action without trace event;
- camera target outside safe bounds;
- insufficient result dwell;
- frozen or black frames.

---

# 38. Synchronization QA

Cross-check:

```text
trace
↔ scene
↔ narration
↔ captions
↔ audio
↔ cursor
↔ camera
↔ render
```

Examples of failures:

```text
CAPTION_TRACE_MISMATCH
CAPTION_WITHOUT_PRESENTATION_BEAT
AUDIO_WITHOUT_SCENE
SCENE_WITHOUT_AUDIO_MAPPING
CURSOR_WITHOUT_TRACE_EVENT
SYNC_TIMELINE_GAP
SYNC_TIMELINE_OVERLAP
```

---

# 39. Visual QA

Check:

- black frames;
- frozen frames;
- source fidelity;
- readable UI;
- caption contrast;
- cursor alignment;
- camera bounds;
- smooth scrolling;
- native-speed interaction;
- excessive cropping;
- incorrect browser zoom;
- visual discontinuities.

The final video must remain faithful to the actual browser recording.

---

# 40. Delivery QA

Delivery is allowed only if:

```text
required artifacts exist
AND
critical trace events verified
AND
scene QA passes
AND
editorial QA passes
AND
sync QA passes
AND
render QA passes
AND
visual QA passes
AND
required multimodal checks pass
```

Conceptually:

```text
hard_failures =
    execution failures
  ∪ outcome failures
  ∪ trace failures
  ∪ scene failures
  ∪ story failures
  ∪ editorial failures
  ∪ synchronization failures
  ∪ video failures
  ∪ visual-review failures
  ∪ artifact failures
```

If the union is non-empty, delivery fails.

---

# 41. Failure ownership

Every failure must have an owner.

Recommended mapping:

```text
auth/discovery blocker
    → DISCOVERY

target discovery / interaction capability
    → EXECUTION / INTERACTION

rehearsal failure
    → PLANNING

certified outcome missing
    → PLANNING

trace/outcome failure
    → EXECUTION

story/editorial/caption failure
    → NARRATION

sync timeline failure
    → SYNC / NARRATION boundary

scene rendering failure
    → RENDER

final media failure
    → RENDER / VIDEO_QA

multimodal review unavailable
    → VIDEO_QA
```

Do not retry the entire pipeline when the failure belongs to a later stage.

---

# 42. Retry boundaries

Retries must be targeted.

Examples:

```text
Narration failure
→ retry NARRATION

Scene render failure
→ retry affected scene

Sync failure
→ rebuild sync manifest

Final composition failure
→ recomposite

Interaction failure
→ rerun interaction/EXECUTION

Rehearsal failure
→ return to PLANNING
```

A failed provider call must not be interpreted as a successful browser action.

A dispatched mutation must never be blindly replayed merely because a later stage failed.

---

# 43. Scene-level retry lineage

Each scene retry should preserve:

```text
parent_scene_id
parent_run_id
failure_reason
source_trace_version
source_sync_version
retry_count
```

A repaired scene must not silently overwrite the evidence that produced the failure.

---

# 44. Persistence and artifacts

The run-local artifact directory remains the source of durable stage handoff.

Recommended structure:

```text
artifacts/runs/<run_id>/
│
├── objective.json
│
├── discovery/
│   ├── product-context.json
│   ├── product-knowledge.json
│   ├── capabilities.json
│   ├── behavioral-product-model.json
│   ├── relevance-graph.json
│   ├── stagehand-observation.json
│   ├── viewport-decision.json
│   └── page-knowledge/
│
├── planning/
│   ├── demo-brief.json
│   ├── certified-workflow-graph.json
│   ├── validated-state-graph.json
│   ├── capability-resolutions.json
│   └── storyboard.json
│
├── execution/
│   ├── trace.json
│   ├── interaction-trace.json
│   ├── action-attempts.json
│   ├── state-snapshots.json
│   ├── verification-results.json
│   ├── browser-recording.mp4
│   └── browserbase-session.json
│
├── scenes/
│   ├── scene-manifest.json
│   ├── scene-001/
│   ├── scene-002/
│   └── ...
│
├── narration/
│   ├── fact-extraction.json
│   └── editorial-script.json
│
├── sync/
│   ├── timeline.json
│   ├── sync-edl.json
│   └── timing-report.json
│
├── presentation/
│   ├── storyboard.json
│   ├── scene-plan.json
│   ├── presentation-plan.json
│   ├── captions.json
│   └── remotion-props.json
│
├── render/
│   ├── status.json
│   ├── scenes/
│   └── final-candidate.mp4
│
├── qa/
│   ├── interaction-report.json
│   ├── outcome-report.json
│   ├── scene-report.json
│   ├── editorial-report.json
│   ├── synchronization-report.json
│   ├── video-report.json
│   ├── multimodal-report.json
│   ├── delivery-report.json
│   └── repair-decision.json
│
└── final/
    ├── demo.mp4
    └── poster.jpg
```

Every artifact should identify the run and relevant version.

---

# 45. Existing package architecture

Preserve the existing package boundaries where they remain useful:

```text
app.api
app.auth

app.services
app.orchestration
app.workers

app.contracts

app.discovery
app.providers

app.planning

app.interaction
app.execution

app.persistence
app.artifacts
app.storage

app.presentation
app.narration

app.video

app.quality
app.evaluation
```

The exact module names may evolve during refactoring, but responsibility must remain explicit.

---

# 46. Architecture hotspots to refactor carefully

Existing complexity hotspots include:

## Generation coordinator

Problem:

- large stage algorithms;
- too much orchestration logic;
- too many responsibilities.

Desired direction:

```text
Coordinator
    ↓
small collaborators
    ↓
browser lifecycle
persistence
planning
verification
scene production
QA
```

Do not create meaningless wrapper classes solely to reduce line count.

---

## Discovery

Separate:

```text
evidence capture
```

from:

```text
exploration policy
```

Keep the public facade simple.

---

## Planning

Separate:

```text
candidate generation
candidate scoring
workflow validation
navigation compilation
```

Do not mix legacy fallbacks with evidence-backed planning indefinitely.

---

## Interaction

Separate:

```text
target resolution
action selection
browser dispatch
verification
exploration
recovery
```

The browser adapter should not become the planner.

---

## Editorial

Separate:

```text
fact extraction
story construction
narration writing
caption generation
editorial validation
```

---

## Video

Separate:

```text
media probing
source cutting
scene rendering
composition policy
sync policy
```

The Sync Engine should not be buried inside a giant renderer.

---

## Persistence

The repository facade can remain for compatibility, but internal operations should eventually be grouped by bounded context.

---

# 47. Current architecture must not be rewritten blindly

Before changing code, the implementation agent must:

1. inspect the current repository;
2. map existing modules to the responsibilities in this README;
3. identify which requirements already exist;
4. identify duplicate/overlapping implementations;
5. identify the smallest changes needed;
6. preserve working contracts;
7. add tests before deleting behavior;
8. refactor incrementally.

Do not start with:

> “Rewrite ProductLens from scratch.”

The goal is to stabilize and simplify the existing system.

---

# 48. Development priority

The implementation order is intentionally strict.

## Phase 1 — Interaction correctness

Make this reliable:

```text
observe
→ target
→ act
→ verify
→ re-observe
```

Test:

- buttons;
- forms;
- selects;
- checkboxes;
- modals;
- dropdowns;
- hidden menus;
- dynamic forms;
- SPA navigation.

---

## Phase 2 — Progressive UI discovery

Add/strengthen:

- state graph;
- exploration actions;
- target-not-visible handling;
- relevance ranking;
- bounded exploration;
- state re-observation;
- loop detection.

---

## Phase 3 — Outcome verification

Strengthen:

- independent witnesses;
- create verification;
- URL-change verification;
- entity field matching;
- postcondition evidence;
- trace confidence.

---

## Phase 4 — Failure observability

Every action should produce a diagnosable lifecycle:

```text
OBSERVED
TARGET_CANDIDATES
TARGET_RESOLVED
SAFETY_PASSED
DISPATCHED
AFTER_STATE_CAPTURED
VERIFICATION_STARTED
VERIFIED / FAILED
```

This is essential.

“Execution failed” is not an acceptable debugging message.

---

## Phase 5 — Rehearsal

Once direct interaction is reliable:

- run isolated rehearsal;
- certify workflow;
- validate independent outcomes;
- test repeated runs.

---

## Phase 6 — Production execution

Run fresh sessions and create the authoritative DemoTrace.

---

## Phase 7 — Scene production

Convert verified traces into independent semantic scene units.

---

## Phase 8 — Narration and Sync Engine

Implement:

```text
verified trace
→ scene timeline
→ narration
→ audio
→ sync
```

---

## Phase 9 — Scene rendering and final composition

Render and validate each scene independently.

Then compose the final video.

---

## Phase 10 — QA and repair

Strengthen:

- scene QA;
- synchronization QA;
- visual QA;
- delivery QA;
- targeted repair.

---

## Phase 11 — Complex applications

Only after ordinary SaaS workflows are reliable, expand into:

- canvas editors;
- graph editors;
- drag/drop builders;
- opaque custom widgets;
- n8n-like systems;
- Excalidraw-like systems.

---

# 49. Complex applications

ProductLens ultimately aims to handle complex software, not only simple CRUD applications.

However, complexity must be treated as a capability expansion.

There are three broad classes.

## Class A — Direct UI

Examples:

```text
button
input
select
table
modal
```

This should become highly reliable first.

## Class B — State-dependent UI

Examples:

```text
menu
dropdown
nested settings
conditional forms
popover
```

This is handled by progressive UI discovery.

## Class C — Application-space interaction

Examples:

```text
canvas
graph
diagram
drag/drop editor
SVG editor
custom visual surface
```

These require richer observation and stronger witnesses.

---

# 50. Canvas/graph principle

For a canvas or graph:

```text
"I can see the canvas"
```

is not proof.

The agent must prove:

```text
target object exists
OR
target relationship exists
OR
target-local rendered change exists
```

The system should use:

- DOM evidence where available;
- accessibility evidence;
- application state;
- object/edge structure;
- screenshot evidence;
- geometry;
- rendered surface changes.

If no reliable witness exists:

```text
UNVERIFIABLE_CANVAS_STATE
```

and fail closed.

---

# 51. What “human-like” interaction means

The target is not literally to simulate every internal human cognitive process.

The engineering target is:

```text
Observe the current application
→ understand available state
→ identify relevant affordances
→ reveal hidden state when necessary
→ act through the correct interaction mechanism
→ observe what changed
→ verify the intended outcome
→ adapt
→ continue
```

This is a general-purpose browser agent behavior.

It should not be confused with:

```text
hardcoded website recipes
```

or:

```text
LLM blindly clicking screenshots
```

---

# 52. Known limitations

ProductLens cannot honestly guarantee arbitrary websites.

Known hard cases include:

- inaccessible UI;
- visually ambiguous controls;
- remote desktops;
- opaque embedded applications;
- CAPTCHA;
- OTP;
- authentication blockers;
- provider infrastructure failures;
- inaccessible iframes;
- opaque canvas/graph surfaces;
- application behavior changing between rehearsal and production;
- mutations without explicit authorization.

The correct response is:

```text
classify
→ preserve evidence
→ stop safely
→ offer read-only/human-assisted path where possible
```

Not:

```text
guess
→ render
→ claim success
```

---

# 53. Browserbase, Stagehand, Playwright

Responsibilities must remain clear.

## Playwright

Authoritative execution layer:

- dispatch;
- DOM interaction;
- deterministic browser primitives;
- postconditions;
- screenshots;
- trace;
- local recording.

## Browserbase

Cloud browser infrastructure:

- sessions;
- remote browser;
- cloud recording/screencast;
- execution environment.

## Stagehand

Advisory semantic/visual capability:

- observation;
- semantic candidate generation;
- exploration assistance;
- complex UI interpretation.

Stagehand does not become the final source of truth.

Playwright/browser evidence remains authoritative.

---

# 54. Provider failures

Provider failures must be explicit.

Examples:

```text
BROWSERBASE_UNAVAILABLE
STAGEHAND_UNAVAILABLE
OPENROUTER_UNAVAILABLE
ELEVENLABS_UNAVAILABLE
MULTIMODAL_REVIEW_UNAVAILABLE
```

A provider outage must not create a fake success.

Where possible, provider-independent fixtures should test the deterministic portions of the system.

---

# 55. Testing strategy

Testing must exist at multiple levels.

## Unit tests

Test:

- target resolution;
- state identity;
- candidate ranking;
- safety;
- verification;
- scene segmentation;
- synchronization;
- timing;
- QA classification.

## Interaction tests

Use real browser fixtures to test:

```text
button
form
dropdown
modal
nested menu
conditional field
SPA navigation
```

## Repeated reliability tests

For a supported workflow, run multiple independent attempts.

Example:

```text
10 fresh runs
```

Track:

```text
target resolution success
action success
verification success
full workflow success
```

Do not hide flaky behavior behind a single successful run.

---

# 56. Full-pipeline acceptance

Acceptance should be incremental:

```text
Gate 1
Discovery works

Gate 2
Discovery + planning works

Gate 3
Rehearsal works

Gate 4
Execution produces verified trace

Gate 5
Scene segmentation works

Gate 6
Narration/captions work

Gate 7
Sync Engine works

Gate 8
Scene rendering works

Gate 9
Final composition works

Gate 10
VIDEO_QA passes
```

A failure at Gate 4 should not be debugged through Gate 10.

---

# 57. Golden fixtures

Maintain deterministic fixtures for:

- semantic navigation;
- hidden menu discovery;
- dropdown interaction;
- form filling;
- dependent form fields;
- verification;
- scrolling;
- scene segmentation;
- caption timing;
- sync;
- rendering;
- QA.

Golden media should be regenerated from traces where possible rather than committed as the primary source of truth.

---

# 58. Rerender without reopening the browser

One of the most important benefits of the trace/scene architecture is:

```text
Existing verified trace
+
Existing source recording
+
New presentation/sync implementation
→
new video
```

without:

```text
reopen browser
rerun workflow
rerun mutation
```

This makes presentation development safe and cheap.

---

# 59. Debugging methodology

When a run fails, classify the first meaningful failure.

Use this ladder:

```text
1. Could we reach the application?
2. Did authentication succeed?
3. Did discovery observe the required capability?
4. Did planning select a grounded workflow?
5. Did rehearsal succeed?
6. Was the target found?
7. Was the target uniquely resolved?
8. Was the action dispatched?
9. Did the browser state change?
10. Did the expected outcome appear?
11. Was the outcome independently verified?
12. Was the trace written correctly?
13. Was the scene generated correctly?
14. Was narration grounded?
15. Was timing synchronized?
16. Did the scene render?
17. Did final composition work?
18. Did VIDEO_QA pass?
```

Do not debug the entire pipeline at once.

---

# 60. Development observability

For each action, log structured information similar to:

```text
ACTION_START
  objective_id
  scene_id
  action_id

OBSERVATION
  url
  route
  visible_affordances
  state_id

TARGET_CANDIDATES
  candidates
  scores

TARGET_RESOLVED
  target
  evidence

SAFETY_CHECK
  status

DISPATCH
  primitive
  timestamp

AFTER_STATE
  state_id
  url
  screenshot

VERIFICATION
  expected
  witness
  result

ACTION_COMPLETE
```

This should make a failed workflow explainable from artifacts.

---

# 61. Complexity policy

Refactoring is not about reducing line count.

A refactor is justified when it:

- clarifies ownership;
- removes duplicated responsibility;
- makes failures easier to diagnose;
- reduces coupling;
- creates a reusable domain boundary;
- makes testing easier;
- reduces accidental behavior.

Do not split a 1000-line file into ten 100-line files if responsibility is still unclear.

Do not preserve 1000-line files merely because splitting feels risky.

Use responsibility as the boundary.

---

# 62. Explicit non-goals

Do not:

- add website recipes;
- weaken evidence gates;
- allow LLM guesses to become execution truth;
- blindly retry mutations;
- make video QA compensate for execution failures;
- process the whole video as an opaque monolith;
- invent synchronization;
- claim universal browser automation;
- claim universal canvas automation;
- optimize only for line-count reduction;
- rewrite the whole repository without first mapping it.

---

# 63. Definition of done for the interaction engine

The interaction engine is considered substantially stabilized when it can reliably perform, on fresh browser sessions:

```text
open application
→ authenticate
→ navigate
→ reveal hidden UI
→ interact with form/control
→ handle state-dependent UI
→ verify each critical transition
→ verify final outcome
→ emit complete Verified Trace
```

across multiple ordinary SaaS workflows without website-specific recipes.

---

# 64. Definition of done for the presentation pipeline

The presentation pipeline is substantially stabilized when:

```text
Verified Trace
→ semantic scenes
→ grounded narration
→ measured audio timing when TTS exists
→ synchronized captions
→ synchronized cursor/camera
→ independently rendered scenes
→ scene QA
→ final composition
→ VIDEO_QA
```

produces a final video whose visual and audio timeline is consistent with the verified browser behavior.

---

# 65. Definition of done for ProductLens 2.0

ProductLens 2.0 is not done because:

- the API works;
- the UI looks good;
- an MP4 was generated;
- one website succeeded once;
- an LLM produced a plausible plan.

The system is ready when it can repeatedly demonstrate:

```text
URL + Objective
        ↓
Evidence-backed understanding
        ↓
Grounded workflow
        ↓
Reliable interaction
        ↓
Independent verification
        ↓
Verified Trace
        ↓
Semantic scene production
        ↓
Grounded narration
        ↓
Deterministic synchronization
        ↓
Scene rendering
        ↓
Layered QA
        ↓
Truthful final demo
```

---

# 66. Recommended implementation sequence for Cursor

When given this README, the implementation agent should follow this sequence.

## Step 1 — Audit

Inspect:

- repository structure;
- current interaction engine;
- discovery;
- planning;
- rehearsal;
- execution;
- trace;
- presentation;
- narration;
- rendering;
- QA;
- persistence.

Create an internal map:

```text
Requirement
→ existing implementation
→ missing implementation
→ duplicate implementation
→ risky implementation
```

Do not immediately rewrite.

## Step 2 — Establish a minimal interaction harness

Make it possible to run:

```text
URL + objective
→ browser agent
→ verified trace
```

without narration/rendering.

## Step 3 — Fix direct interaction

Make ordinary controls reliable.

## Step 4 — Implement progressive discovery

Add bounded exploration for hidden/state-dependent controls.

## Step 5 — Strengthen verification

Make independent outcome witnesses authoritative.

## Step 6 — Stabilize rehearsal

Only certify workflows that actually work.

## Step 7 — Stabilize production execution

Produce a trustworthy DemoTrace.

## Step 8 — Introduce scene production units

Break verified traces into semantic, independently renderable scenes.

## Step 9 — Build the Sync Engine

Synchronize:

```text
trace
audio
captions
cursor
camera
scene timing
```

## Step 10 — Render scenes independently

Validate each scene.

## Step 11 — Compose final video

Only from validated scenes.

## Step 12 — Strengthen QA and repair

Use targeted stage/scene retry boundaries.

---

# 67. Cursor implementation rules

When implementing this README:

1. **Inspect before editing.**
2. Reuse existing contracts when they already express the correct concept.
3. Do not create duplicate representations of the same truth.
4. Prefer one authoritative source for browser state.
5. Prefer one authoritative source for execution truth.
6. Prefer one authoritative source for synchronization.
7. Keep probabilistic decisions separate from deterministic enforcement.
8. Keep provider adapters separate from product logic.
9. Never hardcode application workflows.
10. Never relax verification merely to increase pass rate.
11. Never make the renderer decide whether an action succeeded.
12. Never make narration invent missing evidence.
13. Never blindly replay a dispatched mutation.
14. Preserve run-local artifacts for every meaningful stage.
15. Add focused tests before deleting existing behavior.
16. Refactor in small coherent phases.
17. Keep the existing durable stage architecture unless a real ownership problem requires changing it.
18. Optimize for **verified workflow reliability**, not number of generated videos.
19. Optimize for **diagnosability**, not merely fewer exceptions.
20. Treat a safe failure as preferable to a false demo.

---

# 68. The central architecture to keep in mind

Everything in ProductLens should ultimately reduce to:

```text
                 PRODUCTLENS
                     │
             URL + Objective
                     │
                     ▼
              ┌─────────────┐
              │  DISCOVERY  │
              └──────┬──────┘
                     │
                     ▼
              ┌─────────────┐
              │  PLANNING   │
              └──────┬──────┘
                     │
                     ▼
        ┌─────────────────────────┐
        │    INTERACTION ENGINE   │
        │                         │
        │ observe                 │
        │ understand              │
        │ explore if needed       │
        │ resolve                 │
        │ act                     │
        │ verify                  │
        │ re-observe              │
        └────────────┬────────────┘
                     │
                     ▼
              ┌─────────────┐
              │ VERIFIED    │
              │ TRACE       │
              └──────┬──────┘
                     │
                     ▼
             ┌──────────────┐
             │ SCENE ENGINE │
             └──────┬───────┘
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
     NARRATION             TIMELINE
          │                   │
          ▼                   ▼
       AUDIO ───────────► SYNC ENGINE
                              │
                              ▼
                       SCENE RENDERING
                              │
                              ▼
                       SCENE QA
                              │
                              ▼
                     FINAL COMPOSITION
                              │
                              ▼
                         VIDEO QA
                              │
                              ▼
                         DELIVERY
```

The key boundary is:

```text
                 TRUTH
                   │
                   ▼
             VERIFIED TRACE
                   │
                   ▼
              PRESENTATION
```

Everything before the Verified Trace is responsible for determining **what
actually happened**.

Everything after the Verified Trace is responsible for **communicating what
actually happened well**.

Neither side should silently compensate for the other.

---

# 69. Final engineering objective

The immediate goal is **not**:

> Make ProductLens work on every application.

The immediate goal is:

> **Make ProductLens reliably understand and execute ordinary unfamiliar web-app workflows, including state-dependent UI, and produce independently verified traces.**

Once that foundation is strong:

```text
ordinary SaaS
      ↓
hidden controls
      ↓
multi-step workflows
      ↓
complex custom widgets
      ↓
canvas/graph applications
      ↓
broader general-purpose browser agent
```

can be expanded incrementally.

The long-term technical capability is a general-purpose, product-aware browser agent.

The long-term product application is ProductLens: turning that capability into truthful, polished, interactive product demonstrations.

**Build the agent first. Build the presentation system on top of verified truth.**
