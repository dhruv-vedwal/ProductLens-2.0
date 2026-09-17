"""Editorial storyboard construction and LLM enrichment."""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlsplit

from app.contracts.models import (
    DemoPlan,
    EditorialBrief,
    EditorialNarrationDraft,
    EditorialScene,
    EditorialStoryboard,
    OperationKind,
    ProductContext,
)
from app.observability.logging import redact_prompt_text
from app.presentation.editorial.narrative import *
from app.providers.errors import ProviderError


def build_editorial_storyboard(context: ProductContext, plan: DemoPlan) -> EditorialStoryboard:
    """Build a deterministic story from observed text and the validated plan.

    Model-created prose may improve this later, but this artifact is intentionally
    useful on its own: every statement is traceable to current discovery evidence.
    """
    opening_page = context.page_knowledge[0] if context.page_knowledge else None
    purpose = _clean(context.title or "Product walkthrough")
    objective_subject = _objective_subject(context)
    opening_fact = next(
        (
            prose
            for _, prose in _page_fact_records(opening_page)
            if not _looks_like_label_collection(prose)
            and _safe_editorial_text(prose)
            and not re.search(
                r"\b(?:chevron|notification|dashboard highlights)\b", prose, re.IGNORECASE
            )
        ),
        None,
    )
    intro_summary = _summary_from_intro(
        _safe_editorial_text(opening_fact or context.visible_text),
        objective_subject or purpose.split("|", 1)[0].strip() or "product experience",
    )
    # A dense dashboard shell can technically satisfy the generic checklist
    # heuristic while saying nothing about the requested product area. The
    # objective is authoritative in that case; retain a concise, truthful
    # orientation rather than narrating a generic "opening view" sentence.
    opening = (
        intro_summary
        or opening_fact
        or (
            f"The {objective_subject} workspace is ready for a focused walkthrough."
            if objective_subject
            else ""
        )
        or (
            f"The opening view establishes {purpose}"
            + (
                f" through {', '.join(labels)}."
                if opening_page
                and (
                    labels := [
                        label
                        for section in opening_page.visible_sections[:6]
                        if (label := _meaningful_section_label(section)) and len(label.split()) >= 2
                    ][:3]
                )
                else "."
            )
        )
        or _clean(context.title)
        or context.title
    )
    nav = [item.name for item in context.navigation if item.name][:8]
    brief = EditorialBrief(
        title=f"{_concise_title(purpose)} walkthrough",
        product_purpose=purpose,
        opening_message=opening,
        navigation_order=nav,
        facts=_facts(context),
        excluded_areas=["external links", "destructive actions", "unverified claims"],
    )
    scenes: list[EditorialScene] = [
        EditorialScene(
            id="opening",
            title="Opening view",
            purpose="Establish the product before any interaction.",
            # Give every walkthrough a presenter-led opening.  The product
            # name comes from the observed browser title and the following
            # sentence is the evidence-grounded opening message; this avoids
            # dropping viewers into a page with a bare DOM description.
            narration=_fallback_presenter_intro(purpose, opening, objective_subject),
            evidence=[f"page:{context.url}"],
            interaction="opening",
            required_dwell_seconds=5.0,
            completion_criteria=["initial product view is stable", "opening message is readable"],
        )
    ]
    opening_operation = plan.workflow_steps[0].operation if plan.workflow_steps else None
    opening_destination = (
        _page_for_operation(context, opening_operation)
        if opening_operation is not None
        else opening_page
    )
    # The discovery URL is often the authenticated shell, while a focused
    # workflow plan intentionally starts on the requested destination.  The
    # opening chapter must describe the state the viewer actually sees after
    # authentication/navigation, never the stale post-login shell.  Rebind
    # its fact and evidence to the validated plan boundary before enrichment.
    if opening_destination is not None and (
        opening_page is None
        or _canonical_page_url(opening_destination.url) != _canonical_page_url(opening_page.url)
    ):
        destination_fact = next(
            (
                prose
                for _, prose in _page_fact_records(opening_destination)
                if not _looks_like_label_collection(prose)
                and _safe_editorial_text(prose)
                and not re.search(
                    r"\b(?:chevron|notification|dashboard highlights)\b", prose, re.IGNORECASE
                )
            ),
            None,
        )
        destination_summary = _summary_from_intro(
            _safe_editorial_text(destination_fact or " ".join(opening_destination.visible_facts)),
            objective_subject or purpose.split("|", 1)[0].strip() or "product experience",
        )
        destination_opening = (
            destination_summary
            or destination_fact
            or (
                f"The {objective_subject} workspace is ready for a focused walkthrough."
                if objective_subject
                else ""
            )
            or f"The opening view establishes {_clean(opening_destination.purpose or opening_destination.title or purpose)}"
        )
        scenes[0] = scenes[0].model_copy(
            update={
                "narration": _fallback_presenter_intro(
                    purpose, destination_opening, objective_subject
                ),
                "evidence": [f"page:{opening_destination.url}"],
                "page_url": opening_destination.url,
            }
        )
    opening_bridge = _exploration_context_bridge(context, opening_destination)
    if opening_bridge is not None:
        bridge_copy, bridge_evidence = opening_bridge
        scenes[0] = scenes[0].model_copy(
            update={
                # Both fragments are already validated complete sentences. Do not
                # re-run their combined copy through the short fact normalizer: it
                # can clip the final relationship sentence at its length boundary.
                "narration": f"{scenes[0].narration} {bridge_copy}",
                "evidence": [*scenes[0].evidence, bridge_evidence],
            }
        )
    # Authentication is an optional, objective-driven chapter.  The execution
    # layer emits these stable operation IDs only when a login form is actually
    # encountered, so an already-authenticated run cleanly omits the scenes.
    # No credential value is ever included in the storyboard or narration.
    objective_text = str(getattr(context.objective, "raw", "") or plan.objective).casefold()
    if "login" in objective_text or "sign in" in objective_text or "sign-in" in objective_text:
        scenes.extend(
            [
                EditorialScene(
                    id="authentication-username",
                    operation_id="auth:username",
                    title="Email address",
                    purpose="Begin the authenticated workspace session.",
                    narration="We start by signing in so the walkthrough reflects the workspace a real team member would use.",
                    evidence=[f"page:{context.url}", "auth:credential-entry"],
                    interaction="observe",
                    required_dwell_seconds=2.0,
                    completion_criteria=["email field is visibly completed"],
                    story_phase="context",
                    page_url=context.url,
                ),
                EditorialScene(
                    id="authentication-password",
                    operation_id="auth:password",
                    title="Password",
                    purpose="Complete authentication without exposing the secret.",
                    narration="The sign-in form is completed securely before the product workspace opens.",
                    evidence=[f"page:{context.url}", "auth:credential-entry"],
                    interaction="observe",
                    required_dwell_seconds=2.0,
                    completion_criteria=["password field remains redacted"],
                    story_phase="context",
                    page_url=context.url,
                ),
                EditorialScene(
                    id="authentication-submit",
                    operation_id="auth:submit",
                    title="Sign in",
                    purpose="Enter the authenticated product experience.",
                    narration="The workspace is now authenticated, and we can move into the requested product flow.",
                    evidence=[f"page:{context.url}", "auth:submit"],
                    interaction="click",
                    required_dwell_seconds=2.5,
                    completion_criteria=["authenticated workspace is visible"],
                    story_phase="enter",
                    page_url=context.url,
                ),
            ]
        )
    total_steps = len(plan.workflow_steps)
    for index, step in enumerate(plan.workflow_steps, start=1):
        operation = step.operation
        if (
            index == 1
            and operation.kind is OperationKind.NAVIGATE
            and (str(operation.value or "").rstrip("/") == context.url.rstrip("/"))
        ):
            # The opening scene already establishes the initial route; do not
            # create a duplicate generic "next view" chapter for its load.
            continue
        page = _page_for_operation(context, operation)
        if (
            operation.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}
            and operation.target is not None
        ):
            # The execution planner stores the source page on a visible link
            # target.  For narration, however, this scene must describe the
            # destination that the viewer is about to see. Recover the
            # same-origin href from the observed semantic selector and bind to
            # that PageKnowledge record, avoiding source-shell captions such
            # as ``Dashboard brings Lead Management into view``.
            selector = str(operation.target.selector or "")
            href_match = re.search(r"href=['\"]([^'\"]+)['\"]", selector)
            if href_match:
                destination = _canonical_page_url(urljoin(context.url, href_match.group(1)))
                page = next(
                    (
                        candidate
                        for candidate in context.page_knowledge
                        if _canonical_page_url(candidate.url) == destination
                        or urlsplit(_canonical_page_url(candidate.url)).path.rstrip("/")
                        == urlsplit(destination).path.rstrip("/")
                    ),
                    page,
                )
            if page is None or _canonical_page_url(page.url) == _canonical_page_url(context.url):
                expected_destination = next(
                    (
                        str(condition.expected)
                        for condition in operation.postconditions
                        if getattr(condition, "kind", "") == "url" and condition.expected
                    ),
                    "",
                )
                if expected_destination:
                    destination = _canonical_page_url(urljoin(context.url, expected_destination))
                    page = next(
                        (
                            candidate
                            for candidate in context.page_knowledge
                            if _canonical_page_url(candidate.url) == destination
                            or urlsplit(_canonical_page_url(candidate.url)).path.rstrip("/")
                            == urlsplit(destination).path.rstrip("/")
                        ),
                        page,
                    )
        all_page_facts = " ".join(str(item) for item in getattr(page, "visible_facts", []) or [])
        # A route navigation without a DOM target still has a destination
        # page.  Use that observed page identity as the scene subject instead
        # of inventing ``the next view``; the latter is tour mechanics, cannot
        # be grounded, and produces a misleading caption for every product.
        target = (
            operation.target.name
            if operation.target
            else _clean(
                str(getattr(page, "purpose", "") or getattr(page, "title", "") or "this workspace"),
                96,
            )
        )
        # Canvas/editor actions often expose the application shell as their
        # DOM target while the operation intent names the semantic object being
        # created. Follow that observed object in the scene contract so
        # captions and QA describe what the viewer sees, not an implementation
        # surface. The rule is vocabulary-free and applies to any visual
        # editor.
        if operation.kind in {OperationKind.KEY_PRESS, OperationKind.POINTER_SEQUENCE}:
            semantic_match = re.search(
                r"(?:requested|observed)\s+(.+?)\s+label\b",
                operation.intent,
                flags=re.IGNORECASE,
            )
            if semantic_match:
                target = _clean(f"{semantic_match.group(1).strip()} label", 96)
        # Placeholder copy is an implementation hint, not a viewer-facing
        # chapter title. Normalize it to the semantic field being shown so
        # captions never read out example values or ellipses verbatim.
        if re.search(r"\([^)]{2,}\)|\.\.\.$", target):
            placeholder_target = re.sub(r"\s*\([^)]*\)", "", target).strip(" .:-")
            placeholder_target = re.sub(
                r"^(?:type|enter|fill|search|choose)\s+",
                "",
                placeholder_target,
                flags=re.IGNORECASE,
            )
            placeholder_target = re.sub(
                r"^(?:a|an|the)\s+", "", placeholder_target, flags=re.IGNORECASE
            )
            if placeholder_target:
                target = _clean(f"{placeholder_target} input", 96)
        target = target.rstrip(" :.-") or target
        target_words = re.findall(r"[a-z0-9]{4,}", target.lower())
        normalized_target = " ".join(target.split()).lower()
        observed = _observed_narration(context, operation, target)
        if re.search(
            r"\b(?:click|press|tap|select)\b.*\b(?:to|then|and)\b", observed, re.IGNORECASE
        ):
            observed = _human_sentence(
                f"The {target} area groups the product's available capabilities, giving the viewer a clear map of what follows"
            )
        if (
            operation.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}
            and page is not None
        ):
            # A destination scene must establish its visible *purpose*, not
            # narrate the tab the cursor just clicked. Prefer a readable fact
            # from the destination page. If discovery has no such fact the
            # conservative fallback remains grounded in its page identity and
            # is later rejected by editorial QA if it cannot explain value.
            destination_fact = next(
                (prose for _, prose in _page_fact_records(page) if not _is_metadata_fact(prose)),
                "",
            )
            observed = _page_intro_from_fact(page, destination_fact)
            path_parts = [
                part.replace("-", " ").replace("_", " ")
                for part in urlsplit(page.url).path.split("/")
                if part
            ]
            subject = _clean(str(getattr(page, "purpose", "") or ""), 56)
            if (
                not subject
                or subject.casefold() == str(getattr(page, "title", "")).casefold()
                or re.fullmatch(
                    r"(?:svg|canvas|element)-?\s*workspace", subject, flags=re.IGNORECASE
                )
            ):
                subject = path_parts[-1] if path_parts else "workspace"
            opening_purpose = str(
                getattr(context.page_knowledge[0], "purpose", "") if context.page_knowledge else ""
            )
            if path_parts and subject.casefold() in {
                _clean(context.title).casefold(),
                _clean(opening_purpose).casefold(),
            }:
                subject = _clean(path_parts[-1].title(), 56)
            if not observed:
                # Some dashboards expose their purpose only inside a dense
                # accessibility inventory (for example, "Manage & track all
                # appointments"), which is correctly rejected as a DOM dump
                # by ``_readable_fact``. Recover the short action phrase from
                # that observed text and pair it with a route-derived subject;
                # otherwise use a neutral evidence-backed workspace sentence.
                raw_page_text = " ".join(
                    str(item) for item in getattr(page, "visible_facts", []) or []
                )
                structured_summary = (
                    _summary_from_schedule(raw_page_text, subject)
                    or _summary_from_repeated_items(raw_page_text, subject)
                    or _summary_from_collection(raw_page_text, subject)
                )
                if structured_summary:
                    observed = structured_summary

                if not observed:
                    local_labels = list(
                        dict.fromkeys(
                            _clean(str(value), 56)
                            for value in (
                                list(getattr(page, "visible_sections", []) or [])
                                or list(getattr(page, "visible_facts", []) or [])
                            )
                            if str(value).strip() and not _is_metadata_fact(str(value))
                        )
                    )[:3]
                    if local_labels:
                        joined = ", ".join(local_labels[:-1])
                        if len(local_labels) > 1:
                            joined = f"{joined}, and {local_labels[-1]}"
                        else:
                            joined = local_labels[0]
                        observed = _human_sentence(
                            f"The {subject} page brings {joined} into view, showing what this part of the product contains"
                        )

                action_match = re.search(
                    r"\b((?:manage|track|schedule|review|configure|organize|monitor|plan|list)"
                    r"\s+(?:&|and)?\s*[a-z][\w-]*(?:\s+[a-z][\w-]*){0,4})",
                    raw_page_text,
                    flags=re.IGNORECASE,
                )
                if observed:
                    pass
                elif action_match:
                    observed = _human_sentence(
                        f"The {subject} workspace lets teams {action_match.group(1).strip().lower()}"
                    )
                else:
                    observed = _human_sentence(
                        f"The {subject} workspace brings its observed controls and current information together for review"
                    )
            # A destination fact can be a short heading that passes the
            # readability filter but still yields route-label prose such as
            # ``the Lead Management view brings Lead Management into view``.
            # Prefer a grounded collection/action summary from the same page;
            # never let a navigation mechanic become the viewer's takeaway.
            if re.search(
                r"\bview brings .* into view, showing how this part of the product is organized\b",
                observed,
                flags=re.IGNORECASE,
            ):
                raw_page_text = " ".join(
                    str(item) for item in getattr(page, "visible_facts", []) or []
                )
                observed = (
                    _summary_from_collection(raw_page_text, subject)
                    or _summary_from_schedule(raw_page_text, subject)
                    or _summary_from_repeated_items(raw_page_text, subject)
                    or _human_sentence(
                        f"The {subject} workspace presents its visible controls and current information together, so the viewer can see how this area is used"
                    )
                )
            exploration_bridge = _exploration_context_bridge(context, page)
            if exploration_bridge is not None:
                bridge_copy, bridge_evidence = exploration_bridge
                # Keep the destination's own page purpose in the caption. The
                # relationship learned during discovery is a short bridge,
                # not a replacement for explaining what is visible here.
                destination_intro = _page_intro_from_fact(page, destination_fact)
                observed = " ".join(
                    item for item in (destination_intro, bridge_copy) if item
                ).strip()
                step.operation.evidence_refs.append(bridge_evidence)
        target_fact = _target_fact_narration(page, target)
        # A heading-plus-inventory extraction (for example ``Week 1
        # highlights ...``) is evidence for planning, not a viewer-ready
        # sentence. Do not let it override the page-local summary selected by
        # the deterministic narrator; this was the source of title-dump
        # captions in the study-plan walkthrough.
        if target_fact and not _viewer_ready(target_fact, target):
            target_fact = ""
        bridge = _configuration_bridge(context, page)
        if operation.kind is OperationKind.VERIFY_STATE:
            # A pre-action inspection is part of the story: it establishes
            # what the current screen can support. Avoid echoing a polluted
            # accessibility target such as ``Leads phone Enter Phone Number``
            # and describe the observed field in its page-local context.
            page_subject = _clean(
                str(getattr(page, "purpose", "") or getattr(page, "title", "") or "workspace"),
                64,
            )
            opening_purpose = str(
                getattr(context.page_knowledge[0], "purpose", "") if context.page_knowledge else ""
            )
            if page is not None:
                path_parts = [
                    part.replace("-", " ").replace("_", " ")
                    for part in urlsplit(str(getattr(page, "url", ""))).path.split("/")
                    if part
                ]
                if path_parts and page_subject.casefold() in {
                    _clean(context.title).casefold(),
                    _clean(opening_purpose).casefold(),
                }:
                    page_subject = _clean(path_parts[-1].title(), 64)
            field_subject = _clean(target, 64) or "available information"
            field_ref = _label_reference(field_subject)
            field_lower = field_subject.casefold()
            if re.search(r"\b(?:phone|mobile|telephone)\b", field_lower):
                observed = _human_sentence(
                    f"The {page_subject} view keeps {field_ref} available for contact verification while this record is reviewed"
                )
            elif re.search(r"\b(?:email|mail)\b", field_lower):
                observed = _human_sentence(
                    f"The {page_subject} view keeps {field_ref} available as a follow-up channel for this record"
                )
            elif re.search(r"\b(?:language|locale|region)\b", field_lower):
                observed = _human_sentence(
                    f"The {page_subject} view exposes {field_ref} so the visible content can be reviewed in the selected context"
                )
            elif re.search(r"\b(?:date|time|from|to)\b", field_lower):
                observed = _human_sentence(
                    f"The {page_subject} view exposes {field_ref} to define the range represented by the information on screen"
                )
            else:
                raw_page_text = " ".join(
                    str(value) for value in getattr(page, "visible_facts", []) or []
                )
                observed = (
                    _summary_from_collection(raw_page_text, page_subject)
                    or _summary_from_schedule(raw_page_text, page_subject)
                    or _summary_from_repeated_items(raw_page_text, page_subject)
                )
                if not observed and field_ref.casefold() in {
                    page_subject.casefold(),
                    _clean(context.title).casefold(),
                    _clean(opening_purpose).casefold(),
                }:
                    observed = _human_sentence(
                        f"The {page_subject} view is now established, giving the viewer a readable look at this part of the product before we continue"
                    )
                else:
                    observed = _human_sentence(
                        f"The {page_subject} view exposes {field_ref} as part of its current state, grounding the next step in what is visible"
                    )
        if (
            bridge
            and operation.kind is OperationKind.SCROLL_TO
            and re.search(r"\b(?:settings?|dashboard)\b", target, re.IGNORECASE)
        ):
            observed = bridge
        # Form controls often have no prose fact of their own: discovery
        # records only the label/placeholder and the value is intentionally
        # excluded from narration.  Give those scenes a useful, target-bound
        # explanation instead of a generic "the details are shown" line.
        if operation.kind is OperationKind.FILL_PHONE:
            observed = _human_sentence(
                "The phone field captures a contact number so this record can be verified in the workflow"
            )
        elif operation.kind is OperationKind.FILL_EMAIL:
            observed = _human_sentence(
                "The email field gives this record a reliable contact path for follow-up"
            )
        elif operation.kind is OperationKind.FILL_TEXT and not target_fact:
            field_label = re.sub(r"^(?:enter|fill)\s+", "", target, flags=re.IGNORECASE)
            field_label = re.sub(r"\s*\([^)]*\)", "", field_label).strip() or "text"
            field_reference = _label_reference(field_label)
            if field_reference.lower().startswith("the "):
                field_reference = field_reference[4:]
            lower_label = field_label.casefold()
            if re.search(r"\b(?:remark|note|comment|description)\b", lower_label):
                observed = _human_sentence(
                    f"The {field_reference} field preserves the context that helps a teammate understand this record"
                )
            elif re.search(r"\b(?:name|title|subject)\b", lower_label):
                observed = _human_sentence(
                    f"The {field_reference} field gives the record a recognizable identity for later follow-up"
                )
            else:
                observed = _human_sentence(
                    f"The {field_reference} field adds the visible context needed to distinguish this record in the workflow"
                )
        # A readable fact can be correct yet lose its subject when extracted
        # from a dense card (for example a metric or an internship's
        # contribution). Prefer the exact target-headed fact in that case so
        # the resulting line names what the viewer is looking at.
        if (
            target_fact
            and operation.kind is OperationKind.SCROLL_TO
            and target_words
            and not _summary_from_repeated_items(all_page_facts, target)
            and not any(word in observed.lower() for word in target_words)
        ):
            observed = target_fact
        if operation.kind is OperationKind.SCROLL_TO:
            narration = observed
            interaction, dwell = "scroll", 5.0
        elif operation.kind in {OperationKind.OPEN_NAVIGATION_ITEM, OperationKind.NAVIGATE}:
            narration = observed
            interaction, dwell = "navigate", 4.5
        elif operation.kind in {
            OperationKind.CLICK,
            OperationKind.OPEN_MODAL,
            OperationKind.CLOSE_MODAL,
        }:
            narration = observed
            interaction, dwell = "click", 4.0
        elif operation.kind in {
            OperationKind.FILL_TEXT,
            OperationKind.FILL_EMAIL,
            OperationKind.FILL_PHONE,
            OperationKind.SELECT_OPTION,
            OperationKind.SELECT_DATE,
            OperationKind.SELECT_DATE_RANGE,
        }:
            narration = observed
            interaction, dwell = "type", 4.25
        elif operation.kind is OperationKind.SUBMIT:
            narration = observed
            interaction, dwell = "submit", 4.75
        else:
            narration = observed
            interaction, dwell = "observe", 3.5
        if re.match(r"^(?:saved|auto[- ]calculated)\b", narration.strip(), flags=re.IGNORECASE):
            narration = _human_sentence(
                f"{target or getattr(page, 'purpose', '') or 'This view'} keeps this recorded state available so the overview can be reviewed"
            )
        # Keep heading-plus-description extractions from becoming crawler-like
        # captions. Lead with a viewer-oriented explanation whenever the
        # observed prose starts with the exact scene title.
        narration_normalized = " ".join(narration.split())
        if target_words and _needs_heading_rewrite(narration_normalized, target):
            remainder = narration_normalized[len(normalized_target) :].lstrip(" :—-.")
            if remainder:
                narration = _human_sentence(_heading_caption(target, remainder))
        fact_id, cited_prose = _scene_fact(context, operation, target)
        if (
            cited_prose
            and operation.kind is OperationKind.SCROLL_TO
            and not bridge
            and not _summary_from_repeated_items(all_page_facts, target)
            and _viewer_ready(_subject_fact(target, cited_prose), target)
            and len(cited_prose.split()) >= 6
            and not (not re.search(r"[.!?]", cited_prose) and bool(re.search(r"\d", cited_prose)))
        ):
            # Prefer the exact page-local fact selected for this landmark over
            # a generic connective sentence. This keeps project/role/feature
            # scenes specific while still rejecting dense inventory evidence.
            observed = _target_fact_narration(page, target) or _subject_fact(target, cited_prose)
            narration = observed
        # Evidence recovery above may reintroduce instructional copy from a
        # page's raw text. Keep the final line viewer-oriented even when the
        # source UI itself describes how its controls work.
        if re.search(
            r"\b(?:click|press|tap|select)\b.*\b(?:to|then|and)\b", observed, re.IGNORECASE
        ):
            observed = _human_sentence(
                f"The {target} area groups the product's available capabilities, giving the viewer a clear map of what follows"
            )
        # The planner has already selected the exact facts and intermediate
        # headings this continuous scroll covers.  Preserve that provenance in
        # the editorial contract; retaining only the final target caused a
        # grouped project/career scene to narrate one arbitrary card.
        scene_evidence = [
            f"operation:{operation.id}",
            *operation.evidence_refs,
            f"element:{target}",
        ]
        if page is not None:
            scene_evidence.append(f"page:{page.url}")
        if fact_id:
            scene_evidence.append(fact_id)
        # Keep the complete page-local provenance for grouped scroll beats.
        # ``_scene_fact`` intentionally returns no positional guess, so use
        # the same conservative subject matcher as the fallback narrator for
        # any additional card/role facts that this continuous movement covers.
        if operation.kind is OperationKind.SCROLL_TO and page is not None:
            grouped_subjects = [
                " ".join(str(item).split())
                for item in (getattr(operation, "covered_content_groups", None) or [])
                if str(item).strip()
            ]
            for subject in [target, *grouped_subjects]:
                local_fact_id, _local_fact = _matching_page_fact(page, subject)
                if local_fact_id:
                    scene_evidence.append(local_fact_id)
        provisional = EditorialScene(
            id=f"scene-{index}",
            operation_id=operation.id,
            title=_clean(target, 120),
            purpose=_clean(operation.intent),
            narration=narration,
            evidence=list(dict.fromkeys(scene_evidence)),
            interaction=interaction,
            required_dwell_seconds=dwell,
            completion_criteria=["target state is visible", "caption evidence is readable"],
            page_url=page.url if page is not None else operation.page_url,
            story_phase=(
                "context"
                if index == 1
                else "close"
                if index == total_steps
                else "enter"
                if interaction == "navigate"
                else "demonstrate"
                if interaction == "click"
                else "explain"
            ),
            action_classification="transitional" if interaction == "navigate" else "essential",
        )
        # The fallback writer can find a useful nearby sentence on a dense
        # page. Never retain it when it is not also in this scene's immutable
        # evidence: the result sounds polished but tells the viewer about the
        # next card while the current card is on screen.
        evidence_words = set(
            re.findall(r"[a-z0-9]{4,}", _scene_source(context, provisional).lower())
        )
        narration_words = set(re.findall(r"[a-z0-9]{4,}", narration.lower()))
        if (
            len(evidence_words) >= 3
            and len(narration_words & evidence_words) < 2
            and not (
                operation.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}
                and _viewer_ready(narration, target)
            )
            # A control/gesture scene is grounded by its semantic target even
            # when the page-level accessibility inventory is intentionally
            # noisy.  Replacing a target-bound transition with the first raw
            # inventory sentence was the source of captions such as
            # ``Here, ... Shapes Canvas actions ...`` on visual editors.
            and not (
                operation.kind
                in {OperationKind.CLICK, OperationKind.POINTER_SEQUENCE, OperationKind.DRAG}
                and target_words
                and bool(set(target_words) & evidence_words)
                and _viewer_ready(narration, target)
            )
        ):
            # A cited fact can be provenance-only inventory (for example a
            # flattened list of cards/controls).  Never feed that raw string
            # back into captions during evidence recovery; re-run the
            # deterministic observer, which selects a sentence-shaped local
            # fact or a constrained page summary instead.
            if cited_prose and not _looks_like_screen_transcript(cited_prose, cited_prose):
                narration = _viewer_fact(cited_prose)
            else:
                narration = _observed_narration(context, operation, target)
            if len(narration.split()) < 10:
                narration = _grounded_scene_fallback(target, _scene_source(context, provisional))
            normalized_fallback = " ".join(narration.split())
            if target_words and _needs_heading_rewrite(normalized_fallback, target):
                remainder = normalized_fallback[len(normalized_target) :].lstrip(" :—-.")
                if remainder:
                    narration = _human_sentence(_heading_caption(target, remainder))
            provisional = provisional.model_copy(update={"narration": narration})
        # Apply the same guard after evidence recovery, which may replace the
        # initial narration with a cited heading-plus-description fragment.
        normalized_final = " ".join(provisional.narration.split())
        if target_words and _needs_heading_rewrite(normalized_final, target):
            remainder = normalized_final[len(normalized_target) :].lstrip(" :—-.")
            if remainder:
                provisional = provisional.model_copy(
                    update={"narration": _human_sentence(_heading_caption(target, remainder))}
                )
        # Evidence recovery may select a nearby card while the scene is aimed
        # at a named landmark. Re-apply the exact target fact as the final
        # guard so captions retain the project/company/feature being shown.
        if (
            target_fact
            and operation.kind is OperationKind.SCROLL_TO
            and target_words
            and not _summary_from_repeated_items(all_page_facts, target)
            and not any(word in provisional.narration.lower() for word in target_words)
        ):
            provisional = provisional.model_copy(update={"narration": target_fact})
        # Keep the configuration-to-workflow bridge authoritative after all
        # dense-page evidence recovery guards have run.
        if (
            bridge
            and operation.kind is OperationKind.SCROLL_TO
            and re.search(r"\b(?:settings?|dashboard)\b", target, re.IGNORECASE)
        ):
            provisional = provisional.model_copy(update={"narration": bridge})
        if re.search(
            r"\b(?:click|press|tap|select)\b.*\b(?:to|then|and)\b",
            provisional.narration,
            re.IGNORECASE,
        ):
            provisional = provisional.model_copy(
                update={
                    "narration": _human_sentence(
                        f"The {target} area groups the product's available capabilities, giving the viewer a clear map of what follows"
                    )
                }
            )
        # Evidence recovery can replace a destination introduction with the
        # old route-label template late in this function.  Keep the final
        # guard next to the scene append so no later rewrite can reintroduce
        # crawler language such as ``view brings X into view``.  Rebuild the
        # line from the destination page's observed content instead.
        if re.search(
            r"\b(?:view|page)\s+brings\b.*\binto\s+view\b|\bshowing\s+how\s+this\s+part\s+of\s+the\s+product\s+is\s+organized\b",
            provisional.narration,
            re.IGNORECASE,
        ):
            destination_subject = (
                _clean(
                    str(getattr(page, "purpose", "") or getattr(page, "title", "") or target),
                    64,
                )
                or target
            )
            raw_destination_text = " ".join(
                str(item) for item in getattr(page, "visible_facts", []) or []
            )
            repaired_navigation = (
                _summary_from_collection(raw_destination_text, destination_subject)
                or _summary_from_schedule(raw_destination_text, destination_subject)
                or _summary_from_repeated_items(raw_destination_text, destination_subject)
            )
            provisional = provisional.model_copy(
                update={
                    "narration": repaired_navigation
                    or _human_sentence(
                        f"The {destination_subject} workspace presents its observed controls and current information together for review"
                    )
                }
            )
        # VerifyState beats are often emitted beside a page-wide inventory.
        # The inventory is useful evidence for planning but is not the thing
        # being verified.  Rebind the final caption to the observed target so
        # a field checkpoint cannot inherit a neighbouring page summary.
        if operation.kind is OperationKind.VERIFY_STATE and target:
            target_lower = target.casefold()
            if any(
                term in target_lower
                for term in ("phone", "mobile", "telephone", "email", "mail", "name")
            ):
                provisional = provisional.model_copy(
                    update={
                        "narration": _human_sentence(
                            f"This form provides the {target} field as a clear checkpoint before the record is completed"
                        )
                    }
                )
        if (
            operation.kind is OperationKind.OPEN_NAVIGATION_ITEM
            and target
            and re.search(r"\boption\s+includes\b|\bopen\s+the\b", observed, re.IGNORECASE)
        ):
            destination_subject = (
                _clean(
                    str(getattr(page, "purpose", "") or getattr(page, "title", "") or target),
                    64,
                )
                or target
            )
            destination_text = " ".join(
                str(value) for value in getattr(page, "visible_facts", []) or []
            )
            navigation_summary = (
                _summary_from_collection(destination_text, destination_subject)
                or _summary_from_schedule(destination_text, destination_subject)
                or _summary_from_repeated_items(destination_text, destination_subject)
            )
            provisional = provisional.model_copy(
                update={
                    "narration": navigation_summary
                    or _human_sentence(
                        f"The {target} workspace organizes its visible records and controls into the area we are about to explore"
                    )
                }
            )
        if operation.kind is OperationKind.SELECT_OPTION and target:
            provisional = provisional.model_copy(
                update={
                    "narration": _human_sentence(
                        f"The selected {target} choice shows the observed context this workflow carries forward"
                    )
                }
            )
        scenes.append(provisional)
    # A page may be re-entered after an earlier chapter when the validated
    # workflow proves a meaningful follow-up interaction (for example opening
    # a create form from a list). Reusing the original page summary makes the
    # story sound duplicated and trips the repetition gate. Rephrase the
    # re-entry from its own page evidence while retaining the same scene ID,
    # timing, and provenance.
    seen_narration: list[str] = []
    operation_by_id = {step.operation.id: step.operation for step in plan.workflow_steps}
    diversified: list[EditorialScene] = []
    for scene in scenes:
        normalized = " ".join(scene.narration.split()).casefold()
        same_page_title = [
            prior_scene
            for prior_scene in diversified
            if scene.operation_id is not None
            and prior_scene.operation_id is not None
            and (scene.page_url or "").rstrip("/").casefold()
            == (prior_scene.page_url or "").rstrip("/").casefold()
            and " ".join(scene.title.split()).casefold()
            == " ".join(prior_scene.title.split()).casefold()
        ]
        # The hard editorial gate is intentionally story-wide, not limited to
        # identical titles. A model can repeat the same card copy under two
        # different headings (common on documentation/testimonial pages), so
        # repair any earlier semantic beat on the same product page before QA.
        prior_repeated = next(
            (
                prior_scene
                for prior_scene in diversified
                if prior_scene.interaction != "opening"
                and _narration_repeats(scene.narration, prior_scene.narration)
            ),
            None,
        )
        duplicate = any(
            normalized == " ".join(prior_scene.narration.split()).casefold()
            or (
                len(set(re.findall(r"[a-z0-9]{4,}", normalized))) >= 4
                and len(
                    set(re.findall(r"[a-z0-9]{4,}", normalized))
                    & set(re.findall(r"[a-z0-9]{4,}", prior_scene.narration.casefold()))
                )
                >= 4
            )
            for prior_scene in same_page_title
        )
        if (duplicate or prior_repeated is not None) and scene.operation_id:
            operation = operation_by_id.get(scene.operation_id)
            title = _clean(scene.title, 72) or "this workspace"
            source = _scene_source(context, scene).casefold()
            if duplicate and scene.interaction == "navigate":
                scene = scene.model_copy(
                    update={
                        "narration": _human_sentence(
                            f"We return to the {title} workspace to continue the demonstrated flow, keeping its visible records and controls in context"
                        )
                    }
                )
            elif "filter" in source and ("status" in source or "assign" in source):
                scene = scene.model_copy(
                    update={
                        "narration": _human_sentence(
                            f"The visible {title} records can be narrowed by filters and status, giving this review a focused starting point"
                        )
                    }
                )
            elif operation is not None:
                # Re-entry/duplicate-title scenes still need a page-local
                # explanation.  Prefer the deterministic evidence summary for
                # this operation; the old connective sentence described the
                # tour mechanics and was rejected as generic narration.
                previous = (
                    prior_repeated.narration
                    if prior_repeated is not None
                    else (diversified[-1].narration if diversified else "")
                )
                local, fact_id = _distinct_evidence_narration(
                    context,
                    operation,
                    title,
                    previous,
                    interaction=scene.interaction,
                    story_phase=scene.story_phase,
                )
                if local and _viewer_ready(local, title):
                    evidence = list(scene.evidence)
                    if fact_id and fact_id not in evidence:
                        evidence.append(fact_id)
                    scene = scene.model_copy(update={"narration": local, "evidence": evidence})
                else:
                    local = _observed_narration(context, operation, title)
                    if (
                        local
                        and not _narration_repeats(local, previous)
                        and _viewer_ready(local, title)
                    ):
                        scene = scene.model_copy(update={"narration": local})
            normalized = " ".join(scene.narration.split()).casefold()
        seen_narration.append(normalized)
        diversified.append(scene)
    scenes = diversified
    # Run a final story-wide pass after heading/evidence guards. Those guards
    # can legitimately restore a target-specific fact, but that restored line
    # may duplicate a prior scene's editorial beat; repair it before duration
    # allocation and before any provider enrichment.
    scenes = _repair_fragmented_editorial_copy(context, scenes)
    scenes = _repair_repeated_narration(context, scenes)
    requested = min(max(plan.target_duration_seconds, 60), 180)
    # Capture time, rather than render-time slowdown, supplies the editorial
    # duration. Each chapter therefore gets enough reading time to be useful.
    # Allocate enough real reading time to every scene while keeping a typical
    # thorough walkthrough within its requested 2–3 minute editorial window.
    # Capture stays at native speed; this only governs how long the browser
    # holds after each meaningful reveal.
    # Reserve native-speed scroll and navigation time before allocating reading
    # holds. Otherwise a nominal three-minute plan can grow into a five-minute
    # capture simply because every scene also has physical motion.
    scroll_count = sum(scene.interaction == "scroll" for scene in scenes)
    navigation_count = sum(scene.interaction == "navigate" for scene in scenes)
    motion_budget = scroll_count * 2.5 + navigation_count * 2.5 + 5.0
    reading_budget = max(len(scenes) * 2.25, requested - motion_budget)
    # Small stories should not balloon solely because their requested target
    # is large; their minimum is still governed by the actual script.  Once a
    # story has enough scenes to carry a real walkthrough, however, capping
    # every chapter at 4.75 seconds makes the approved captions outrun the
    # browser footage. Allocate the remaining budget as native page reading
    # time, bounded to a deliberate per-scene maximum rather than a renderer
    # stretch or freeze.
    # Full walkthroughs commonly have many short, page-local beats; their
    # requested envelope already accounts for the breadth of coverage and a
    # modest chapter hold keeps the journey from ballooning. Focused stories
    # with several evidence beats need the larger allocation so narration can
    # actually be read over native footage.
    full_walkthrough = bool(
        re.search(r"\b(?:full|complete|entire|every|each)\b", plan.objective or "", re.IGNORECASE)
    )
    if len(scenes) <= 4 or full_walkthrough:
        chapter_dwell = min(4.75, max(2.25, reading_budget / len(scenes)))
    else:
        chapter_dwell = min(12.0, max(2.25, reading_budget / len(scenes)))
    scenes = [
        scene.model_copy(
            update={
                "required_dwell_seconds": chapter_dwell
                if scene.operation_id
                else max(scene.required_dwell_seconds, chapter_dwell)
            }
        )
        for scene in scenes
    ]
    # The planner's objective minimum is the delivery contract.  Do not
    # replace it with the arithmetic sum of scene holds: render transitions
    # legitimately share a frame boundary, so that sum would reject a valid
    # native-speed edit merely because each scene's dwell overlaps a cut by a
    # few frames.  Per-scene dwell remains enforced by editorial QA.
    return EditorialStoryboard(
        brief=brief,
        scenes=scenes,
        minimum_duration_seconds=max(45, plan.minimum_duration_seconds),
    )


