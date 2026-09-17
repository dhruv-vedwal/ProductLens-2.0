"""Caption bounds, narrated scenes, and editorial script assembly."""

from __future__ import annotations

import re

from app.contracts.models import (
    EditorialScene,
    EditorialStoryboard,
)
from app.presentation.editorial.narrative import *
from app.presentation.editorial.storyboard import (  # noqa: F401
    bind_storyboard_events,
    build_editorial_storyboard,
    enrich_editorial_brief,
    enrich_editorial_storyboard,
)


def _scene_page_key(scene: EditorialScene) -> str:
    """Keep editorial compression page-aware even for older persisted boards."""
    if scene.page_url:
        return scene.page_url.rstrip("/") or "/"
    return next(
        (
            str(item).removeprefix("page:").rstrip("/")
            for item in scene.evidence
            if str(item).startswith("page:")
        ),
        "unscoped",
    )


def narrated_storyboard_scenes(storyboard: EditorialStoryboard) -> list[EditorialScene]:
    """Select reader-sized editorial beats while retaining the complete trace.

    The browser trace proves every safe action.  Treating every low-level
    action as narration forces captions to flash at crawler speed, especially
    on rich pages with several scroll landmarks.  Retain navigations and
    demonstrations, then allow two evidence-rich scroll explanations per
    page.  This preserves complete page coverage while leaving each approved
    line enough reading time in a caption-led delivery.
    """
    bound = [scene for scene in storyboard.scenes if scene.operation_id is not None]
    if len(bound) <= 12:
        return bound
    selected: list[EditorialScene] = [
        scene for scene in bound if scene.interaction in {"navigate", "click", "type", "submit"}
    ]
    scrolls_by_page: dict[str, list[EditorialScene]] = {}
    for scene in bound:
        if scene.interaction == "scroll":
            scrolls_by_page.setdefault(_scene_page_key(scene), []).append(scene)
    opening_page = _scene_page_key(bound[0]) if bound else ""
    for page_key, page_scrolls in scrolls_by_page.items():
        count = len(page_scrolls)
        # A complete walkthrough must not silently drop meaningful cards,
        # roles, articles, or design sections merely to save caption lines.
        # Small/medium page chapters can afford one reader-sized beat per
        # discovered landmark; only unusually dense pages use representative
        # sampling below. This keeps the story complete without hardcoding a
        # particular product's labels.
        if (
            (page_key == opening_page and count <= 16)
            or (page_key != opening_page and count <= 8)
            or count <= 3
        ):
            chosen_indexes = range(count)
        elif page_key == opening_page:
            # Opening pages often contain the product promise, a dense
            # featured-work/card collection, and a closing bridge. Keep the
            # central cluster rather than only the first two landmarks, which
            # previously skipped the very projects the viewer came to see.
            midpoint = count // 2
            chosen_indexes = (
                0,
                1,
                max(1, midpoint - 1),
                midpoint,
                min(count - 2, midpoint + 1),
                count - 1,
            )
        else:
            # The visible navigation itself establishes a secondary page.
            # Two local scenes then cover representative substance without
            # spending the entire narration budget on repeated cards/list
            # rows. This leaves time for the later pages in a full tour.
            chosen_indexes = (0, 1)
        selected.extend(page_scrolls[index] for index in dict.fromkeys(chosen_indexes))
    # Restore browser/story order after page-local selection above.
    selected_ids = {scene.id for scene in selected}
    selected = [scene for scene in bound if scene.id in selected_ids]
    # A short/atypical plan may contain only observe states. Keep enough
    # opening and closing evidence to make a real journey, never an empty
    # narration track.
    if not selected and bound:
        selected = [bound[0], *([bound[-1]] if len(bound) > 1 else [])]
    elif bound and selected[-1].id != bound[-1].id:
        selected.append(bound[-1])
    return list({scene.id: scene for scene in selected}.values())


