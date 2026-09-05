# ProductLens — Revised Implementation Plan
## Reliability-First, Demosmith-Targeted Architecture

**Status:** Revised after architecture post-mortem, Smartsevak benchmark analysis, and current web research  
**Primary objective:** Generate purposeful, polished product-demo videos from a URL + objective, comparable in behavior and presentation to the supplied Demosmith examples.

---

# 0. Executive Decision

The previous ProductLens plans were too architecture-first. They contained the right components but did not make reliability a prerequisite for progressing to higher layers.

This revision changes the implementation philosophy:

> **Every layer must prove itself before the next layer is built on top of it.**

The system is therefore not:

```text
LLM agent → browser → recording → Remotion
```

It is:

```text
User objective
    ↓
Goal understanding
    ↓
Targeted product understanding
    ↓
Semantic DemoPlan
    ↓
Validated semantic workflow
    ↓
ProductLens Execution Engine
    ↓
Browserbase + Stagehand v4 + Playwright
    ↓
Verified state transitions
    ↓
DemoTrace
    ↓
Presentation Director
    ↓
PresentationPlan
    ↓
Narration + captions + camera + cursor
    ↓
Remotion
    ↓
Video QA
    ↓
Final video
```

The critical architectural difference is that **ProductLens owns reliability**.

Stagehand, Playwright, Browserbase, LLMs, TTS and Remotion are infrastructure/components. None of them is allowed to become the implicit owner of the whole workflow.

---

# 1. What This Plan Is and Is Not

## This plan is

A production architecture for a system that should eventually support:

- arbitrary normal web applications within a defined support envelope
- URL + natural-language objective
- authentication
- targeted exploration
- semantic workflow discovery
- realistic contextual form data
- browser execution
- verification
- recording
- cinematic presentation
- narration
- captions
- adaptive viewport/zoom
- quality validation
- retries
- persistent product knowledge
- future audience-specific demos
- future multi-tab/multi-application workflows

## This plan is not

It is not a guarantee that every arbitrary website will succeed.

