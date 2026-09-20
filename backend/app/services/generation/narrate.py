from __future__ import annotations

import json
import re
from pathlib import Path

from app.artifacts.store import RunArtifacts
from app.contracts.models import (
    AudienceProfile,
    EditorialStoryboard,
    NarrationScript,
    NarrationSegment,
    OperationKind,
    ProductContext,
)
from app.narration.script import (
    bind_opening_to_first_event,
    captions_from_duration,
    recommended_caption_duration,
    script_from_moments,
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
        # Planning owns the approved editorial wording (enrich + preflight).
        # First-pass narration must reuse that persisted storyboard. Rebuilding
        # drafts here without enrich overwrites polished lines with skeleton
        # fallbacks, fails REPETITIVE_EDITORIAL_NARRATION, and wastes the
        # Browserbase credits already spent in EXECUTION.
        # Targeted narration repairs (refresh_editorial=True) rebuild from the
        # immutable plan/evidence and re-enrich so a stale writer cannot stick.
        persisted_storyboard = artifacts.presentation / "storyboard.json"
        if refresh_editorial:
            storyboard = build_editorial_storyboard(context, plan)
            if self.planner is None or self.planner.provider is None:
                raise ProviderError(
                    "openrouter",
                    None,
                    "ENRICH_PROVIDER_ERROR: structured provider required for narration refresh",
                )
            storyboard = await enrich_editorial_brief(context, storyboard, self.planner.provider)
            storyboard = await enrich_editorial_storyboard(
                context, storyboard, self.planner.provider
            )
        elif persisted_storyboard.is_file():
            storyboard = EditorialStoryboard.model_validate(
                json.loads(persisted_storyboard.read_text(encoding="utf-8"))
            )
        else:
            # No planning artifact — require live enrich; never ship draft skeletons.
            storyboard = build_editorial_storyboard(context, plan)
            if self.planner is None or self.planner.provider is None:
                raise ProviderError(
                    "openrouter",
                    None,
                    "ENRICH_PROVIDER_ERROR: structured provider required for narration",
                )
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
            scene_by_operation = {
                scene.operation_id: scene.id
                for scene in (storyboard.scenes if storyboard is not None else [])
                if scene.operation_id
            }
            if first_event is not None:
                product_title = (
                    " ".join(str(storyboard.brief.product_purpose).split())[:96]
                    if storyboard is not None
                    else "this visual workspace"
                )
                opening_scene = next(
                    (
                        scene
                        for scene in (storyboard.scenes if storyboard is not None else [])
                        if scene.operation_id == first_event.operation_id
                    ),
                    None,
                )
                opening_area = (
                    str(opening_scene.title).strip()
                    if opening_scene is not None and str(opening_scene.title).strip()
                    else "workspace"
                )
                script = [
                    {
                        "event_id": first_event.id,
                        # Keep the welcome on the first *real* scene.  An
                        # operation-less opening storyboard row cannot carry
                        # browser evidence and previously left the first
                        # canvas scene with a duplicated/generic welcome.
                        "scene_id": scene_by_operation.get(
                            first_event.operation_id, "opening"
                        ),
                        "opening": True,
                        "text": (
                            f"Welcome to {product_title}. Today we'll build a chat architecture "
                            f"on the {opening_area} canvas, then connect its components."
                        ),
                        "facts": [f"page:{first_event.page_url}"],
                    },
                    *[line for line in trace_script if str(line.get("event_id")) != first_event.id],
                ]
            events_by_id = {event.id: event for event in trace.events}
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
                    if candidate_event is not None and candidate_event.kind is OperationKind.KEY_PRESS:
                        candidate_text = (
                            str(candidate_gesture.get("text", "")).strip()
                            if isinstance(candidate_gesture, dict)
                            else ""
                        )
                        candidate_match = re.search(
                            r"(?:requested|observed)\s+(.+?)\s+label\b",
                            candidate_event.intent,
                            flags=re.IGNORECASE,
                        )
                        following_label = candidate_text or (
                            candidate_match.group(1).strip() if candidate_match else ""
                        )
                        if following_label:
                            break
                if bool(line.get("opening")):
                    # The opening line is the presenter welcome attached to
                    # the first proved event. Do not replace it with the
                    # low-level scroll/tool caption for that same event.
                    replacement = str(line.get("text") or replacement)
                elif event.kind is OperationKind.KEY_PRESS:
                    label_match = re.search(
                        r"(?:requested|observed)\s+(.+?)\s+label\b",
                        event.intent,
                        flags=re.IGNORECASE,
                    )
                    semantic_label = (
                        text_value
                        or (label_match.group(1).strip() if label_match else "")
                    )
                    if semantic_label:
                        replacement = (
                            f"The canvas now names the {semantic_label} component, making its role "
                            "explicit in the architecture."
                        )
                    else:
                        replacement = (
                            "The focused canvas editor now holds the requested text, making the "
                            "next architectural element visible."
                        )
                elif event.kind is OperationKind.CLICK and "text tool" in intent:
                    replacement = (
                        f"I select the observed text tool to prepare the {following_label or 'next'} "
                        "component label before placing it on the canvas."
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
                    else:
                        replacement = (
                            "I select the observed connector tool so the verified relationships "
                            "between the visible components can be drawn next."
                        )
                elif event.kind is OperationKind.POINTER_SEQUENCE:
                    match = re.search(
                        r"Connect the (?:requested|observed)\s+(.+?)\s+and\s+(.+?)\s+components",
                        event.intent,
                        flags=re.IGNORECASE,
                    )
                    if match:
                        replacement = (
                            f"I draw the verified connector arrow from {match.group(1).strip()} to "
                            f"{match.group(2).strip()}, making the data flow explicit."
                        )
                    else:
                        label_match = re.search(
                            r"(?:requested|observed)\s+(.+?)\s+label\b",
                            event.intent,
                            flags=re.IGNORECASE,
                        )
                        if label_match:
                            replacement = (
                                f"I place the {label_match.group(1).strip()} label at its observed "
                                "canvas position so the architecture can be read in sequence."
                            )
                        else:
                            replacement = (
                                "I draw the observed connection on the canvas, preserving the "
                                "relationship that makes the visual workflow understandable."
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
            # Visual-editor traces are intentionally rich: a complete
            # drawing journey includes every verified placement, label entry,
            # and connector.  ``script_from_trace`` may compress pointer
            # events for ordinary walkthroughs, which can leave the selected
            # last connector scene paired with the first connector's event
            # (and therefore fail scene/evidence QA).  Rebind visual captions
            # directly from the approved storyboard so every scene ID owns
            # its exact browser event and semantic narration.
            if storyboard is not None:
                events_by_operation = {
                    event.operation_id: event
                    for event in trace.events
                    if event.success and event.operation_id
                }
                visual_storyboard_lines: list[dict[str, object]] = []
                for scene in storyboard.scenes:
                    if not scene.operation_id:
                        continue
                    event = events_by_operation.get(scene.operation_id)
                    if event is None:
                        continue
                    visual_storyboard_lines.append(
                        {
                            "event_id": event.id,
                            "scene_id": scene.id,
                            "text": scene.narration,
                            "facts": list(scene.evidence[:8]),
                        }
                    )
                if visual_storyboard_lines:
                    first_line = visual_storyboard_lines[0]
                    opening_scene = next(
                        (
                            scene
                            for scene in storyboard.scenes
                            if scene.interaction == "opening" or scene.id == "opening"
                        ),
                        None,
                    )
                    if opening_scene is not None:
                        opening_text = str(opening_scene.narration or "").strip()
                        if opening_text and opening_text not in str(first_line["text"]):
                            first_line = {
                                **first_line,
                                "opening": True,
                                "text": f"{opening_text} {first_line['text']}".strip(),
                            }
                    script = [first_line, *visual_storyboard_lines[1:]]
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
        if storyboard is not None and not visual_request:
            # Prefer the deterministic storyboard over moment/provider scripts.
            # Moment scene ids do not match storyboard ids, so syncing by
            # scene_id left generic moment prose in editorial QA even after a
            # good storyboard existed.
            events_by_operation = {
                event.operation_id: event for event in trace.events if event.success
            }
            storyboard_script: list[dict] = []
            for scene in storyboard.scenes:
                if not str(scene.narration or "").strip():
                    continue
                if scene.operation_id and scene.operation_id in events_by_operation:
                    event = events_by_operation[scene.operation_id]
                    storyboard_script.append(
                        {
                            "event_id": event.id,
                            "scene_id": scene.id,
                            "text": scene.narration,
                            "facts": [str(item) for item in scene.evidence[:8]],
                        }
                    )
                elif scene.interaction == "opening" or scene.id == "opening":
                    anchor = next(
                        (
                            event
                            for event in trace.events
                            if event.success
                            and not str(event.operation_id or "").startswith("auth:")
                        ),
                        next((event for event in trace.events if event.success), None),
                    )
                    if anchor is not None:
                        storyboard_script.append(
                            {
                                "event_id": anchor.id,
                                "scene_id": scene.id,
                                "opening": True,
                                "text": scene.narration,
                                "facts": [str(item) for item in scene.evidence[:8]],
                            }
                        )
            if storyboard_script:
                auth_lines = [
                    line
                    for line in script
                    if str(line.get("scene_id") or "").startswith("auth:")
                ]
                # Fold the operation-less opening welcome into the first
                # product scene instead of emitting a second script row on the
                # same event id (that duplicates the first product beat and
                # fails SCRIPT_TRACE_MISMATCH / CAPTION_WITHOUT_PRESENTATION_BEAT).
                opening_text = next(
                    (
                        str(scene.narration).strip()
                        for scene in storyboard.scenes
                        if (scene.interaction == "opening" or scene.id == "opening")
                        and str(scene.narration or "").strip()
                    ),
                    "",
                )
                product_lines = [
                    line
                    for line in storyboard_script
                    if not (
                        line.get("opening")
                        or str(line.get("scene_id") or "") in {"opening"}
                    )
                ]
                if opening_text and product_lines:
                    first = dict(product_lines[0])
                    first_text = str(first.get("text") or "").strip()
                    if opening_text not in first_text:
                        first["text"] = f"{opening_text} {first_text}".strip()
                    first["opening"] = True
                    product_lines = [first, *product_lines[1:]]
                script = [*auth_lines, *product_lines]
        elif trace.moments and not visual_request:
            script = script_from_moments(
                trace,
                product_title=context.title or "the product",
                product_purpose=(
                    storyboard.brief.product_purpose if storyboard is not None else ""
                ),
            )
        if storyboard and script:
            # The approved storyboard is the narration source of truth for
            # product scenes. Moment/provider scripts can still carry older
            # generic route sentences after the deterministic storyboard has
            # been rebuilt; evaluate and render the storyboard wording.
            storyboard_text = {scene.id: scene.narration for scene in storyboard.scenes}
            operation_to_scene = {
                str(scene.operation_id): scene.id
                for scene in storyboard.scenes
                if scene.operation_id
            }
            events_by_id = {event.id: event for event in trace.events}

            def _storyboard_line_text(line: dict) -> str:
                scene_id = str(line.get("scene_id") or "")
                if scene_id in storyboard_text and str(storyboard_text[scene_id]).strip():
                    return str(storyboard_text[scene_id])
                event = events_by_id.get(str(line.get("event_id") or ""))
                if event is not None:
                    mapped = operation_to_scene.get(str(event.operation_id or ""))
                    if mapped and str(storyboard_text.get(mapped) or "").strip():
                        return str(storyboard_text[mapped])
                return str(line.get("text") or "")

            script = [
                {
                    **line,
                    "text": (
                        str(line.get("text") or "")
                        if (
                            str(line.get("scene_id") or "").startswith("auth:")
                            or bool(line.get("opening"))
                        )
                        else _storyboard_line_text(line)
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
                            update={
                                "narration": script_by_scene.get(scene.id, scene.narration),
                            }
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