def _collapse_repeated_sentences(text: str) -> str:
    """Remove accidental verbatim sentence repetition from an editorial pass."""
    sentences = re.findall(r"[^.!?]+[.!?]", text)
    if not sentences:
        return " ".join(text.split())
    unique: list[str] = []
    seen: set[str] = set()
    for sentence in sentences:
        normalized = " ".join(sentence.lower().split())
        if normalized not in seen:
            unique.append(sentence.strip())
            seen.add(normalized)
    return " ".join(unique)


def _caption_length_bound(text: str, *, maximum_words: int = 32) -> str:
    """Keep a caption reader-sized without inventing or truncating a claim.

    Editorial providers occasionally append a closing connective to an
    otherwise valid evidence sentence.  Hard word slicing leaves broken
    grammar and makes the scene fail readability QA. Prefer the longest
    complete sentence that fits the caption budget; if one sentence itself is
    too long, retain it unchanged so evidence is never silently rewritten.
    """
    words = text.split()
    if len(words) <= maximum_words:
        return text
    sentences = [part.strip() for part in re.findall(r"[^.!?]+[.!?]", text)]
    fitting = [sentence for sentence in sentences if len(sentence.split()) <= maximum_words]
    if fitting:
        # Prefer the most informative complete sentence.  Taking the first
        # fitting sentence turns a rhetorical opener such as ``Ready to
        # plan?`` into a title-only caption while discarding the grounded
        # explanation that follows it.
        return max(fitting, key=lambda sentence: len(sentence.split()))
    return text


_EDITORIAL_STOPWORDS = {
    "about",
    "after",
    "again",
    "around",
    "before",
    "being",
    "between",
    "clearer",
    "current",
    "details",
    "every",
    "from",
    "into",
    "part",
    "section",
    "that",
    "their",
    "this",
    "through",
    "view",
    "visible",
    "viewer",
    "what",
    "when",
    "where",
    "which",
    "with",
    "your",
}


def _distinct_editorial_narration(
    scene: EditorialScene, text: str, prior_texts: list[str], evidence: str = ""
) -> str:
    """Prevent repeated evidence beats from becoming a narrated slideshow.

    Discovery can legitimately expose the same entity at two levels (for
    example a Week card and its detail page).  Repeating the same sentence is
    not useful, but inventing a new fact is worse.  Rephrase the later beat
    using only semantic labels already attached to that scene's evidence.
    """
    # Compare the same semantic payload used by editorial QA.  Comparing raw
    # words here made every navigation chapter look duplicated because
    # connective copy ("checkpoint", "shown", "keeping", "details") was
    # treated as product evidence.  That caused the repair itself to replace
    # good page-specific prose with a worse generic checkpoint sentence.
    words = _diversity_tokens(text)
    if not words:
        return text
    for previous in prior_texts:
        previous_words = _diversity_tokens(previous)
        overlap = len(words & previous_words) / max(1, min(len(words), len(previous_words)))
        if overlap < 0.82:
            continue
        # Start with the scene's own semantic target.  Choosing the first
        # section labels from a page-wide evidence list made a later scene
        # talk about an unrelated heading (for example Professional History
        # while Backend Services was on screen).
        subject = _concise_scene_subject(scene.title)
        labels: list[str] = [subject]
        for evidence_ref in scene.evidence:
            value = str(evidence_ref)
            if value.startswith(("section:", "element:")):
                label = value.split(":", 1)[1].replace("\\n", " ")
                if label.casefold() != scene.title.casefold():
                    labels.append(label)
        labels.extend(re.findall(r"[A-Za-z][A-Za-z0-9/-]{3,}", scene.purpose))
        anchors: list[str] = []
        for label in labels:
            normalized = label.casefold()
            if normalized in _EDITORIAL_STOPWORDS or normalized in anchors:
                continue
            anchors.append(label)
            if len(anchors) == 2:
                break
        if not anchors:
            anchors = re.findall(r"[A-Za-z][A-Za-z0-9/-]{3,}", scene.title)[:2]
        if anchors:
            # Include concrete words from this scene's own evidence so the
            # repair cannot drift into a page-wide generic transition.
            evidence_labels = [
                token
                for token in re.findall(r"[A-Za-z][A-Za-z0-9/-]{3,}", evidence)
                if token.casefold() not in {"fact", "section", "element"}
                and not re.fullmatch(r"[a-f0-9]{10,}", token, flags=re.IGNORECASE)
            ]
            if evidence_labels:
                offset = (
                    int(re.search(r"\d+", scene.id).group()) % len(evidence_labels)
                    if re.search(r"\d+", scene.id)
                    else 0
                )
                anchors.append(evidence_labels[offset])
            unique_anchors = list(dict.fromkeys(anchors))[:3]
            primary = unique_anchors[0]
            secondary = unique_anchors[1] if len(unique_anchors) > 1 else ""
            # Keep the repeated beat grounded in its own visible labels while
            # avoiding the crawler-like ``current view connects`` boilerplate.
            # Repeated semantic beats still need a useful reason for the
            # second hold. Avoid crawler boilerplate such as ``distinct
            # visible beat``; describe the relationship between this target
            # and its containing page using only scene-local labels.
            if secondary:
                return _human_sentence(
                    f"The {primary} checkpoint is shown within {secondary}, keeping its visible details together for review"
                )
            return _human_sentence(
                f"The {primary} checkpoint opens its own visible details for review"
            )
        return "This scene adds a distinct, visible beat to the walkthrough for comparison."
    return text