No current evidence supports 100% success on arbitrary websites. Demosmith itself publicly claims a 95% first-pass rate, not 100%. Its public product page also describes the same broad pipeline ProductLens is targeting: autonomous UI navigation, contextual understanding, realistic form data, smart editing, UI-aware zoom, narration and captions. [Demosmith](https://demosmith.ai/)

Therefore ProductLens will treat **95% as a measured engineering target for a defined benchmark**, not as an assumption.

---

# 2. Evidence That the Target Is Achievable

The supplied Smartsevak recording is a critical benchmark because it demonstrates the target on a real custom application rather than only a well-known public website.

The successful Demosmith flow demonstrated:

- login
- CAPTCHA handling
- application-specific navigation
- reading UI data
- lead creation
- booking creation
- appropriate form values
- dropdown/field interaction
- scrolling
- coherent workflow progression
- context-aware narration
- purposeful visual framing
- presentation that is not simply a raw 100% viewport recording

Demosmith's public site currently describes:

- autonomous clicking, typing and scrolling
- contextual UI understanding
- realistic form data
- UI-aware zooms
- auto-cuts
- cinematic pacing
- narration
- captions
- demos from URL + prompt
- persona-specific stories

It also explicitly states that it gets demos right 95% of the time on the first try. That figure is a vendor claim, not an independently verified benchmark, so ProductLens must not treat it as proof of our own future reliability.

Reference:
https://demosmith.ai/

Demos:
https://demosmith.ai/explore

---

# 3. Core Architectural Principle

## ProductLens is a semantic execution system

The LLM should not continuously decide primitive mouse operations.

Bad architecture:

```text
LLM
 ↓
click coordinates
 ↓
screenshot
 ↓
LLM
 ↓
scroll
 ↓
screenshot
 ↓
LLM
 ↓
click
```

Preferred architecture:

```text
Objective
 ↓
Semantic workflow
 ↓
Semantic operation
 ↓
UI grounding
 ↓
Browser action
 ↓
State verification
 ↓
Semantic result
```

Example:

```text
CreateLead
(
    name = "Sarah Mitchell",
    company = "Acme Systems",
    source = "Website"
)
```

is preferable to:

```text
click x=918 y=123
type "Sarah Mitchell"
click x=1044 y=412
```

The semantic layer is what allows the system to reason about the application while keeping execution deterministic whenever possible.

This direction is strongly supported by the 2026 paper **Web Agents Should Adopt the Plan-Then-Execute Paradigm**, which argues that primitive click/type/scroll tools are too page-dependent for reliable planning and that typed, task-level operations are the missing infrastructure layer.

Reference:
https://arxiv.org/abs/2605.14290

---

# 4. External Technology Decisions

## Browserbase

Use Browserbase for:

- cloud browser infrastructure
- remote sessions
- browser lifecycle
- session isolation
- supported CAPTCHA capabilities
- browser observability
- future parallel browser sessions

Browserbase also provides a direct Playwright integration.

Reference:
https://www.browserbase.com/

Playwright + Browserbase:
https://www.browserbase.com/templates/playwright

## Stagehand v4

Use Stagehand v4 for:

- semantic UI interaction
- agentic discovery
- difficult/unknown UI states
- target resolution
- flexible fallback
- browser-native state/context management

Stagehand v4 moved target management, frame tracking, execution context tracking and CDP dispatch into the browser, specifically addressing problems caused by keeping a remote browser state mirror in the client. It also improves iframe and Shadow DOM handling.

Reference:
https://www.browserbase.com/blog/stagehand-v4

Current docs:
https://docs.browserbase.com/welcome/quickstarts/stagehand

## Playwright

Use Playwright for:

- deterministic execution
- locators
- actionability
- assertions
- screenshots
- tracing
- video capture
- browser/page instrumentation
- low-level browser control

Playwright performs actionability checks before actions and provides retrying web assertions. This is valuable for ProductLens because successful invocation is not enough; the application state must be verified.

References:
https://playwright.dev/docs/actionability
https://playwright.dev/docs/test-assertions
https://playwright.dev/docs/videos
https://playwright.dev/docs/tracing

## Remotion

Use Remotion for:

- final composition
- camera movement
- cursor rendering
- click effects
- captions
- narration synchronization
- transitions
- final video rendering

Reference:
https://www.remotion.dev/docs/

## FFmpeg

Use FFmpeg only for supporting media operations:

- media inspection
- audio normalization
- muxing
- codec conversion
- compatibility processing

Do not create a second video-composition system in FFmpeg.

---

# 5. Critical Architecture Boundary

The system must have a dedicated:

# ProductLens Execution Engine

Architecture:

```text
                 ProductLens Execution Engine
                           |
             +-------------+-------------+
             |                           |
      Deterministic path            Agentic path
             |                           |
        Playwright                  Stagehand v4
             |                           |
             +-------------+-------------+
                           |
                    State Verifier
                           |
                     Action Result
```

### Rule

Stagehand is not the executor of record.

Playwright is not the executor of record.

**ProductLens is the executor of record.**

The engine decides:

- what semantic operation is being performed
- what target must be found
- what precondition must hold
- which browser mechanism to use
- what postcondition proves success
- whether to continue
- whether to retry
- whether to re-ground
- whether to stop

---

# 6. Reliability Gates

This is the most important change from previous plans.

No phase is considered complete because the code exists.

Each phase has an acceptance gate.

## Gate 1 — Browser primitives

Prove reliable execution of:

- click
- fill
- select
- checkbox
- radio
- date
- date range
- searchable dropdown
- phone field
- scroll
- navigation
- modal
- search
- filter
- submit
- wait
- state assertion

across a benchmark.

If this fails, stop.

## Gate 2 — Semantic workflow execution

Prove:

```text
DemoPlan
 ↓
10–20 semantic operations
 ↓
verified completed workflow
```

If this fails, stop.

## Gate 3 — DemoTrace

Prove that every successful action produces sufficient metadata for video production.

If this fails, stop.

## Gate 4 — Presentation

Prove:

```text
DemoTrace
 ↓
PresentationPlan
 ↓
polished video
```

If this fails, stop.

## Gate 5 — AI planning

Prove:

```text
objective
 ↓
DemoPlan
 ↓
successful execution
```

If this fails, do not blame the browser layer.

## Gate 6 — End-to-end

Only now run:

```text
URL + objective
 ↓
final video
```

---

# 7. Reliability Target

ProductLens should initially target:

## ≥95% successful first-pass runs on a defined supported-app benchmark

This does **not** mean:

> 95% of every website on the internet.

The benchmark must explicitly define:

- supported application classes
- supported interaction types
- authentication assumptions
- CAPTCHA assumptions
- external side-effect restrictions
- maximum workflow complexity
- unsupported components

The benchmark must be representative of ProductLens's intended market.

---

# 8. Golden Benchmark

The Smartsevak lead + booking flow becomes a mandatory golden benchmark.

Input:

```text
URL
credentials
objective = "Explore lead and bookings flow"
```

Expected behavior:

```text
login
 ↓
identify Leads
 ↓
understand lead creation
 ↓
create lead
 ↓
understand booking relationship
 ↓
create booking
 ↓
verify booking
 ↓
produce coherent demo
```

Expected video characteristics:

- no broken interaction
- no stuck browser
- appropriate viewport
- smooth navigation
- useful scrolling
- contextual data
- coherent narration
- captions
- visible outcome
- cinematic framing

This benchmark must be replayable after every major architecture change.

---

# 9. Product Scope

## V1 supported applications

Target:

- SaaS applications
- CRMs
- dashboards
- forms
- marketplaces
- ecommerce
- scheduling apps
- admin panels
- portfolio websites
- search/filter applications
- common business applications

Do not promise arbitrary browser compatibility.

Unsupported or difficult cases must be detected and classified rather than silently producing bad videos.

---

# 10. User Input

The user provides:

- product URL
- objective
- optional audience
- optional duration
- credentials
- optional constraints
- optional things to emphasize
- optional things to skip

Example:

```text
Explore lead and bookings flow.
```

or:

```text
Create a demo showing how a sales rep creates and books a lead.
```

---

# 11. Objective Semantics

If objective is specific:

```text
"Explore lead and bookings flow"
```

ProductLens should focus only on relevant areas.

If objective is vague:

```text
"Create a demo of this CRM."
```

ProductLens performs bounded capability discovery and selects the strongest workflows.

It must not crawl the entire application by default.

---

# 12. Targeted Exploration

Exploration must be:

## Goal-directed
Explore only areas relevant to the objective.

## Budgeted
Set:

- time budget
- page budget
- action budget
- model-call budget
- exploration depth

## Progressive
Stop as soon as enough information exists to create and validate a strong workflow.

## Evidence-backed
Do not store assumptions as facts.

---

# 13. Why Full-Product Exploration Is Wrong

A CRM could have:

- leads
- bookings
- marketing
- analytics
- campaigns
- settings
- integrations
- billing
- telephony
- WhatsApp
- reports
- administration

A 2-minute demo does not require all of that.

Correct:

```text
objective
 ↓
relevant capabilities
 ↓
deep exploration
 ↓
stop
```

Incorrect:

```text
objective
 ↓
crawl entire CRM
 ↓
hours later
 ↓
generate demo
```

---

# 14. Product Knowledge

Persist reusable knowledge:

```text
ProductKnowledge
- project_id
- version
- application_type
- navigation
- routes
- entities
- feature_map
- page_knowledge
- workflow_knowledge
- form_schemas
- interaction_patterns
- known_blockers
- successful_actions
- last_verified_at
```

Knowledge must be:

- versioned
- timestamped
- evidence-backed
- freshness-aware

Never treat stale knowledge as ground truth.

---

# 15. Product Discovery Model

The discovery layer should produce:

```text
ProductContext
- application_type
- authentication_state
- relevant_routes
- relevant_features
- entities
- navigation
- UI patterns
- forms
- workflows
- candidate_demo_flows
- blockers
- confidence
```

Discovery should consume:

- DOM
- accessibility tree
- screenshots
- current URL
- visible text
- semantic element information
- browser state

Pixels alone are insufficient for ordinary web applications.

---

# 16. Demo Planner

Input:

```text
User objective
ProductContext
Audience
Duration
Constraints
```

Output:

```text
DemoPlan
```

The DemoPlan must be schema validated.

It contains:

```text
DemoPlan
- objective
- narrative_goal
- audience
- target_duration
- selected_workflow
- workflow_steps
- synthetic_data_plan
- expected_outcomes
- important_elements
- excluded_areas
- viewport_strategy
- risk_flags
- stop_conditions
```

The LLM cannot return arbitrary browser instructions.

---

# 17. WorkflowStep

Each step:

```text
WorkflowStep
- step_id
- semantic_intent
- operation_type
- target_semantics
- input_value
- precondition
- postcondition
- fallback_strategy
- importance
- narration_intent
- visual_intent
- allowed_retries
```

Example:

```json
{
  "semantic_intent": "Select company size",
  "operation_type": "select",
  "target_semantics": {
    "role": "combobox",
    "label": "Company size"
  },
  "input_value": "51-200 employees",
  "precondition": {
    "page_contains": "Company size"
  },
  "postcondition": {
    "selected_value": "51-200 employees"
  }
}
```

---

# 18. Semantic Operation Library

The engine should have a typed operation library.

Initial operations:

```text
Navigate
OpenNavigationItem
Click
FillText
FillEmail
FillPhone
SelectOption
SelectDate
SelectDateRange
Check
Uncheck
ChooseRadio
Search
ApplyFilter
OpenModal
CloseModal
ScrollTo
Submit
WaitForState
ReadValue
VerifyState
```

Later:

```text
CreateLead
CreateBooking
CreateProject
InviteUser
Publish
UploadFile
ConfigureIntegration
```

Domain operations are compiled into lower-level browser operations.

---

# 19. UI Grounding

Every semantic operation must be grounded to the current UI.

Possible evidence:

1. Accessible role/name
2. Label
3. Text
4. Nearby semantic context
5. DOM relationships
6. Stable attributes
7. Stagehand semantic locator
8. Screenshot evidence where necessary

The engine should score candidate targets.

Example:

```text
Target candidates
-----------------------------
"Create Lead" button      0.98
"Create Booking" button    0.31
"New Lead" menu item       0.72
```

Only sufficiently confident targets proceed.

---

# 20. Action Execution

Preferred order:

```text
Known deterministic locator
        ↓
Semantic Playwright locator
        ↓
Stagehand locator/action
        ↓
Agentic re-grounding
```

Do not invoke an LLM if a previously validated deterministic path remains valid.

Stagehand's current caching system follows a related principle: it caches resolved selectors and validates that the current page still matches before executing without another LLM call. Browserbase reports workload-dependent speedups of up to ~80% for repeated actions.

Reference:
https://www.browserbase.com/blog/stagehand-caching

---

# 21. State Verification

Every meaningful operation requires a postcondition.

Examples:

### Click

Verify:

- modal opened
- menu opened
- route changed
- expected element appeared
- state changed

### Fill

Verify:

- expected value exists
- field accepted the value
- validation state is acceptable

### Select

Verify:

- selected value is correct

### Submit

Verify:

- success state
- created record
- confirmation
- route change
- expected result

### Navigation

Verify:

- expected route
- expected title
- expected key UI
- expected page state

---

# 22. Playwright Reliability Layer

Use Playwright's native guarantees wherever possible.

Playwright's actionability checks verify conditions such as:

- element resolves uniquely
- visible
- stable
- receives events
- enabled

It also provides retrying web assertions.

References:
https://playwright.dev/docs/actionability
https://playwright.dev/docs/test-assertions

Do not bypass these checks with `force` unless a specific adapter explicitly requires it and the resulting state is independently verified.

---

# 23. State Machine

The browser workflow must be represented as explicit state.

Example:

```text
LOGIN_PAGE
 ↓
AUTHENTICATED
 ↓
DASHBOARD
 ↓
LEADS_LIST
 ↓
LEAD_CREATE_FORM
 ↓
LEAD_CREATED
 ↓
LEAD_DETAIL
 ↓
BOOKING_FORM
 ↓
BOOKING_CREATED
```

A transition is only valid when its postcondition is satisfied.

This prevents:

```text
click succeeded
```

from being interpreted as:

```text
workflow succeeded
```

---

# 24. Execution vs Exploration

Use separate browser runs.

## Exploration run

Purpose:

- understand product
- discover workflow
- test candidate actions
- inspect UI
- learn state transitions

It may be messy.

## Production run

Purpose:

- execute only the validated workflow
- capture clean interaction
- produce DemoTrace

It should not improvise unnecessarily.

This separation is mandatory.

---

# 25. Recording Architecture

Do not treat raw browser video as the final video.

Capture:

- browser video
- timestamps
- interaction events
- target bounding boxes
- element semantics
- screenshots
- DOM/accessibility snapshots
- URL
- viewport
- browser zoom
- scroll position
- state transitions
- action result
- loading periods
- errors

Playwright supports video recording, and its tracing system can capture screenshots, DOM snapshots and network activity. Traces should be enabled for debugging/diagnostics rather than used as the final presentation artifact.

References:
https://playwright.dev/docs/videos
https://playwright.dev/docs/tracing

---

# 26. DemoTrace

DemoTrace is the canonical evidence layer.

Example:

```json
{
  "timestamp": 12.42,
  "page": "/leads",
  "operation": "Click",
  "semantic_intent": "Open Create Lead",
  "target": {
    "role": "button",
    "name": "Create Lead",
    "bbox": [920, 120, 140, 42]
  },
  "viewport": {
    "width": 1440,
    "height": 900
  },
  "browser_zoom": 0.8,
  "scroll": {
    "x": 0,
    "y": 420
  },
  "result": {
    "success": true,
    "state_transition": "LEADS_LIST -> LEAD_CREATE_FORM"
  }
}
```

DemoTrace must contain enough information to reconstruct presentation decisions without asking the browser agent to replay the workflow.

---

# 27. Viewport Optimization

The system should not hardcode 100%, 90%, 80% or 75%.

Instead:

```text
Load relevant page
 ↓
test candidate viewport/zoom configurations
 ↓
score UI
 ↓
select best configuration
 ↓
lock for production capture
```

Score:

- relevant content visible
- text readability
- form completeness
- navigation visibility
- responsive layout
- target accessibility
- visual density
- cinematic composition potential

---

# 28. Browser Zoom vs Cinematic Zoom

These are separate.

## Browser zoom

Changes the actual application layout.

Purpose:

- make the application fit the capture viewport
- improve readability
- show more relevant content

## Cinematic zoom

Implemented in Remotion.

Purpose:

- emphasize important UI
- guide viewer attention
- create polished camera movement

The Smartsevak reference should be used as a visual benchmark, but the exact mechanism Demosmith uses cannot be inferred from the final MP4.

---

# 29. Synthetic Data Engine

Generate data from the actual UI semantics.

Examples:

```text
Country = India
→ Indian phone number

Field = Work email
→ realistic business email

Company size = 51-200
→ plausible company context

Booking date
→ valid future date
```

Pipeline:

```text
Form schema
 ↓
field semantics
 ↓
dependencies
 ↓
synthetic dataset
 ↓
local validation
 ↓
browser fill
 ↓
application validation
 ↓
postcondition
```

---

# 30. Form Intelligence

Initial support:

- text
- textarea
- email
- password
- number
- phone
- country + phone
- select
- searchable select
- multi-select
- checkbox
- radio
- date
- date range
- time
- slider
- dependent fields
- required/optional fields
- validation
- common custom controls

The application itself remains the final validator.

---

# 31. Authentication

V1:

- username/password

Credentials must never be included in:

- LLM prompts
- narration prompts
- logs
- DemoTrace
- screenshots
- analytics

The planning layer should receive only:

```text
authentication_available = true
```

The browser credential service handles actual secrets.

---

# 32. CAPTCHA / 2FA / OTP

CAPTCHA:

- use supported Browserbase capabilities where appropriate
- treat success/failure as browser infrastructure state
- verify login after CAPTCHA

2FA/OTP:

- do not demonstrate OTP/2FA in the video unless explicitly supported later
- notify user when encountered
- continue from authenticated state when possible
- fail cleanly if authentication cannot continue

Never fake authentication.

---

# 33. Side Effects

Initially allow only safe/testable operations.

Potentially allowed:

- create test lead
- create booking
- create project
- edit test data
- delete test data when explicitly requested

Initially block:

- real payments
- external email sending
- SMS/WhatsApp
- real user invitations
- uncontrolled webhooks
- production publishing
- external integrations

---

# 34. Demo Director

Input:

```text
DemoPlan
+
DemoTrace
+
UI metadata
```

Output:

```text
PresentationPlan
```

The PresentationPlan decides:

- scenes
- selected actions
- camera states
- zooms
- cursor paths
- cuts
- narration
- captions
- loading compression
- emphasis
- transitions

This is where browser success becomes a purposeful video.

---

# 35. Story Model

Default structure:

```text
1. Context
2. Enter relevant product area
3. Perform meaningful workflow
4. Show result
5. Reinforce outcome
```

The story must answer:

> What is being demonstrated?

and:

> Why should the viewer care?

---

# 36. Action Classification

Each interaction is classified:

```text
ESSENTIAL
TRANSITIONAL
DEAD_TIME
```

### Essential

Keep.

### Transitional

Can be compressed.

### Dead time

Remove only when continuity remains understandable.

Do not cut so aggressively that the viewer loses causality.

---

# 37. Scrolling

Execution scrolling:

> get the browser to the target.

Presentation scrolling:

> show the movement in a visually understandable way.

They must be separate systems.

Presentation scroll should consider:

- current camera position
- target
- scroll distance
- duration
- narration
- UI density
- continuity

---

# 38. Cursor

Do not depend on the real browser cursor for final presentation.

Render a synthetic cursor from DemoTrace.

Support:

- smooth movement
- easing
- hover pause
- click ripple
- click emphasis
- cursor visibility
- target-aware positioning

This directly addresses the current ProductLens problem where the cursor is laggy or barely moves.

---

# 39. Camera

Camera state:

```text
CameraState
- x
- y
- scale
- duration
- easing
- target
```

Interaction pattern:

```text
reframe
 ↓
move toward target
 ↓
settle
 ↓
action
 ↓
hold
 ↓
reframe
```

Do not generate arbitrary camera paths directly from an LLM.

The LLM can identify the important target; deterministic presentation logic should generate the motion.

---

# 40. Loading Handling

For slow application operations:

- narrate during loading
- accelerate footage
- shorten waiting
- cut dead time
- retain enough visual continuity to prove the operation happened

Never leave 5–10 seconds of unexplained waiting in a 2-minute demo.

---

# 41. Narration

Narration input:

```text
DemoPlan
+
actual DemoTrace
+
visible UI
```

Not merely the user's prompt.

The script should explain:

- what is happening
- why it matters
- what the feature accomplishes
- what result was achieved

Avoid robotic narration such as:

> "Now I click the Leads tab."

Prefer:

> "The Leads workspace gives the sales team a single place to qualify and manage new prospects."

Action narration is used when the action itself matters.

---

# 42. TTS Abstraction

Interface:

```text
TTSProvider
- generate_audio()
- get_voice()
- get_languages()
- estimate_duration()
```

Initial provider:

- ElevenLabs

Future providers must be plug-in adapters.

---

# 43. Narration Timing

Pipeline:

```text
script
 ↓
TTS
 ↓
actual audio duration
 ↓
segment timing
 ↓
caption timing
 ↓
scene timing
 ↓
render
```

Never estimate spoken duration from character count alone.

---

# 44. Captions

Generate captions from final narration timing.

Each caption:

```text
start
end
text
scene_id
```

Captions must synchronize with actual audio.

---

# 45. Remotion Architecture

```text
VideoComposition
├── BrowserLayer
├── CameraLayer
├── CursorLayer
├── ClickEffectLayer
├── HighlightLayer
├── CaptionLayer
├── AudioLayer
├── BrandLayer
├── TransitionLayer
└── BackgroundLayer
```

Remotion should receive structured presentation data.

It should not call the browser agent.

---

# 46. Video QA

A rendered MP4 is not automatically a successful demo.

## Execution QA

Check:

- objective satisfied
- critical workflow completed
- expected states reached
- no unrecovered errors
- final outcome exists

## Visual QA

Check:

- application visible
- no black frames
- no broken frames
- no accidental cropping
- borders/edges preserved
- text readable
- cursor visible
- smooth cursor
- smooth scroll
- sensible zoom
- no major jitter

## Story QA

Check:

- objective is understandable
- workflow has purpose
- narration matches screen
- no unexplained jumps
- outcome is visible
- pacing is coherent

## Audio QA

Check:

- narration exists
- no unintended silence
- audio/video synchronization
- captions synchronized
- acceptable levels

---

# 47. Quality Report

```text
QualityReport
- execution_score
- workflow_score
- visual_score
- story_score
- audio_score
- synchronization_score
- viewport_score
- overall_score
- hard_failures
- warnings
```

---

# 48. Hard Failures

Never deliver a video containing:

- failed critical action
- visible browser error
- stuck loading
- missing workflow outcome
- major UI corruption
- unexplained navigation
- missing audio when narration was requested
- broken caption synchronization
- severe cursor/camera failure
- silent video caused by missing capture
- recording that does not demonstrate the requested objective

---

# 49. Retry Policy

Retry only after classifying the failure.

```text
Video QA failure
 ↓
classify
 ↓
presentation failure?
 ├── yes → regenerate presentation
 ↓
execution failure?
 ├── yes → targeted re-execution
 ↓
provider failure?
 ├── yes → provider retry/fallback
 ↓
internal failure?
 └── fail job with diagnostics
```

Do not blindly repeat the entire workflow.

---

# 50. Repair Policy

Repair is a fallback, not the architecture.

Normal path:

```text
plan
 ↓
execute
 ↓
verify
 ↓
continue
```

Fallback:

```text
verification failure
 ↓
diagnose
 ↓
re-ground
 ↓
alternate strategy
 ↓
verify
```

If repair exceeds its budget:

```text
run invalid
```

The user should receive a meaningful failure reason rather than a broken video.

---

# 51. Failure Taxonomy

```text
PLANNING_FAILURE
DISCOVERY_FAILURE
AUTH_FAILURE
CAPTCHA_FAILURE
TARGET_RESOLUTION_FAILURE
EXECUTION_FAILURE
STATE_VERIFICATION_FAILURE
APPLICATION_BLOCKER
CAPTURE_FAILURE
TRACE_FAILURE
PRESENTATION_FAILURE
NARRATION_FAILURE
AUDIO_FAILURE
RENDER_FAILURE
VIDEO_QA_FAILURE
PROVIDER_FAILURE
TIMEOUT
UNSUPPORTED_APPLICATION
UNSUPPORTED_INTERACTION
```

---

# 52. End-to-End State Machine

```text
QUEUED
 ↓
FEASIBILITY_CHECK
 ↓
DISCOVERING
 ↓
PLAN_READY
 ↓
PLAN_VALIDATED
 ↓
EXPLORATORY_EXECUTION
 ↓
WORKFLOW_VALIDATED
 ↓
PRODUCTION_EXECUTION
 ↓
TRACE_READY
 ↓
PRESENTATION_PLANNED
 ↓
NARRATION_READY
 ↓
RENDERING
 ↓
VIDEO_QA
 ↓
COMPLETED
```

Failure:

```text
FAILED
```

Retry:

```text
RETRYING
```

---

# 53. Exploration Budget

For normal 2–3 minute demos:

```text
exploration_time_budget
page_budget
action_budget
model_call_budget
depth_budget
```

The system must continuously evaluate:

```text
Do we know enough to execute a good demo?
```

If yes:

```text
STOP EXPLORING
```

Do not explore the entire product.

---

# 54. Duration

Do not hardcode 2–3 minutes.

Store:

```text
requested_duration
minimum_duration
maximum_duration
target_duration
```

The planner allocates workflow time based on the objective.

V1 target:

```text
120–180 seconds
```

Longer demos should reuse the same pipeline with a larger time budget.

---

# 55. Audience

Model audience now:

```text
AudienceProfile
- type
- priorities
- vocabulary
- depth
- narration_style
- workflow_preferences
```

Types:

```text
GENERAL_USER
SALES
RECRUITER
FOUNDER
PROSPECT
SUPPORT
ONBOARDING
INTERNAL
```

Audience affects:

- workflow selection
- narration
- emphasis
- duration
- visual priority

---

# 56. Future Multi-Application Support

Do not implement in V1, but do not make the core incompatible with it.

Future structure:

```text
DemoRun
 └── ExecutionContext
      ├── BrowserSession A
      │    ├── Page 1
      │    └── Page 2
      ├── BrowserSession B
      │    └── Page 1
      └── ApplicationContext
           ├── website
           ├── chatbot
           └── CRM
```

Example future workflow:

```text
website lead
 ↓
chatbot qualification
 ↓
CRM creation
 ↓
CRM booking
```

DemoTrace remains the unified evidence layer.

---

# 57. Backend MVC Architecture

MVC applies to the ProductLens application/API.

## Model

Domain entities:

```text
User
Project
DemoRequest
DemoRun
DemoAttempt
DemoPlan
WorkflowStep
BrowserSession
InteractionEvent
ProductKnowledge
PageKnowledge
FormSchema
SyntheticDataset
PresentationPlan
NarrationScript
AudioAsset
VideoRender
QualityReport
ProviderConfiguration
Artifact
```

## View

Next.js UI.

## Controller

FastAPI controllers.

Controllers must not contain:

- browser automation
- LLM prompts
- video rendering
- business orchestration

They call application services.

---

# 58. Backend Structure

```text
backend/
├── app/
│   ├── main.py
│   ├── api/
│   │   └── v1/
│   │       ├── controllers/
│   │       ├── schemas/
│   │       └── dependencies/
│   ├── domain/
│   │   ├── models/
│   │   ├── enums/
│   │   ├── value_objects/
│   │   └── interfaces/
│   ├── services/
│   │   ├── demo/
│   │   ├── planning/
│   │   ├── discovery/
│   │   ├── execution/
│   │   ├── presentation/
│   │   ├── narration/
│   │   ├── quality/
│   │   ├── credentials/
│   │   └── projects/
│   ├── agents/
│   │   ├── planner/
│   │   ├── product_understanding/
│   │   ├── workflow_discovery/
│   │   ├── execution/
│   │   └── presentation/
│   ├── browser/
│   │   ├── browserbase/
│   │   ├── stagehand/
│   │   ├── playwright/
│   │   ├── sessions/
│   │   ├── instrumentation/
│   │   └── verification/
│   ├── video/
│   │   ├── remotion/
│   │   ├── composition/
│   │   ├── camera/
│   │   ├── cursor/
│   │   ├── captions/
│   │   ├── audio/
│   │   └── encoding/
│   ├── providers/
│   ├── workers/
│   ├── repositories/
│   ├── events/
│   ├── config/
│   └── logging/
└── tests/
```

---

# 59. Frontend Structure

```text
frontend/
├── app/
│   ├── dashboard/
│   ├── projects/
│   ├── demos/
│   └── settings/
├── components/
│   ├── demo/
│   ├── project/
│   ├── video/
│   ├── providers/
│   └── common/
├── features/
│   ├── demo-generation/
│   ├── demo-library/
│   ├── project-management/
│   └── settings/
├── services/
├── hooks/
├── stores/
├── types/
└── utils/
```

---

# 60. Services

Recommended boundaries:

```text
DemoService
GoalAnalysisService
ProductDiscoveryService
WorkflowPlanningService
WorkflowValidationService
BrowserExecutionService
StateVerificationService
TraceService
SyntheticDataService
ViewportOptimizationService
PresentationService
NarrationService
AudioService
RenderService
QualityService
ArtifactService
ProviderService
```

Avoid a MegaAgent.

---

# 61. Provider Abstractions

```text
LLMProvider
TTSProvider
BrowserProvider
CaptchaProvider
StorageProvider
```

Provider configuration:

```text
Provider
- id
- type
- name
- config
- credential_reference
- active
- priority
```

The business logic must not depend on a specific provider.

---

# 62. Job Architecture

Generation is asynchronous.

```text
API
 ↓
Queue
 ├── discovery worker
 ├── planning worker
 ├── execution worker
 ├── narration worker
 ├── render worker
 └── QA worker
```

Workers must be:

- idempotent
- resumable
- observable

---

# 63. Job Idempotency

Track:

```text
request_id
demo_id
run_id
attempt_id
provider_call_id
artifact_id
```

Retries must not:

- duplicate records
- corrupt successful artifacts
- duplicate uncontrolled side effects
- overwrite good runs

---

# 64. Database

Core tables:

```text
users
projects
demo_requests
demo_runs
demo_attempts
demo_plans
workflow_steps
browser_sessions
interaction_events
product_knowledge
page_knowledge
form_schemas
synthetic_datasets
presentation_plans
narration_scripts
audio_assets
video_renders
quality_reports
provider_configs
artifacts
```

Indexes:

```text
project_id
demo_request_id
demo_run_id
status
created_at
```

---

# 65. Artifact Storage

Per-run:

```text
runs/
  {run_id}/
    feasibility.json
    plan.json
    discovery/
      screenshots/
      pages.json
      knowledge.json
    execution/
      trace.json
      screenshots/
      browser-recording.webm
      playwright-trace.zip
    presentation/
      presentation-plan.json
      camera-plan.json
      cursor-plan.json
      captions.json
    audio/
      narration.mp3
    render/
      draft.mp4
    qa/
      report.json
    final/
      demo.mp4
```

Use object storage in production.

---

# 66. Observability

Structured logging is mandatory.

Include:

```text
request_id
project_id
demo_id
run_id
attempt_id
stage
provider
operation
duration_ms
status
error_code
```

Never log:

- passwords
- OTPs
- API keys
- secrets

Capture diagnostics for:

- browser actions
- state verification
- LLM calls
- provider calls
- rendering
- QA

---

# 67. Evaluation Framework

The benchmark must be a first-class part of ProductLens.

## Application categories

At minimum:

```text
CRM
Form Builder
Marketplace
Scheduler
Dashboard
Portfolio
Ecommerce
Admin Panel
Search/Filter Application
```

## Workflow complexity

```text
Simple
Moderate
Complex
Multi-step
```

## Interaction categories

```text
Navigation
Forms
Dropdowns
Date pickers
Phone fields
Search
Filters
Modals
Tables
Dynamic UI
Infinite scroll
Custom controls
```

---

# 68. Metrics

## Browser metrics

```text
task_success_rate
critical_action_success_rate
state_verification_rate
target_grounding_accuracy
average_repair_count
execution_time
model_calls
browser_actions
```

## Video metrics

```text
objective_completeness
story_coherence
cursor_smoothness
scroll_smoothness
camera_quality
viewport_quality
narration_quality
caption_sync
visual_readability
loading_handling
outcome_clarity
```

---

# 69. First-Pass Success Definition

A run counts as successful only if:

1. correct workflow selected
2. workflow completed
3. all critical postconditions passed
4. DemoTrace complete
5. final render succeeded
6. video QA passed
7. objective clearly demonstrated

A video that renders but demonstrates the wrong thing is a failure.

A browser run that succeeds but produces a poor video is a failure.

A beautiful video that contains incorrect actions is a failure.

---

# 70. Reliability Measurement

Run benchmark suites repeatedly.

Example:

```text
30 applications
×
3 representative objectives
×
10 repeated runs
=
900 runs
```

Measure:

```text
successful first-pass runs / total runs
```

The target is:

```text
≥95%
```

for the defined support envelope.

This is the point at which ProductLens can legitimately claim its own 95% reliability for that benchmark.

---

# 71. Failure Analysis

Every failure must answer:

```text
What failed?
Why did it fail?
Which layer owns the failure?
Can it be prevented?
Can it be repaired?
Does this expose an architectural weakness?
```

Examples:

```text
TARGET_RESOLUTION_FAILURE
→ grounding issue

STATE_VERIFICATION_FAILURE
→ application state misunderstood

PRESENTATION_FAILURE
→ DemoTrace-to-camera issue

NARRATION_FAILURE
→ script generation issue
```

Do not solve every failure with another prompt.

---

# 72. Development Order

This order is mandatory.

## Phase 0 — Benchmark + contracts

Build:

- benchmark suite
- domain models
- DemoPlan schema
- WorkflowStep schema
- DemoTrace schema
- PresentationPlan schema
- failure taxonomy
- state machine
- logging schema

Do not build the full UI.

---

## Phase 1 — Execution primitives

Build:

- Browserbase integration
- Stagehand v4 integration
- Playwright integration
- action adapters
- target grounding
- state verification
- traces
- screenshots
- browser video
- structured InteractionEvents

Use hardcoded semantic workflows.

### Gate

Browser primitives must pass the benchmark.

---

## Phase 2 — Workflow engine

Build:

```text
DemoPlan
 ↓
semantic operations
 ↓
execution
 ↓
verification
```

No AI planner yet.

### Gate

Known workflows must execute reliably.

---

## Phase 3 — Smart recording / DemoTrace

Build:

- event capture
- bounding boxes
- timestamps
- DOM snapshots
- viewport
- zoom
- scroll
- state transitions
- action result

### Gate

Every successful workflow must generate a complete trace.

---

## Phase 4 — Presentation engine

Build:

- camera
- cursor
- click effects
- scrolling
- zoom
- cuts
- loading compression

Use manually created DemoTrace fixtures first.

### Gate

A known-good trace must reliably become a polished video.

---

## Phase 5 — Remotion production

Build the reusable Remotion composition.

### Gate

Benchmark traces produce visually acceptable videos.

---

## Phase 6 — Narration

Build:

- script generation
- TTS adapter
- duration measurement
- timing
- captions

### Gate

Narration must match the actual DemoTrace.

---

## Phase 7 — Video QA

Build deterministic QA first.

Then multimodal QA.

### Gate

Known-bad videos must be rejected.

---

## Phase 8 — Product understanding

Build:

- application classification
- objective understanding
- targeted discovery
- feature detection
- workflow discovery
- exploration budgets

### Gate

User objectives produce valid candidate workflows.

---

## Phase 9 — Demo Planner

Build:

```text
objective
 ↓
candidate workflows
 ↓
scoring
 ↓
DemoPlan
```

### Gate

AI-generated plans must execute successfully through the existing engine.

---

## Phase 10 — Synthetic data intelligence

Build semantic form understanding and typed data generation.

### Gate

Representative form benchmark passes.

---

## Phase 11 — Persistent Product Knowledge

Add:

- knowledge versions
- successful action caching
- freshness
- reuse

Stagehand caching may be used where appropriate, but ProductLens should maintain its own higher-level semantic knowledge.

---

## Phase 12 — End-to-End

Only now:

```text
URL
+
objective
 ↓
ProductLens
 ↓
final video
```

---

# 73. What Must NOT Be Built Initially

Do not start with:

- full-product crawling
- multi-tab
- multi-application workflows
- advanced timeline editor
- multiple TTS providers
- multiple browser providers
- billing
- credits
- public sharing
- complex branding
- localization
- unrestricted autonomous agents
- manual repair editor

First prove:

```text
one application
+
one objective
+
one excellent demo
```

---

# 74. What We Should Use as the First Real ProductLens Test

The Smartsevak benchmark.

Do not start with an imaginary application.

Do not start with a toy Todo app.

Do not start by building the UI.

Run:

```text
Smartsevak
+
credentials
+
"Explore lead and bookings flow"
```

The target is to reproduce the successful Demosmith behavior as closely as possible.

Then repeat it.

Then intentionally perturb:

- viewport
- timing
- data
- browser state
- UI state
- session freshness

The system should still work within its supported envelope.

---

# 75. Critical Anti-Patterns

Never implement:

## Mega-agent

```text
One LLM does everything.
```

## Screenshot-only automation

```text
LLM sees image → guesses click coordinates.
```

## Blind retries

```text
failure → repeat same action
```

## Completion by queue exhaustion

```text
no tasks → success
```

## Rendering before verification

```text
browser probably worked → make video
```

## Full-product crawl by default

```text
URL → explore everything
```

## AI-generated camera coordinates

```text
LLM → arbitrary x/y/scale
```

## Raw browser recording as final output

```text
Playwright video → publish
```

---

# 76. Final Architecture

```text
                         USER
                          |
                   URL + OBJECTIVE
                          |
                          v
                   Goal Analyzer
                          |
                          v
                Targeted Discovery
                          |
                          v
                   Demo Planner
                          |
                       DemoPlan
                          |
                          v
                Workflow Validator
                          |
                          v
          +-------------------------------+
          | ProductLens Execution Engine  |
          |                               |
          | Semantic Operation             |
          |       ↓                       |
          | UI Grounding                  |
          |       ↓                       |
          | Playwright / Stagehand        |
          |       ↓                       |
          | State Verification            |
          |       ↓                       |
          | Semantic Result               |
          +-------------------------------+
                          |
                       DemoTrace
                          |
                          v
                  Presentation Director
                          |
                  PresentationPlan
                          |
             +------------+------------+
             |            |            |
          Camera       Cursor      Narration
             |            |            |
             +------------+------------+
                          |
                          v
                       Remotion
                          |
                          v
                    Rendered Video
                          |
                          v
                       Video QA
                          |
                    +-----+-----+
                    |           |
                   PASS        FAIL
                    |           |
                    v           v
                 DELIVER    TARGETED RETRY
```

---

# 77. The Most Important Engineering Rule

Do not ask:

> "Does the architecture look complete?"

Ask:

> **"What evidence proves this layer works?"**

For every subsystem:

```text
implementation
 ↓
benchmark
 ↓
measurement
 ↓
failure analysis
 ↓
improvement
 ↓
gate
 ↓
next subsystem
```

This is the main correction from the previous ProductLens plans.

---

# 78. Definition of Done

ProductLens V1 is not done when:

- the agent clicks
- a browser recording exists
- Remotion renders
- a video file is produced

V1 is done when:

```text
URL + objective
       ↓
ProductLens understands the relevant workflow
       ↓
executes it
       ↓
proves every critical state transition
       ↓
captures a complete DemoTrace
       ↓
creates a purposeful PresentationPlan
       ↓
generates contextual narration
       ↓
renders smooth camera/cursor/captions
       ↓
passes video QA
       ↓
returns a polished demo
```

And the system has demonstrated its reliability target on the benchmark.

---

# 79. Research / Reference Material

## Demosmith

Product:
https://demosmith.ai/

Demo catalog:
https://demosmith.ai/explore

The public product page currently describes autonomous navigation, contextual UI understanding, realistic form data, smart editing, UI-aware zooms, narration, captions, and a claimed 95% first-pass success rate. The 95% figure is a vendor claim and should not be treated as independently verified.

---

## Browserbase / Stagehand

Stagehand v4:
https://www.browserbase.com/blog/stagehand-v4

Stagehand documentation:
https://docs.browserbase.com/welcome/quickstarts/stagehand

Stagehand caching:
https://www.browserbase.com/blog/stagehand-caching

Playwright + Browserbase:
https://www.browserbase.com/templates/playwright

---

## Playwright

Actionability:
https://playwright.dev/docs/actionability

Assertions:
https://playwright.dev/docs/test-assertions

Tracing:
https://playwright.dev/docs/tracing

Videos:
https://playwright.dev/docs/videos

Best practices:
https://playwright.dev/docs/best-practices

---

## Remotion

Documentation:
https://www.remotion.dev/docs/

---

## Web-Agent Research

Web Agents Should Adopt the Plan-Then-Execute Paradigm:
https://arxiv.org/abs/2605.14290

WebArena:
https://arxiv.org/abs/2307.13854

WebVoyager:
https://arxiv.org/abs/2401.13919

WebAgent:
https://arxiv.org/abs/2307.12856

---

# 80. Final Position

The architecture should **not claim that 95% reliability is guaranteed**.

Instead:

> **95% is the acceptance target that ProductLens must demonstrate on a representative benchmark before we consider the system production-ready.**

The strongest evidence available today is that the desired product category already works in practice. Demosmith demonstrates this publicly and the supplied Smartsevak recording demonstrates it on a real custom application.

The engineering challenge for ProductLens is therefore not inventing an impossible technology.

It is reproducing the required capability with a system whose individual layers are measurable, observable, testable and independently reliable.

The most important architectural decision is:

> **ProductLens owns the semantic execution and verification loop; external browser/AI/video tools are components underneath it.**

The most important process decision is:

> **Do not implement the complete system and hope it works. Prove each layer before building the next one.**

The most important reliability decision is:

> **Never confuse browser action success, workflow success, recording success, presentation success, render success, and final demo success. They are separate states.**

And the most important product decision is:

> **Explore the relevant product area deeply, not the entire application broadly, unless the user explicitly requests a complete walkthrough.**