_GENERIC_EDITORIAL_PATTERNS = (
    "walkthrough pauses",
    "before moving on",
    "comes into focus",
    "visible detail is clear",
    "gives the viewer concrete evidence",
    "sets up the details",
    "bespoke command interface",
    "is visibly established in",
    "section is now visible",
    "section visibly presents",
    "section brings the visible details together",
    "quick overview of key metrics",
    "giving you immediate insight",
    "various filters and details",
    "ready for management and action",
    "surrounding controls visible for context",
    "observed details are read before the walkthrough continues",
    "clear next step for continuing the conversation",
)


def _supported(text: str, source: str) -> bool:
    """Require a meaningful phrase overlap before accepting model prose."""
    words = set(re.findall(r"[a-z0-9]{4,}", text.lower()))
    evidence = set(re.findall(r"[a-z0-9]{4,}", source.lower()))
    lowered = text.lower()
    if any(pattern in lowered for pattern in _GENERIC_EDITORIAL_PATTERNS) or bool(
        re.search(r"\b(?:keeps|brings|puts)\b[^.]{0,80}\bin focus\b", lowered)
    ):
        return False
    # Accessibility snapshots frequently contain a numbered/card inventory.
    # A writer that simply copies it has produced a transcript, not an
    # explanation. Allow an occasional metric, but reject inventory-shaped
    # prose before it can replace the deterministic editorial fallback.
    if len(re.findall(r"\b\d+\b", text)) >= 3:
        return False
    if len(re.findall(r"\b[A-Z]{2,}\b", text)) >= 5:
        return False
    if not re.search(
        r"\b(?:is|are|was|were|has|have|lets|helps|shows|keeps|brings|groups|gathers|contains|connects|supports|organizes|tracks|lists|offers|provides|explains|uses|creates|draws|draw|moves|opens|captures|gives|makes|enables|demonstrates|appears|remains|becomes|causes|caused|prevents|reduces|handles|processes|integrates|improves|requires|can|will)\b",
        text,
        flags=re.IGNORECASE,
    ):
        return False
    # Two shared words permits a model to smuggle a fabricated claim into a
    # scene. Requiring three content words makes the sentence recognisably
    # anchored in the specific card/page evidence it is allowed to describe.
    return len(words) >= 6 and len(words & evidence) >= 3