def editorial_script(
    storyboard: EditorialStoryboard,
    event_by_operation: dict[str, str],
    *,
    opening_event_id: str | None = None,
) -> list[dict[str, object]]:
    """Map proven operations to a presenter-led, evidence-backed narration.

    The opening storyboard scene has no browser operation, but a caption-only
    demo still needs its welcome while the opening page is on screen.  Attach
    that welcome to the first proved opening operation instead of discarding
    it during the scene-to-event conversion.
    """
    lines: list[dict[str, object]] = []
    opening = next((scene for scene in storyboard.scenes if scene.operation_id is None), None)
    first_bound = True
    narrated_scenes = narrated_storyboard_scenes(storyboard)
    prior_texts: list[str] = []
    bound_event_ids: set[str] = set()
    for scene_index, scene in enumerate(narrated_scenes):
        event_id = event_by_operation.get(scene.operation_id)
        # A plan repair can leave multiple semantic scenes referring to one
        # physical browser action. The renderer cannot truthfully present two
        # captions as two distinct moments in that one state. Retain the
        # earliest (the presenter opening when applicable) and let the next
        # independently proved event carry the next explanation.
        if event_id and event_id not in bound_event_ids:
            text = scene.narration
            facts = list(scene.evidence)
            scene_id = scene.id
            if first_bound and opening is not None:
                # The opening welcome is connective, not a replacement for
                # the first proved scene. Preserve that scene's own grounded
                # explanation so the first caption establishes the product
                # *and* tells the viewer why the visible opening state matters.
                # Otherwise evidence QA correctly sees a welcome attached to
                # (for example) a Users navigation event with no Users facts.
                # The opening scene is the single source of truth for the
                # presenter introduction. Do not reconstruct a fixed greeting
                # here: doing so discards model-approved, evidence-grounded
                # prose and makes every product sound identical.
                intro = opening.narration.strip()
                # Let the welcome stand on the fully established opening
                # frame.  Appending the first target here made introductions
                # sound like route instructions ("the next view is ...") and
                # encouraged the viewer to leave Home before it was properly
                # explained.  The next scene retains its own evidence-bound
                # thought and begins only after the opening dwell.
                # There is one approved caption per proved browser event.
                # Keeping a second line on the same event breaks the
                # script/trace identity contract and makes timing repair
                # ambiguous.  Use the compact presenter opening for this
                # first visible product state; the following operation owns
                # the first page-local explanation.
                # Keep the opening caption sentence-complete. A word-count
                # slice can leave polished model prose ending in fragments
                # such as ``now we.``, which sounds broken even though all
                # evidence is valid. Prefer complete sentences within the
                # caption budget, then use the existing punctuation-aware
                # cleaner for an unusually long single sentence.
                intro_was_complete = bool(intro and intro.rstrip().endswith((".", "!", "?")))
                intro = _sentence_complete(intro, 240)
                # Some model/provider paths split a dotted product suffix
                # (for example ``draw.io``) away from the presenter sentence,
                # leaving an opening such as ``io. Today ...``. Repair that
                # punctuation artifact from the immutable brief identity and
                # keep the required human greeting; never pass the malformed
                # fragment into captions or future TTS.
                if re.match(r"^[a-z0-9-]+\.\s+Today\b", intro):
                    product = _clean(storyboard.brief.product_purpose, 96) or "this product"
                    today = intro.split("Today", 1)[1].lstrip()
                    intro = f"Welcome to {product}. Today {today}"
                if intro and intro[-1] not in ".!?":
                    intro += "."
                if opening_event_id and opening_event_id != event_id:
                    # A low-level readiness witness may precede the first
                    # narrated scene. Give that witness the presenter welcome
                    # and retain the current scene's own explanatory line;
                    # each line remains bound to a different proven event.
                    lines.append(
                        {
                            "event_id": opening_event_id,
                            "text": intro,
                            "facts": list(opening.evidence),
                            "scene_id": opening.id,
                            "opening": True,
                        }
                    )
                    bound_event_ids.add(opening_event_id)
                else:
                    # A short/atypical product can have only one proved
                    # opening event (for example a canvas that exposes its
                    # whole usable surface immediately).  Do not discard the
                    # page-local explanation in that case: the one caption
                    # attached to the event carries both the presenter
                    # welcome and the observed opening takeaway.  Keeping
                    # them on the same event preserves the one-event/one-line
                    # timing contract while preventing QA from evaluating a
                    # scene with only the welcome text.
                    scene_copy = _sentence_complete(text, 240)
                    # If the approved opening itself had to be repaired from
                    # an unfinished sentence, retain the historical opening
                    # contract; appending another beat would preserve a
                    # truncated model fragment in the presenter line. A
                    # complete greeting, however, can safely carry the one
                    # page-local takeaway when this is the only event.
                    text = (
                        " ".join(part for part in (intro, scene_copy) if part).strip()
                        if intro_was_complete
                        else intro
                    )
                    facts = list(opening.evidence)
                    scene_id = opening.id
                first_bound = False
            elif scene_index == len(narrated_scenes) - 1 or scene.story_phase == "close":
                # A full walkthrough should resolve as a guided journey, not
                # abruptly end on the final field/button label. The first
                # sentence remains evidence-grounded; the second is editorial
                # connective language that tells the viewer why this visible
                # final state matters.
                text = f"{text.rstrip('.')}. This leaves the visible state established at the end of the walkthrough."
            # Accessibility fallbacks such as ``svg workspace`` describe an
            # implementation surface, not a viewer-facing product area. If a
            # persisted storyboard contains that placeholder, derive a stable
            # subject from the scene's observed route and keep the copy
            # deliberately evidence-safe instead of allowing a route-label
            # caption through the editorial gate.
            if re.search(r"\b(?:svg|canvas|element)-?\s*workspace\b", text, flags=re.IGNORECASE):
                route = next(
                    (
                        value.split("://", 1)[-1].split("/", 1)[-1].split("?", 1)[0]
                        for value in scene.evidence
                        if value.startswith("page:") and "/" in value.split("://", 1)[-1]
                    ),
                    "workspace",
                )
                subject = (
                    _clean(route.replace("-", " ").replace("_", " ").title(), 56) or "workspace"
                )
                text = _human_sentence(
                    f"The {subject} view keeps the visible workspace in context for review"
                )
            # Keep the approved scene wording intact.  Rewriting a grounded
            # sentence merely because its neighbouring control uses a similar
            # grammatical pattern tends to erase the target-specific evidence
            # and produce unsupported filler.  Duplicate detection remains a
            # deterministic editorial repair: when two page-local scenes are
            # backed by the same dense fact, retain the current scene's own
            # labels while avoiding a narrated slideshow.
            # Product identities can contain dotted tokens (``draw.io``,
            # package names, domains). The generic sentence de-duplicator
            # treats the first dot as punctuation and inserts a space, so
            # never run it over the presenter opening; opening copy is already
            # bounded and assembled from approved sentences.
            if scene_id != opening.id:
                text = _collapse_repeated_sentences(text)
            text = _distinct_editorial_narration(
                scene,
                text,
                prior_texts,
                evidence=" ".join([*facts, *scene.evidence]),
            )
            # Duplicate-beat repair can itself select a placeholder evidence
            # label as an anchor. Apply the same implementation-label guard
            # after that repair so ``svg workspace`` can never be the final
            # viewer-facing caption.
            if re.search(r"\b(?:svg|canvas|element)-?\s*workspace\b", text, flags=re.IGNORECASE):
                route = next(
                    (
                        value.split("://", 1)[-1].split("/", 1)[-1].split("?", 1)[0]
                        for value in scene.evidence
                        if value.startswith("page:") and "/" in value.split("://", 1)[-1]
                    ),
                    "workspace",
                )
                subject = (
                    _clean(route.replace("-", " ").replace("_", " ").title(), 56) or "workspace"
                )
                text = _human_sentence(
                    f"The {subject} view keeps the visible workspace in context for review"
                )
            # Model prose can occasionally echo a dense accessibility dump
            # (zero-width characters, template braces, or phrases such as
            # ``where the visible create is``). It is not publishable copy and
            # is not a reason to discard an otherwise valid trace. Replace it
            # with a short scene-local summary grounded in the observed title
            # and section labels.
            if re.search(
                r"\u200b|\{\{|\}\}|where\s+the\s+visible\s+create\s+is|\bSort\s+By\b",
                text,
                flags=re.IGNORECASE,
            ):
                labels = [
                    _clean(value.split(":", 1)[1], 56)
                    for value in scene.evidence
                    if value.startswith("section:")
                    and not re.fullmatch(
                        r"(?:svg|canvas|element)-?\s*workspace",
                        value.split(":", 1)[1].strip(),
                        flags=re.IGNORECASE,
                    )
                ]
                subject = labels[0] if labels else _concise_scene_subject(scene.title)
                text = _human_sentence(
                    f"The {subject} view shows the visible records and controls that can be reviewed here"
                )
            # The presenter opening intentionally contains a greeting, the
            # walkthrough subject, and one purpose sentence. It may be longer
            # than a normal scene line; clipping it to the 32-word scene cap
            # drops the actual introduction and leaves only the product name.
            text = _caption_length_bound(
                text,
                maximum_words=48 if scene_id == opening.id else 32,
            )
            prior_texts.append(text)
            lines.append(
                {
                    "event_id": event_id,
                    "text": text,
                    "facts": facts,
                    "scene_id": scene_id,
                    "opening": first_bound is False and scene_id == opening.id,
                }
            )
            bound_event_ids.add(event_id)
    return lines

__all__ = [
    "_EDITORIAL_STOPWORDS",
    "_caption_length_bound",
    "_collapse_repeated_sentences",
    "_distinct_editorial_narration",
    "_scene_page_key",
    "editorial_script",
    "narrated_storyboard_scenes",
]
