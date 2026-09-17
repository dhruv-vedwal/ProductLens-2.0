from __future__ import annotations

import json
import os
import re
from pathlib import Path

from app.artifacts.store import RunArtifacts
from app.contracts.models import (
    AudienceProfile,
    NarrationScript,
    NarrationSegment,
    OperationKind,
    ProductContext,
)
from app.narration.script import (
    bind_opening_to_first_event,
    captions_from_duration,
    recommended_caption_duration,
    script_from_trace,
)
from app.narration.service import NarrationService
from app.presentation.director import build_presentation_plan
from app.presentation.editorial import (
    bind_storyboard_events,
    build_editorial_storyboard,
    editorial_script,
    enrich_editorial_brief,
    enrich_editorial_storyboard,
)
from app.presentation.journey import build_journey
from app.presentation.scenes import build_scene_plan
from app.providers.errors import ProviderError
from app.quality.consistency import validate_selected_candidate_consistency
from app.quality.editorial import inspect_editorial
from app.quality.repair import classify_repair

from .render import GenerationPreconditionError


def _narration_script_contract(
    script: list[dict[str, object]],
    *,
    mode: str,
    audience: str,
    audience_profile: AudienceProfile | None = None,
    captions: list[dict[str, object]] | None = None,
) -> NarrationScript:
    """Validate and normalize the single script shared by every presentation layer."""
    timing_by_scene = {
        str(item.get("scene_id") or item.get("event_id")): item
        for item in (captions or [])
        if item.get("scene_id") or item.get("event_id")
    }
    segments: list[NarrationSegment] = []
    for line in script:
        event_id = str(line.get("event_id") or "").strip()
        if not event_id:
            raise GenerationPreconditionError(
                "NARRATION_SCRIPT_INVALID: every line needs an event_id"
            )
        scene_id = str(line.get("scene_id") or event_id).strip()
        facts = line.get("facts", [])
        evidence = (
            [str(item) for item in facts if isinstance(item, str)]
            if isinstance(facts, list)
            else []
        )
        # Deterministic trace fallbacks carry browser-state facts as a mapping
        # rather than a list of editorial IDs. Bind those lines to their
        # immutable event witness instead of allowing an evidence-free script
        # to cross into captions/TTS.
        if not evidence:
            evidence = [f"trace:event:{event_id}"]
        # A page-local scene can legitimately cite many section/element refs,
        # but the public narration contract intentionally caps evidence IDs so
        # artifacts stay bounded. Preserve stable order and provenance rather
        # than failing an otherwise valid script on a dense page inventory.
        evidence = list(dict.fromkeys(evidence))[:24]
        timing = timing_by_scene.get(scene_id) or timing_by_scene.get(event_id) or {}
        segments.append(
            NarrationSegment(
                scene_id=scene_id,
                event_id=event_id,
                text=str(line.get("text") or "").strip(),
                evidence=evidence,
                facts=facts if isinstance(facts, (list, dict)) else [],
                opening=bool(line.get("opening", False)),
                start_seconds=float(timing["start"]) if timing.get("start") is not None else None,
                end_seconds=float(timing["end"]) if timing.get("end") is not None else None,
            )
        )
    return NarrationScript(
        mode="tts" if mode == "tts" else "caption_only",
        audience=audience,
        audience_profile=audience_profile or AudienceProfile(),
        timing_owner="measured_audio" if mode == "tts" else "scene",
        segments=segments,
    )