async def enrich_editorial_brief(
    context: ProductContext, storyboard: EditorialStoryboard, provider: object
) -> EditorialStoryboard:
    """Optional first OpenRouter pass, rejected unless grounded in visible evidence."""
    structured = getattr(provider, "structured", None)
    if structured is None:
        return storyboard
    source = _editorial_evidence(context)
    prompt = (
        "Extract a concise product-demo editorial brief from this observed website evidence. "
        "Write opening_message as a natural presenter introduction: greet the viewer, identify the observed product or experience, "
        "and preview the value of the walkthrough in one or two sentences. Do not merely copy a heading or screen transcript. "
        "Return only the supplied schema. Every fact must cite an evidence string using page:<url>, element:<name>, or source:<url>. "
        "Do not add facts that cannot be supported by at least two meaningful words from the evidence. "
        f"Evidence:\n{source[:12000]}"
    )
    try:
        candidate = await structured(prompt, EditorialBrief)
    except (ProviderError, ValueError, TypeError, AssertionError, IndexError, KeyError):
        return storyboard
    allowed_evidence = {
        f"page:{context.url}",
        *[f"page:{page.url}" for page in context.page_knowledge],
        *[
            _fact_id(page.url, fact)
            for page in context.page_knowledge
            for fact in page.visible_facts
        ],
        *[f"element:{item.name}" for item in context.elements],
        *[f"source:{item.source_url or context.url}" for item in context.elements],
    }
    if any(
        not set(fact.evidence).issubset(allowed_evidence) or not _supported(fact.text, source)
        for fact in candidate.facts
    ):
        return storyboard
    if (
        len(candidate.title.split()) > 7
        or len(candidate.title) > 64
        or len(candidate.opening_message.split()) > 28
        or not _readable_fact(candidate.opening_message)
        or not _supported(candidate.opening_message, source)
        or not _supported(candidate.product_purpose, source)
    ):
        return storyboard
    scenes = list(storyboard.scenes)
    if scenes and scenes[0].operation_id is None:
        opening_page = next(
            (
                page
                for page in context.page_knowledge
                if _canonical_page_url(page.url) == _canonical_page_url(context.url)
            ),
            context.page_knowledge[0] if context.page_knowledge else None,
        )
        bridge = _exploration_context_bridge(context, opening_page)
        # The deterministic opening owns the greeting and the requested
        # journey subject.  A model brief may sharpen the artifact metadata,
        # but it must not replace a real welcome with a DOM-shaped phrase such
        # as "the opening view presents ...".
        narration = scenes[0].narration
        evidence = list(scenes[0].evidence)
        if bridge is not None:
            bridge_copy, bridge_evidence = bridge
            if bridge_evidence not in evidence:
                narration = f"{narration} {bridge_copy}"
                evidence.append(bridge_evidence)
        scenes[0] = scenes[0].model_copy(update={"narration": narration, "evidence": evidence})
    return storyboard.model_copy(update={"brief": candidate, "scenes": scenes})


async def enrich_editorial_storyboard(
    context: ProductContext, storyboard: EditorialStoryboard, provider: object
) -> EditorialStoryboard:
    """Optional second pass: turn the accepted brief into scene narration."""
    structured = getattr(provider, "structured", None)
    if structured is None:
        return storyboard.model_copy(
            update={
                "scenes": _repair_repeated_narration(
                    context,
                    _repair_fragmented_editorial_copy(context, list(storyboard.scenes)),
                )
            }
        )
    source = _editorial_evidence(context)
    scene_evidence = {
        scene.id: {
            "allowed_evidence": scene.evidence,
            "observed_text": _scene_source(context, scene),
            "scene_title": scene.title,
            "scene_purpose": scene.purpose,
            "story_phase": scene.story_phase,
            "approved_fallback": scene.narration,
        }
        for scene in storyboard.scenes
    }
    objective = str(
        getattr(getattr(context, "objective", None), "raw", "") or storyboard.brief.product_purpose
    )
    objective_spec = getattr(context, "objective", None)
    audience = str(getattr(objective_spec, "audience", "product prospect"))
    audience_profile = getattr(objective_spec, "audience_profile", None)
    video_type = str(getattr(objective_spec, "video_type", "feature_walkthrough"))
    purpose = str(getattr(objective_spec, "purpose", "") or "")
    tone = str(getattr(objective_spec, "tone", "conversational"))
    prompt = (
        "Write a polished product-demo narration for the supplied immutable scenes. Return only {lines:[{id,narration}]}. "
        "Return exactly one line for every non-opening scene id; do not return a brief, timing, evidence, operation, title, or any other field. "
        "Use only visible evidence. Write one or two complete, conversational sentences of 12 to 32 words for every line: synthesize what is visible, why it matters, and the viewer takeaway. "
        "Do not recite screen copy, start with 'The ... section', or use filler such as 'highlights', 'showcases', 'details', or 'is now visible'. "
        "Never output a route label, project name, click instruction, or generic phrase by itself. "
        "Good: 'This communication platform pairs real-time presence with low-latency messaging, showing how the experience stays connected as work moves forward.' "
        "Bad: 'Project name.' Bad: 'The Projects section is now visible.' Bad: 'Open Timeline.' Bad: 'The Timeline section highlights experience.' "
        "Every narration must share at least two meaningful words with ITS OWN scene evidence, never another page's evidence. "
        "Do not describe a fact from a different scene even if it appears elsewhere in the product. "
        f"Objective: {redact_prompt_text(objective)}\nVideo mode: {video_type}\nPurpose: {redact_prompt_text(purpose) or 'explain the observed product clearly'}\n"
        f"Tone: {tone}\nAudience: {audience}\n"
        f"Audience profile: {json.dumps(audience_profile.model_dump(mode='json') if audience_profile is not None else {}, ensure_ascii=False)}\n"
        f"Scenes: {redact_prompt_text(json.dumps({key: value for key, value in scene_evidence.items() if key != 'opening'}, ensure_ascii=False))}\n"
        f"Evidence: {redact_prompt_text(source[:12000])}"
    )
    try:
        candidate = await structured(prompt, EditorialNarrationDraft)
    except (ProviderError, ValueError, TypeError, AssertionError, IndexError, KeyError):
        return storyboard.model_copy(
            update={
                "scenes": _repair_repeated_narration(
                    context,
                    _repair_fragmented_editorial_copy(context, list(storyboard.scenes)),
                )
            }
        )
    original_by_id = {
        scene.id: scene for scene in storyboard.scenes if scene.operation_id is not None
    }
    # Retain compatibility with in-process test providers written against the
    # former whole-storyboard schema. Production providers receive the narrow
    # EditorialNarrationDraft contract above.
    proposed_lines = (
        [(line.id, line.narration) for line in candidate.lines]
        if isinstance(candidate, EditorialNarrationDraft)
        else [
            (scene.id, scene.narration)
            for scene in getattr(candidate, "scenes", [])
            if scene.operation_id is not None
        ]
    )
    proposed_by_id = dict(proposed_lines)
    opening_words = set(re.findall(r"[a-z0-9]{4,}", storyboard.scenes[0].narration.lower()))

    def repeats_opening(scene: EditorialScene) -> bool:
        if scene.operation_id is None or not opening_words:
            return False
        words = set(re.findall(r"[a-z0-9]{4,}", proposed_by_id.get(scene.id, "").lower()))
        inventory_title = "\n" in scene.title or bool(re.search(r"\b\d+\b", scene.title))
        return (
            inventory_title
            and bool(words)
            and len(words & opening_words) / max(len(words), 1) >= 0.72
        )

    # A structured model is free to reorder an array even when it preserves
    # scene ids. Validate each proposed line against its *own* immutable scene
    # id, rather than zipping array positions; position-based validation can
    # approve a grounded line and then attach it to another scene by id.
    if set(original_by_id) != set(proposed_by_id):
        return storyboard.model_copy(
            update={"scenes": _repair_repeated_narration(context, list(storyboard.scenes))}
        )

    # Scene provenance is independent.  A valid model sentence should improve
    # its own scene even when another line is too long or insufficiently
    # grounded; rejecting the entire editorial pass in that case previously
    # restored a crawler-like deterministic script for every page.
    def has_target_specific_fact(scene: EditorialScene) -> bool:
        if not scene.page_url:
            return False
        page = next(
            (
                item
                for item in context.page_knowledge
                if _canonical_page_url(item.url) == _canonical_page_url(scene.page_url)
            ),
            None,
        )
        return bool(_target_fact_narration(page, scene.title))

    def is_route_mechanics_copy(text: str) -> bool:
        """Reject provider prose that describes the tour instead of the UI.

        Navigation scenes are allowed to use page evidence, but a model can
        still return lines such as "the next view" or "this keeps the next
        view in focus".  Accepting those lines here only to fail at editorial
        preflight wastes a run; keep the deterministic, evidence-backed line
        instead.
        """
        lowered = text.casefold()
        return (
            "next view" in lowered
            or "next part of the walkthrough" in lowered
            or "is now visible" in lowered
            or bool(
                re.search(
                    r"\b(?:view|page)\s+brings\b.*\binto\s+view\b|\bshowing\s+how\s+this\s+part\s+of\s+the\s+product\s+is\s+organized\b",
                    lowered,
                )
            )
            or "observed details are read" in lowered
            or "before the walkthrough continues" in lowered
            or bool(re.search(r"\b(?:current|working)\s+view\b.*\b(?:before|then)\b", lowered))
            or bool(
                re.search(r"\b(?:click|press|tap|select|open)\b.{0,80}\b(?:to|then|and)\b", lowered)
            )
        )

    accepted_ids = {
        scene_id
        for scene_id, original_scene in original_by_id.items()
        if _supported(proposed_by_id[scene_id], _scene_source(context, original_scene))
        and _viewer_ready(proposed_by_id[scene_id], original_scene.title)
        and original_scene.interaction not in {"type", "submit", "click"}
        # A model may enrich a scroll only when discovery captured an actual
        # target-specific explanatory fact.  Dense control inventories share
        # enough words to pass generic overlap checks, yet produce the route
        # narration we explicitly reject.  The deterministic writer remains
        # the safer source for those states.
        and (original_scene.interaction != "scroll" or has_target_specific_fact(original_scene))
        and (
            original_scene.interaction in {"navigate", "opening"}
            or (
                _mentions_scene_element(original_scene, proposed_by_id[scene_id])
                and _mentions_scene_subject(original_scene, proposed_by_id[scene_id])
            )
        )
        and not repeats_opening(original_scene)
        and not is_route_mechanics_copy(proposed_by_id[scene_id])
        and not _looks_like_screen_transcript(
            proposed_by_id[scene_id], _scene_source(context, original_scene)
        )
    }
    if not accepted_ids:
        return storyboard.model_copy(
            update={
                "scenes": _repair_repeated_narration(
                    context,
                    _repair_fragmented_editorial_copy(context, list(storyboard.scenes)),
                )
            }
        )
    # The model is an editorial writer, not a workflow editor. Keep the
    # validated evidence, scene timing, action semantics, and completion
    # contract owned by ProductLens; accept only grounded narration prose.
    narration_by_id = proposed_by_id
    scenes = [
        scene.model_copy(update={"narration": narration_by_id[scene.id]})
        if scene.id in accepted_ids
        else scene
        for scene in storyboard.scenes
    ]
    scenes = _repair_fragmented_editorial_copy(context, scenes)
    scenes = _repair_repeated_narration(context, scenes)
    # Structured providers occasionally return a valid sentence beginning
    # with a lower-case product token (for example ``app is ...``).  That is
    # grammatically clipped in captions and fails the same reader-readiness
    # gate as a route label.  Normalize only the presentation casing; the
    # evidence, wording, and timing remain provider/model-owned.
    scenes = [
        scene.model_copy(
            update={
                "narration": (
                    scene.narration[:1].upper() + scene.narration[1:]
                    if scene.narration and scene.narration[0].islower()
                    else scene.narration
                )
            }
        )
        for scene in scenes
    ]
    return storyboard.model_copy(update={"scenes": scenes})