class NarrateMixin:
    async def narration_stage(
        self,
        *,
        run_id: str,
        artifact_root: Path,
        refresh_editorial: bool = False,
        include_audio: bool = True,
    ) -> dict:
        artifacts = RunArtifacts(artifact_root, run_id)
        trace = self._load_trace(artifacts)
        # Regenerate deterministic editorial copy from the persisted evidence at
        # narration time. This lets a narration-only repair improve prose
        # without replaying browser actions or invalidating the DemoTrace.
        context = ProductContext.model_validate(
            json.loads(
                (artifacts.root / "discovery" / "product-context.json").read_text(encoding="utf-8")
            )
        )
        plan = self._load_plan(artifacts)
        plan_consistency_failures = validate_selected_candidate_consistency(
            plan.model_dump(mode="json")
        )
        artifacts.write_json(
            "qa/plan-consistency-report.json",
            {
                "status": "pass" if not plan_consistency_failures else "failed",
                "hard_failures": plan_consistency_failures,
                "selected_workflow": plan.selected_workflow,
            },
        )
        # Planning owns the approved editorial wording. Narration must never
        # rebuild a deterministic fallback over an already enriched storyboard:
        # that silently discards the evidence-reviewed script and changes scene
        # timing without a planning repair.
        # Rebuild from the immutable plan/evidence on every narration pass.
        # A persisted storyboard may have been produced by an older writer or
        # contain a rejected route-label line; loading it during a targeted
        # retry would make the repair boundary stale and defeat deterministic
        # editorial validation.  The trace, plan, and scene IDs remain the
        # authorities, so this refresh cannot invent browser actions.
        storyboard = build_editorial_storyboard(context, plan)
        # A targeted narration repair intentionally starts from the
        # deterministic evidence-bound storyboard. The original planning pass
        # already had an opportunity to use OpenRouter; spending another pair
        # of model calls on a rejected script can reintroduce label dumps. A
        # caller may explicitly opt into a second editorial model pass through
        # the environment when investigating a provider/model regression.
        allow_repair_editorial_model = os.getenv(
            "PRODUCTLENS_REPAIR_EDITORIAL_WITH_LLM", "false"
        ).lower() in {"1", "true", "yes"}
        if (
            refresh_editorial
            and allow_repair_editorial_model
            and self.planner is not None
            and self.planner.provider is not None
        ):
            storyboard = await enrich_editorial_brief(context, storyboard, self.planner.provider)
            storyboard = await enrich_editorial_storyboard(
                context, storyboard, self.planner.provider
            )
        storyboard = bind_storyboard_events(
            storyboard, {event.operation_id for event in trace.events if event.success}
        )
        artifacts.write_json(
            "presentation/editorial-brief.json", storyboard.brief.model_dump(mode="json")
        )
        artifacts.write_json(
            "narration/fact-extraction.json",
            {
                "schema_version": 1,
                "source": "persisted-page-knowledge-and-editorial-brief",
                "facts": [item.model_dump(mode="json") for item in storyboard.brief.facts],
                "excluded_areas": storyboard.brief.excluded_areas,
            },
        )
        artifacts.write_json("presentation/storyboard.json", storyboard.model_dump(mode="json"))
        # Rebuild the presentation contract from the immutable trace on every
        # narration/presentation retry.  Camera and cursor policy is code, not
        # evidence; retaining a plan generated by an older renderer version
        # would let a fixed safety bound (or a cursor alignment fix) be silently
        # bypassed by a repair that only regenerated captions.  The trace and
        # scene IDs remain unchanged, so this refresh cannot invent an action.
        scene_plan_path = artifacts.presentation / "scene-plan.json"
        # A narration/presentation repair must also refresh scene policy from
        # the immutable trace. Retaining an older scene-plan silently keeps
        # stale caption-safe zones (or camera bounds) after a renderer fix,
        # exactly the kind of drift the trace-only repair path is meant to
        # prevent.
        if refresh_editorial:
            scene_plan_payload = build_scene_plan(trace, storyboard=storyboard)
        else:
            try:
                scene_plan_payload = json.loads(scene_plan_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                scene_plan_payload = build_scene_plan(trace, storyboard=storyboard)
        if refresh_editorial:
            artifacts.write_json("presentation/scene-plan.json", scene_plan_payload)
            # The validated scene plan is the renderer's authoritative
            # presentation contract. Keep it in lockstep with a narration
            # repair; otherwise a trace-only rerender can silently consume
            # an older caption-safe-zone/camera policy even though the fresh
            # scene-plan artifact looks correct.
            refreshed_journey = build_journey(trace, scene_plan_payload)
            artifacts.write_json("presentation/validated-scene-plan.json", refreshed_journey)
        first_viewport = next(
            (event.viewport for event in trace.events if event.viewport is not None),
            None,
        )
        if first_viewport is not None:
            refreshed_presentation = build_presentation_plan(
                trace,
                viewport_width=first_viewport.width,
                viewport_height=first_viewport.height,
                allow_camera_zoom=True,
                scene_plan=scene_plan_payload if isinstance(scene_plan_payload, list) else None,
            )
            artifacts.write_json(
                "presentation/presentation-plan.json",
                refreshed_presentation.model_dump(mode="json"),
            )
            artifacts.write_json(
                "presentation/cursor-plan.json",
                {"paths": refreshed_presentation.cursor_paths},
            )
        first_successful_event = next((event for event in trace.events if event.success), None)
        script = (
            editorial_script(
                storyboard,
                {event.operation_id: event.id for event in trace.events if event.success},
                opening_event_id=first_successful_event.id if first_successful_event else None,
            )
            if storyboard
            else script_from_trace(trace)
        )
        # Compatibility traces without an editorial storyboard still need the
        # same opening-at-zero guarantee.
        if storyboard is None:
            script = bind_opening_to_first_event(script, trace)
        # Visual-editor workflows need a presenter narrative tied to the
        # artifact being built, not a sequence of tool labels.  Derive these
        # lines from the verified event intent/gesture and the user's
        # requested component text; this remains product-neutral and avoids
        # repeating "select Text" / "use the canvas" captions.
        visual_request = bool(
            re.search(
                r"\b(?:draw|drawing|diagram|architecture|whiteboard|canvas|flowchart)\b",
                trace.objective.casefold(),
            )
        )
        if visual_request:
            # The generic storyboard intentionally suppresses low-value
            # observe beats for ordinary pages.  In an editor build, however,
            # each verified text placement is a material part of the artifact
            # and must own a caption.  Start from the complete trace so the
            # script cannot silently collapse the creation journey.
            trace_script = script_from_trace(trace, audience="technical engineer")
            first_event = next((event for event in trace.events if event.success), None)
            if first_event is not None:
                product_title = (
                    " ".join(str(storyboard.brief.product_purpose).split())[:96]
                    if storyboard is not None
                    else "this visual workspace"
                )
                script = [
                    {
                        "event_id": first_event.id,
                        "scene_id": "opening",
                        "opening": True,
                        "text": (
                            f"Welcome to {product_title}. Today I'll show how a requested "
                            "architecture takes shape directly on the canvas."
                        ),
                        "facts": [f"page:{first_event.page_url}"],
                    },
                    *[line for line in trace_script if str(line.get("event_id")) != first_event.id],
                ]
            events_by_id = {event.id: event for event in trace.events}
            scene_by_operation = {
                scene.operation_id: scene.id
                for scene in (storyboard.scenes if storyboard is not None else [])
                if scene.operation_id
            }
            visual_lines: list[dict[str, object]] = []
            for line_index, line in enumerate(script):
                event = events_by_id.get(str(line.get("event_id")))
                if event is None:
                    visual_lines.append(line)
                    continue
                intent = event.intent.casefold()
                gesture = event.after.get("gesture") if isinstance(event.after, dict) else None
                text_value = (
                    str(gesture.get("text", "")).strip() if isinstance(gesture, dict) else ""
                )
                replacement = str(line.get("text") or "")
                # The text-tool click is meaningful because it precedes a
                # specific component label.  Carry that observed value into
                # the sentence so repeated tool clicks remain distinct,
                # conversational beats instead of identical UI instructions.
                following_label = ""
                for candidate in script[line_index + 1 :]:
                    candidate_event = events_by_id.get(str(candidate.get("event_id")))
                    candidate_gesture = (
                        candidate_event.after.get("gesture")
                        if candidate_event is not None and isinstance(candidate_event.after, dict)
                        else None
                    )
                    if (
                        candidate_event is not None
                        and candidate_event.kind is OperationKind.KEY_PRESS
                        and isinstance(candidate_gesture, dict)
                        and str(candidate_gesture.get("text", "")).strip()
                    ):
                        following_label = str(candidate_gesture["text"]).strip()
                        break
                if bool(line.get("opening")):
                    # The opening line is the presenter welcome attached to
                    # the first proved event. Do not replace it with the
                    # low-level scroll/tool caption for that same event.
                    replacement = str(line.get("text") or replacement)
                elif event.kind is OperationKind.KEY_PRESS and text_value:
                    replacement = (
                        f"The canvas now names the {text_value} component, making its role "
                        "explicit in the architecture."
                    )
                elif event.kind is OperationKind.CLICK and "label" in intent:
                    match = re.search(
                        r"\blabel\s+(.+?)(?:\s+on the observed|\s*$)",
                        event.intent,
                        re.IGNORECASE,
                    )
                    label = match.group(1).strip() if match else "component"
                    label = re.sub(r"^(?:for|the)\s+", "", label, flags=re.IGNORECASE).strip()
                    replacement = (
                        f"I place the {label} label on the canvas so the architecture can be "
                        "read at a glance."
                    )
                elif event.kind is OperationKind.CLICK and "text tool" in intent:
                    replacement = (
                        f"I select the observed text tool to name the {following_label or 'next'} "
                        "architectural role directly where it belongs."
                    )
                elif event.kind is OperationKind.CLICK and any(
                    token in intent for token in ("arrow", "connector", "line")
                ):
                    match = re.search(
                        r"connect\s+(.+?)\s+to\s+(.+?)(?:\s*$|\s+on\b)",
                        event.intent,
                        flags=re.IGNORECASE,
                    )
                    if match:
                        replacement = (
                            f"I select the observed {event.target.name if event.target else 'connector'} "
                            f"tool to connect {match.group(1).strip()} to {match.group(2).strip()}."
                        )
                elif event.kind is OperationKind.POINTER_SEQUENCE:
                    match = re.search(
                        r"Connect the observed\s+(.+?)\s+and\s+(.+?)\s+components",
                        event.intent,
                        flags=re.IGNORECASE,
                    )
                    if match:
                        replacement = (
                            f"I draw the arrow from {match.group(1).strip()} to "
                            f"{match.group(2).strip()}, making the data flow explicit."
                        )
                    else:
                        replacement = (
                            "I sketch the first visual connection on the canvas, establishing "
                            "the workspace where the architecture will take shape."
                        )
                elif event.kind is OperationKind.SCROLL_TO:
                    replacement = (
                        "I orient the viewer to the canvas and its visible tool palette "
                        "before drawing."
                    )
                visual_lines.append(
                    {
                        **line,
                        "scene_id": scene_by_operation.get(
                            event.operation_id, line.get("scene_id", event.id)
                        ),
                        "text": replacement,
                    }
                )
            script = visual_lines
        # Authentication is part of the visible journey whenever a clean
        # production context had to sign in, even if the user phrased the
        # objective as an already-authenticated experience.  Keep these
        # presenter lines credential-free and bind them to the real auth
        # events so the opening never leaves an unexplained silent login gap.
        auth_events = [
            event
            for event in trace.events
            if event.success and str(event.operation_id or "").startswith("auth:")
        ]
        if auth_events:
            auth_lines = []
            requested_subject = ""
            if context.objective is not None:
                requested_subject = str(context.objective.primary_entity or "").strip()
            requested_subject = re.sub(
                r"^(?:(?:the|a|an|authenticated|operational|relevant|requested|actual|visible|current|primary)\s+)+",
                "",
                requested_subject,
                flags=re.IGNORECASE,
            ).strip()
            requested_subject = requested_subject or "requested workflow"
            for event in auth_events:
                operation_id = str(event.operation_id)
                if operation_id.endswith(":username"):
                    text = "We begin by entering the account email so this walkthrough reflects a real authenticated workspace."
                elif operation_id.endswith(":password"):
                    text = "The password is entered securely and kept out of the recording, preserving a safe demonstration."
                else:
                    text = f"With sign-in complete, the authenticated workspace is ready for the {requested_subject}."
                auth_lines.append(
                    {
                        "event_id": event.id,
                        "scene_id": operation_id,
                        "text": text,
                        "facts": ["auth:credential-entry"],
                    }
                )
            script = [
                *auth_lines,
                *[
                    line
                    for line in script
                    if str(line.get("event_id")) not in {item["event_id"] for item in auth_lines}
                ],
            ]
        if storyboard and script:
            # The approved script is the narration source of truth. Keep the
            # storyboard copy synchronized before editorial QA so a targeted
            # narration repair (including placeholder cleanup) is evaluated
            # against the exact text that will be rendered, not stale model
            # prose persisted by an earlier attempt.
            storyboard_text = {scene.id: scene.narration for scene in storyboard.scenes}

            def _is_route_label_line(value: str) -> bool:
                return bool(
                    re.search(
                        r"\b(?:view|page)\s+brings\b.*\binto\s+view\b|"
                        r"\bshowing\s+how\s+this\s+part\s+of\s+the\s+product\s+is\s+organized\b",
                        value,
                        flags=re.IGNORECASE,
                    )
                )

            # A compatibility/provider script can carry an older generic
            # route sentence even after the deterministic storyboard has been
            # rebuilt. Keep one evidence-bound source of truth by replacing
            # only that rejected line; all other approved script timing and
            # scene identity remain untouched.
            script = [
                {
                    **line,
                    "text": (
                        storyboard_text.get(str(line.get("scene_id")), str(line.get("text") or ""))
                        if _is_route_label_line(str(line.get("text") or ""))
                        else line.get("text")
                    ),
                }
                for line in script
            ]
            script_by_scene = {
                str(line.get("scene_id")): str(line.get("text") or "")
                for line in script
                if line.get("scene_id") and str(line.get("text") or "").strip()
            }
            storyboard = storyboard.model_copy(
                update={
                    "scenes": [
                        scene.model_copy(
                            update={"narration": script_by_scene.get(scene.id, scene.narration)}
                        )
                        for scene in storyboard.scenes
                    ]
                }
            )
            artifacts.write_json("presentation/storyboard.json", storyboard.model_dump(mode="json"))
        editorial = inspect_editorial(
            context=context, plan=plan, trace=trace, storyboard=storyboard, script=script
        )
        artifacts.write_json("qa/editorial-report.json", editorial)
        if editorial["hard_failures"]:
            artifacts.write_json(
                "qa/repair-decision.json",
                classify_repair(editorial["hard_failures"]).model_dump(mode="json"),
            )
            raise GenerationPreconditionError(
                f"EDITORIAL_QA_REJECTED: {editorial['hard_failures']}"
            )
        # Caption-only delivery still needs to cover the complete rendered
        # story.  A word-count estimate can be shorter than the browser edit
        # (especially when a quiet page-read beat is retained), leaving an
        # unexplained silent tail and captions that fail the reader dwell
        # gate.  Use the objective's approved minimum as the lower bound; the
        # renderer remains responsible for the actual visual duration.
        recommended_duration = recommended_caption_duration(script)
        objective_minimum = float(plan.minimum_duration_seconds or 0)
        captions = captions_from_duration(script, max(recommended_duration, objective_minimum))
        narration = None
        if self.speech_provider and include_audio:
            try:
                narration = await NarrationService().create(
                    trace,
                    self.speech_provider,
                    artifacts.root / "audio" / "narration.mp3",
                    script=script,
                )
                script, captions = narration["script"], narration["captions"]
            except ProviderError:
                narration = None
        mode = "tts" if narration else "caption_only"
        approved_script = _narration_script_contract(
            script,
            mode=mode,
            audience=str(
                getattr(getattr(context, "objective", None), "audience", "product prospect")
            ),
            audience_profile=getattr(getattr(context, "objective", None), "audience_profile", None),
            captions=captions,
        )
        # Keep the historical ``script`` list for API compatibility while
        # adding the versioned contract metadata and measured segment timing.
        payload = {
            "schema_version": approved_script.schema_version,
            "mode": approved_script.mode,
            "timing_owner": approved_script.timing_owner,
            "audience": approved_script.audience,
            "audience_profile": approved_script.audience_profile.model_dump(mode="json"),
            "script": [segment.model_dump(mode="json") for segment in approved_script.segments],
        }
        artifacts.write_json("presentation/captions.json", captions)
        artifacts.write_json("presentation/narration-script.json", payload)
        artifacts.write_json("narration/editorial-script.json", payload)
        return {
            **payload,
            "captions": captions,
            "audio_path": narration and narration["audio_path"],
        }