def _mentions_scene_element(scene: EditorialScene, text: str) -> bool:
    """Keep model prose tied to the scene's named subject, not just its page.

    Page evidence often contains many facts. Requiring at least one observed
    element label in a content scene prevents a contribution sentence from
    silently losing the company/project/feature it is meant to explain.
    """
    labels = [
        value.split(":", 1)[1]
        for value in scene.evidence
        if value.startswith("element:") and value.split(":", 1)[1].strip()
    ]
    if not labels:
        return True
    text_words = set(re.findall(r"[a-z0-9]{4,}", text.casefold()))
    ignored = {"toggle", "theme", "button", "control", "section", "page", "home"}
    for label in labels:
        label_words = {
            word for word in re.findall(r"[a-z0-9]{4,}", label.casefold()) if word not in ignored
        }
        if label_words and label_words & text_words:
            return True
    return False


def _mentions_scene_subject(scene: EditorialScene, text: str) -> bool:
    """Require editorial prose to retain the scene's own named subject."""
    title_words = {
        word
        for word in re.findall(r"[a-z0-9]{4,}", scene.title.casefold())
        if word not in {"this", "that", "view", "page", "section", "current", "visible"}
    }
    if not title_words:
        return True
    return bool(title_words & set(re.findall(r"[a-z0-9]{4,}", text.casefold())))


def _scene_source(context: ProductContext, scene: EditorialScene) -> str:
    """Return only the observed text explicitly assigned to a scene."""
    allowed = set(scene.evidence)
    chunks: list[str] = []
    # The planner may ground a semantic control from an accessibility or
    # Stagehand observation without adding it to the page's compact element
    # inventory.  Its explicit ``element:`` evidence is still authoritative;
    # retain the label so editorial validation can distinguish a target-bound
    # state change from a noisy page-wide inventory.
    chunks.extend(
        value.removeprefix("element:")
        for value in scene.evidence
        if value.startswith("element:") and value.removeprefix("element:").strip()
    )
    for page in context.page_knowledge:
        if f"page:{page.url}" not in allowed:
            continue
        chunks.extend(fact for fact in page.visible_facts if _fact_id(page.url, fact) in allowed)
        if not chunks:
            chunks.extend(page.visible_sections[:4])
    for element in context.elements:
        if f"element:{element.name}" in allowed:
            chunks.append(element.text or element.name)
    return " ".join(chunks)


def bind_storyboard_events(
    storyboard: EditorialStoryboard, trace_event_ids: set[str]
) -> EditorialStoryboard:
    """Discard only planned scenes that never produced visible execution evidence."""
    scenes = [
        scene
        for scene in storyboard.scenes
        if scene.operation_id is None or scene.operation_id in trace_event_ids
    ]
    return storyboard.model_copy(update={"scenes": scenes})

__all__ = [
    "_GENERIC_EDITORIAL_PATTERNS",
    "_mentions_scene_element",
    "_mentions_scene_subject",
    "_scene_source",
    "_supported",
    "bind_storyboard_events",
    "build_editorial_storyboard",
    "enrich_editorial_brief",
    "enrich_editorial_storyboard",
]
