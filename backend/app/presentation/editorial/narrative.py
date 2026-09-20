"""Narrative helpers for evidence-grounded editorial copy."""

from __future__ import annotations

import json
import re
from hashlib import sha256
from urllib.parse import unquote, urlsplit, urlunsplit

from app.contracts.models import (
    EditorialFact,
    EditorialScene,
    OperationKind,
    ProductContext,
)


def _canonical_page_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            unquote(parsed.path).rstrip("/") or "/",
            parsed.query,
            "",
        )
    )


def _clean(value: str, limit: int = 280) -> str:
    value = " ".join(value.split()).strip(" ,;:-")
    if len(value) <= limit:
        return value
    # Never turn an otherwise good piece of evidence into a clipped caption.
    # Keep a completed sentence or word boundary and let later selection choose
    # a more suitable fact when the resulting fragment is too short.
    clipped = value[:limit]
    boundary = max(clipped.rfind(". "), clipped.rfind("! "), clipped.rfind("? "))
    if boundary >= max(24, limit // 3):
        return clipped[: boundary + 1].strip()
    return clipped.rsplit(" ", 1)[0].strip(" ,;:-")


def _sentence_complete(value: str, limit: int = 240) -> str:
    """Keep only complete sentences before applying a caption length limit."""
    normalized = " ".join(value.split()).strip()
    if not normalized:
        return ""
    # Protect periods inside dotted identities/domains (``draw.io``,
    # ``api.example``) while finding sentence boundaries. Without this, the
    # matcher can restart immediately after the internal dot and return only
    # the suffix (``io workspace ...``), corrupting otherwise grounded copy.
    protected = re.sub(r"(?<=\w)\.(?=\w)", "\u0000", normalized)
    complete = re.findall(r"[^.!?]*[.!?](?=\s|$)", protected)
    if complete:
        prefix = " ".join(item.strip() for item in complete).replace("\u0000", ".").strip()
        if prefix:
            return _clean(prefix, limit)
    return _clean(normalized, limit)


_SENSITIVE_EDITORIAL_PATTERN = re.compile(
    r"(?:\b\d[\d .()\-]{7,}\d\b|\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b)", re.IGNORECASE
)


def _safe_editorial_text(value: str) -> str:
    """Exclude person/contact-row evidence from prose and model prompts."""
    if _SENSITIVE_EDITORIAL_PATTERN.search(value or ""):
        return ""
    normalized = " ".join((value or "").split()).casefold()
    # Starter editors and CMS templates sometimes expose lorem-ipsum filler as
    # if it were product copy. Keep legitimate documentation *about* filler,
    # but never let a paragraph made almost entirely of the canonical
    # placeholder vocabulary become the opening message or a caption.
    if "lorem ipsum" in normalized:
        words = set(re.findall(r"[a-z]+", normalized))
        placeholder_words = {
            "lorem",
            "ipsum",
            "dolor",
            "sit",
            "amet",
            "consectetur",
            "adipisicing",
            "elit",
            "sed",
            "do",
            "eiusmod",
            "tempor",
            "incididunt",
            "ut",
            "labore",
            "et",
            "dolore",
            "magna",
            "aliqua",
        }
        if len(words - placeholder_words) < 3:
            return ""
    # Diagram/editor starter palettes also expose synthetic list examples
    # ("List Item 1 Item 2 Item 3"). They are useful DOM evidence for the
    # editor, but not a product claim or a viewer-facing introduction.
    if re.fullmatch(r"(?:list\s+)?item(?:\s+\d+|\s+item\s+\d+){2,}[.!]?", normalized):
        return ""
    return value


def _meaningful_section_label(value: str) -> str:
    """Exclude bare accessibility placeholders from an opening summary."""
    normalized = " ".join((value or "").split()).casefold()
    if normalized in {
        "heading",
        "title",
        "description",
        "label",
        "text",
        "content",
        "file",
        "edit",
        "view",
        "help",
    } or re.fullmatch(r"(?:svg|canvas|element)-?\s*workspace", normalized):
        return ""
    return _clean(value, 72)


def _safe_page_subject(page: object | None, fallback: str = "workspace") -> str:
    """Choose a viewer-facing page subject without template starter labels."""
    if page is None:
        return fallback
    purpose = " ".join(str(getattr(page, "purpose", "") or "").split())
    title = " ".join(str(getattr(page, "title", "") or "").split())
    evidence = " ".join(str(value) for value in getattr(page, "visible_facts", []) or []).casefold()
    if "lorem ipsum" in evidence and (
        purpose.casefold() in {"heading", "title", "description"}
        or "lorem ipsum" in purpose.casefold()
    ):
        return title or fallback
    return purpose or title or fallback


def _concise_title(value: str) -> str:
    """Create a compact visible title from observed product identity."""
    value = _clean(value.replace("|", " ").replace("—", " ").replace("–", " "), 72)
    words = value.split()
    if not words:
        return "Product walkthrough"
    return " ".join(words[:6])


def _element_text(context: ProductContext, name: str, source_url: str | None = None) -> str:
    matches = [
        item
        for item in context.elements
        if item.name.strip().lower() == name.strip().lower()
        and (source_url is None or (item.source_url or "").rstrip("/") == source_url.rstrip("/"))
    ]
    # Prefer page-local evidence.  Discovery legitimately finds the same
    # semantic label on multiple routes; taking the first global match can
    # attach a Leads accessibility dump to a Booking caption.
    item = next(iter(matches), None)
    if item is None and source_url is not None:
        item = next(
            (
                item
                for item in context.elements
                if item.name.strip().lower() == name.strip().lower()
            ),
            None,
        )
    return _safe_editorial_text(_clean((item.text if item else "") or ""))


def _without_repeated_subject(value: str, subject: str) -> str:
    """Remove the heading copied into a structured fact's prose body."""
    body = value.split("::", 1)[-1]
    pattern = rf"^\s*{re.escape(subject)}\s*[:\-–—]?\s*"
    return re.sub(pattern, "", body, flags=re.IGNORECASE).strip()


def _page_for_operation(context: ProductContext, operation) -> object | None:
    """Resolve the observed page that owns an operation without guessing routes."""
    # A navigation control belongs to its source page, but the narration scene
    # establishes the destination. Resolve the verified URL postcondition
    # first; otherwise every tab scene repeats opening-page facts.
    expected = next(
        (str(item.expected) for item in operation.postconditions if item.kind == "url"), None
    )
    if expected:
        destination = next(
            (
                page
                for page in context.page_knowledge
                if _canonical_page_url(page.url) == _canonical_page_url(expected)
            ),
            None,
        )
        if destination is not None:
            return destination
    target_source = getattr(getattr(operation, "target", None), "source_url", None)
    if target_source:
        owned_page = next(
            (
                page
                for page in context.page_knowledge
                if _canonical_page_url(page.url) == _canonical_page_url(target_source)
            ),
            None,
        )
        if owned_page is not None:
            return owned_page
    source = next(
        (
            item.source_url
            for item in context.elements
            if operation.target
            and item.name.strip().lower() == operation.target.name.strip().lower()
            and item.source_url
        ),
        None,
    )
    if source:
        return next(
            (
                page
                for page in context.page_knowledge
                if _canonical_page_url(page.url) == _canonical_page_url(source)
            ),
            None,
        )
    return context.page_knowledge[0] if context.page_knowledge else None


def _fact_id(page_url: str, fact: str) -> str:
    digest = sha256(f"{page_url}\n{fact}".encode()).hexdigest()[:16]
    return f"fact:{digest}"


def _page_fact_records(page: object | None) -> list[tuple[str, str]]:
    if page is None:
        return []
    return [
        (_fact_id(page.url, fact), prose)
        for fact in getattr(page, "visible_facts", [])
        if (prose := _readable_fact(fact))
    ]


def _is_metadata_fact(value: str) -> bool:
    """Identify date/number chips that cannot establish a page purpose."""
    if re.fullmatch(r"(?:svg|canvas|element)-?\s*workspace", value.strip(), flags=re.IGNORECASE):
        return True
    return bool(
        re.search(r"\b(?:\d{1,2}\s+[A-Za-z]{3,9}|\d{1,2}:\d{2}|\d{4})\b", value)
        and not re.search(
            r"\b(?:manage|track|review|schedule|configure|organize|monitor|plan|create|compare|follow|coordinate|support|filter|list)\b",
            value,
            flags=re.IGNORECASE,
        )
    )


def _scene_fact(context: ProductContext, operation, target: str) -> tuple[str | None, str]:
    """Pick a readable, page-local fact without borrowing another page's copy."""
    page = _page_for_operation(context, operation)
    records = _page_fact_records(page)
    # A tab label is source-page UI, not the subject of the destination story.
    # Start every chapter with the destination overview; later local scroll
    # scenes select the specific card, project, role, or note being revealed.
    if operation.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}:
        if records:
            return records[0]
        raw = next(iter(getattr(page, "visible_facts", []) or []), None)
        return (_fact_id(page.url, raw), "") if raw else (None, "")
    target_words = set(re.findall(r"[a-z0-9]{4,}", target.lower()))
    explicit_target_fact = next(
        (
            raw_fact
            for raw_fact in getattr(page, "visible_facts", [])
            if page is not None
            if raw_fact.split("::", 1)[0].strip().lower().startswith(target.lower())
        ),
        None,
    )
    # When the exact target is a label inventory, it is better to let the
    # narrator produce a constrained category summary than silently cite a
    # different page card that happens to mention the same words.
    if explicit_target_fact is not None and not _readable_fact(explicit_target_fact):
        # A dense label inventory is not suitable as prose, but it is still
        # valid evidence for a constrained summary (for example, a numbered
        # schedule or a pattern filter). Retain its provenance while keeping
        # the narration text empty so the caller must summarise it.
        return (_fact_id(page.url, explicit_target_fact), "")
    # Match against the raw heading-linked evidence, not only its extracted
    # prose. The readable sentence often intentionally removes the heading;
    # comparing it alone makes a card scene fall back to the page's first fact.
    matching: list[tuple[tuple[int, int, int], tuple[str, str]]] = []
    for raw_fact in getattr(page, "visible_facts", []) if page is not None else []:
        prose = _readable_fact(raw_fact)
        if not prose:
            continue
        raw_words = set(re.findall(r"[a-z0-9]{4,}", raw_fact.lower()))
        overlap = len(target_words & raw_words)
        if not target_words or not overlap:
            continue
        raw_normalized = raw_fact.split("::", 1)[0].strip().lower()
        starts_with_target = raw_normalized.startswith(target.lower())
        # Prefer a fact explicitly introduced by this target, then the closest
        # lexical match; length is only a final tie-breaker.
        matching.append(
            (
                (0 if starts_with_target else 1, -overlap, -len(prose)),
                (_fact_id(page.url, raw_fact), prose),
            )
        )
    if matching:
        return min(matching, key=lambda item: item[0])[1]
    # A local card/section with no readable matching prose must not borrow the
    # page's first fact. The caller can derive a constrained category summary
    # from the target's own observed labels instead.
    return (None, "")


def _matching_page_fact(page: object | None, subject: str) -> tuple[str | None, str]:
    """Find the strongest readable fact for a page-local scene subject.

    Discovery facts are not guaranteed to use the exact accessible heading as
    their prefix (content blocks often contain a heading plus prose).  The
    previous planner therefore emitted section-only evidence for valid cards,
    leaving the editorial writer with nothing concrete to say.  This helper
    performs a conservative lexical match and never returns shell inventories
    or a fact from another page.
    """
    if page is None:
        return None, ""
    subject_words = set(re.findall(r"[a-z0-9]{4,}", str(subject).casefold()))
    if not subject_words:
        return None, ""
    matches: list[tuple[tuple[int, int, int], str, str]] = []
    for index, raw in enumerate(getattr(page, "visible_facts", []) or []):
        prose = _readable_fact(raw)
        if not prose:
            continue
        # A page often stores a bare accessibility heading alongside the
        # heading-plus-description fact.  Treating that duplicate as prose
        # makes the category branch win and emits a title-only caption.  Keep
        # only evidence that contains information beyond the scene subject.
        if " ".join(prose.split()).casefold() == " ".join(str(subject).split()).casefold():
            continue
        raw_words = set(re.findall(r"[a-z0-9]{4,}", str(raw).casefold()))
        overlap = len(subject_words & raw_words)
        if not overlap:
            continue
        heading = str(raw).split("::", 1)[0].strip().casefold()
        exact_heading = heading == " ".join(str(subject).split()).casefold()
        starts_heading = heading.startswith(" ".join(str(subject).split()).casefold())
        matches.append(
            (
                (0 if exact_heading else 1 if starts_heading else 2, -overlap, index),
                _fact_id(page.url, raw),
                prose,
            )
        )
    if not matches:
        return None, ""
    _, fact_id, prose = min(matches, key=lambda item: item[0])
    return fact_id, prose


def _sentence(value: str) -> str:
    # A caption-led scene needs one readable thought, not a DOM dump. The
    # structured editorial pass may improve this prose; this fallback remains
    # concise enough to be a valid on-screen sentence on its own.
    # Keep a complete evidence sentence whenever it fits the scene contract.
    # The former 200-character cap clipped otherwise grounded explanations
    # mid-sentence (for example, ending at ``during high``), which sounded
    # broken in captions and narration. EditorialScene allows substantially
    # more, while the viewer-ready gate still limits word count.
    value = _clean(value, 260)
    if not value:
        return ""
    # Accessibility extraction can duplicate punctuation when a heading and
    # its prose are joined. Normalize that presentation artifact without
    # changing any observed words or claims.
    value = re.sub(r"\s+([,.;:])", r"\1", value)
    value = re.sub(r"([,;:])\s*\1+", r"\1", value)
    value = value.rstrip(".")
    if not value:
        return ""
    return value if value.endswith(("!", "?")) else f"{value}."


def _human_sentence(value: str) -> str:
    """Return safe, complete viewer-facing prose from a visible fact."""
    text = _sentence(_safe_editorial_text(value))
    if not text:
        return ""
    # Evidence frequently starts after a card heading with a lower-case article.
    # Captions should read as a sentence without changing the observed claim.
    return text[:1].upper() + text[1:]


def _label_reference(value: str) -> str:
    """Render an observed UI label as a grammatical viewer reference.

    Labels are evidence, not prose.  In particular, possessive labels such
    as ``YOUR NAME`` cannot be inserted after an article (``the your name``).
    Quoting the exact label keeps the copy grounded while working for every
    product and language-neutral control name.
    """
    label = _clean(value, 80).strip(" :.-") or "this field"
    if re.match(r"^(?:your|my|our|their|his|her|this|that)\b", label, flags=re.IGNORECASE):
        return f"“{label}”"
    return f"the {label.lower()}"


def _viewer_fact(value: str) -> str:
    """Turn a page-local fact into natural caption copy without route labels.

    A fallback is still an editorial writer.  Prefixing every grounded fact
    with ``The X section is now visible`` technically names the current DOM
    target, but produces the crawler-like narration this layer exists to
    prevent.  Prefer the observed sentence itself; only repair the common
    participle form found on experience cards.
    """
    # Page evidence frequently prefixes the explanatory sentence with a
    # heading/category inventory (for example, ``COGNITIVE AUTOMATION FLOWS
    # AI Product Systems Integrating ...``).  Speaking that prefix makes a
    # caption sound like a DOM dump and fails the readable-scene gate. Reuse
    # the same conservative sentence extractor used during fact selection;
    # it only returns prose already present in the captured evidence.
    readable = _readable_fact(value)
    visible = _human_sentence(readable or value)
    if not visible:
        return ""
    return visible


def _subject_fact(subject: str, value: str) -> str:
    """Turn one observed card fact into a fact sentence (no invented wrappers).

    Prefer the viewer-ready observed fact as-is. Only apply a minimal
    grammatical subject when the fact cannot stand alone; LLM enrich owns
    shipped demo prose.
    """
    fact = _viewer_fact(value).strip()
    if not fact:
        return ""
    if _viewer_ready(fact, subject):
        return _human_sentence(fact)
    bare = _without_repeated_subject(fact, subject).strip()
    bare = _human_sentence(bare)
    if not bare:
        return ""
    if _viewer_ready(bare, subject):
        return bare
    # Grammar-only: article-led fragments need a subject copula.
    lowered = bare[:1].lower() + bare[1:] if bare else ""
    if re.match(r"^(?:an?|the)\s+", bare, flags=re.IGNORECASE):
        return _human_sentence(f"{subject} is {lowered}")
    if subject.casefold() not in bare.casefold():
        return _human_sentence(f"{subject}: {bare}")
    return bare


def _needs_heading_rewrite(text: str, subject: str) -> bool:
    """True only for a copied heading, not a grammatical subject sentence."""
    normalized = " ".join(text.split()).casefold()
    heading = " ".join(subject.split()).casefold()
    if not heading or not normalized.startswith(heading):
        return False
    remainder = normalized[len(heading) :].lstrip(" :—-.")
    return not re.match(r"^(?:is|was|has|shows|focuses|highlights|provides|documents)\b", remainder)


def _heading_caption(subject: str, remainder: str) -> str:
    """Subject + remainder as a human sentence of the observed fact only."""
    remainder = " ".join(str(remainder or "").split()).strip(" .:-")
    if not remainder:
        return _interaction_skeleton(subject)
    fact = f"{subject} {remainder}".strip()
    if _viewer_ready(fact, subject):
        return _human_sentence(fact)
    return _human_sentence(f"{subject}: {remainder}")


def _grounded_scene_fallback(target: str, evidence: str) -> str:
    """Viewer-ready fact from scene evidence, else interaction skeleton.

    Deterministic code must not invent purpose sentences such as
    ``This view keeps…``. LLM enrich owns shipped prose.
    """
    source = _clean(evidence, 220)
    if not source:
        return _interaction_skeleton(target)
    if (
        "chevron" in source.casefold()
        or "notification" in source.casefold()
        or (_looks_like_label_collection(source) and not re.search(r"[.!?]", source))
    ):
        return _interaction_skeleton(target)
    sentence = re.split(r"(?<=[.!?])\s+", source, maxsplit=1)[0].strip()
    sentence = _strip_structured_heading(sentence, target)
    sentence = _human_sentence(sentence or source)
    if sentence and _viewer_ready(sentence, target):
        return sentence
    return _interaction_skeleton(target)


def _interaction_skeleton(
    target: str,
    *,
    observed_value: object | None = None,
) -> str:
    """Minimal evidence-bound stub when no page fact exists.

    Real demo prose belongs to the LLM enrich pass. Deterministic code must not
    invent domain sentences, verb conjugations, or product-shaped templates.
    """
    field_label = re.sub(
        r"^(?:enter|fill|type|click|open|select|choose|press|tap)\s+",
        "",
        str(target or "").strip(),
        flags=re.IGNORECASE,
    )
    field_label = re.sub(r"\s*\([^)]*\)", "", field_label).strip() or "this control"
    field_ref = _label_reference(field_label)
    if field_ref.casefold().startswith("the "):
        field_ref = field_ref[4:]
    choice = " ".join(str(observed_value or "").split()).strip()
    if (
        choice
        and len(choice) >= 2
        and choice.casefold()
        not in {
            "select",
            "select an option",
            "choose",
            "choose an option",
            "unassigned",
            "default",
            "none",
            "n/a",
            "na",
            "-",
            "--",
        }
    ):
        return _human_sentence(
            f"{field_ref[:1].upper() + field_ref[1:]} uses the observed {choice} selection on screen"
        )
    return _human_sentence(
        f"{field_ref[:1].upper() + field_ref[1:]} is the control in focus for this step on screen"
    )


def _strip_structured_heading(value: str, heading: str) -> str:
    """Remove a repeated card/section heading from its own captured text."""
    if not heading:
        return value.strip()
    normalized = re.escape(" ".join(heading.split()))
    return re.sub(rf"^\s*{normalized}\s*[:\-â€“â€”]?\s*", "", value, flags=re.IGNORECASE).strip()


def _summary_from_categories(value: str | None, subject: str) -> str:
    """Stub: no longer invents category/role demo prose. LLM owns captions."""
    return ""


def _looks_like_screen_transcript(text: str, evidence: str = "") -> bool:
    """Detect captions that concatenate visible UI labels instead of explaining them.

    A model can satisfy lexical grounding while still producing a literal
    accessibility/DOM dump (for example a heading followed by several card
    labels).  This check intentionally uses structure and overlap, not product
    names, routes, or benchmark-specific words, so it applies to arbitrary
    applications.  A short, well-formed sentence is left alone; a long line
    that reproduces most of a dense evidence string is rejected for the
    deterministic writer to summarise.
    """
    normalized = " ".join(str(text or "").split())
    source = " ".join(str(evidence or "").split())
    if not normalized or not source:
        return False
    text_words = re.findall(r"[a-z0-9]{3,}", normalized.casefold())
    source_words = set(re.findall(r"[a-z0-9]{3,}", source.casefold()))
    if len(text_words) < 12 or len(source_words) < 14:
        return False
    overlap = sum(word in source_words for word in text_words) / max(1, len(text_words))
    # Literal dumps tend to have one final full stop but no clause punctuation;
    # natural editorial prose usually contains a comma, conjunction, or a
    # second sentence while using fewer of the source's exact tokens.
    punctuation = len(re.findall(r"[,;:!?]", normalized))
    title_runs = re.findall(
        r"\b[A-Z][A-Za-z0-9&+/-]*(?:\s+[A-Z][A-Za-z0-9&+/-]*){1,4}\b",
        normalized,
    )
    predicate = re.search(
        r"\b(?:is|are|was|were|shows|highlights|lists|presents|includes|contains|organizes|groups|tracks|provides|focuses|describes|explains|connects|pairs|uses|demonstrates)\b",
        normalized,
        flags=re.IGNORECASE,
    )
    # A real explanatory sentence often contains a second predicate (for
    # example, ``tasks are insulated ... and placed ...``).  Flattened UI
    # inventories generally have one heading verb followed by noun labels.
    # Do not classify the former as a transcript merely because several
    # technical proper nouns happen to be capitalized.
    predicate_count = len(
        re.findall(
            r"\b(?:is|are|was|were|shows|highlights|lists|presents|includes|contains|organizes|groups|tracks|provides|focuses|describes|explains|connects|pairs|uses|demonstrates)\b",
            normalized,
            flags=re.IGNORECASE,
        )
    )
    if predicate_count >= 2:
        return False
    tail = normalized[predicate.end() :] if predicate else normalized
    title_matches = list(re.finditer(r"\b[A-Z][A-Za-z0-9&+/-]*\b", tail))
    # Count separated title-case runs rather than raw proper-noun tokens. A
    # technical sentence may legitimately contain one run such as ``AWS SQS
    # Dead Letter Queue``; a screen dump has several runs interleaved with
    # lower-case UI fragments (``Check ... Next ... Queue ...``).
    title_runs_after_predicate = 0
    previous_end = -2
    for match in title_matches:
        if match.start() > previous_end + 2:
            title_runs_after_predicate += 1
        previous_end = match.end()
    # High lexical overlap alone is not enough: a legitimate explanation may
    # intentionally reuse the page's one project/role name. Require several
    # title-case runs (or the lower-overlap branch below) before classifying it
    # as a concatenated inventory.
    if (
        overlap >= 0.78
        and punctuation <= 2
        and (len(title_runs) >= 3 or title_runs_after_predicate >= 3)
    ):
        return True
    return overlap >= 0.60 and punctuation <= 1 and len(title_runs) >= 3


def _screen_transcript_summary(subject: str, raw_fact: str) -> str:
    """Stub: dense UI inventories stay as evidence; do not invent summaries."""
    return ""


def _summary_from_repeated_items(value: str, subject: str) -> str:
    """Stub: no longer invents stage/completion demo prose."""
    return ""


def _summary_from_schedule(value: str, subject: str) -> str:
    """Stub: no longer invents schedule/calendar demo prose."""
    return ""


def _summary_from_collection(value: str, subject: str) -> str:
    """Stub: no longer invents collection/purpose demo prose."""
    return ""


def _summary_from_intro(value: str, subject: str) -> str:
    """Stub: no longer invents checklist/intro demo prose."""
    return ""


def _looks_like_label_collection(value: str) -> bool:
    """True for a UI inventory that needs summarising rather than quotation."""
    labels = re.findall(r"\b[A-Z]{2,}\b", value)
    # Code names such as React or Node should not trigger this on their own;
    # an inventory with several all-caps category words is not narration.
    if len(labels) >= 5:
        return True
    title_tokens = re.findall(r"\b[A-Z][a-z][A-Za-z&/-]*\b", value)
    camel_breaks = re.findall(r"[a-z][A-Z]", value)
    return (len(title_tokens) >= 7 and value.count(".") == 0 and value.count(",") == 0) or len(
        camel_breaks
    ) >= 4


def _viewer_ready(text: str, title: str = "") -> bool:
    """Reject grounded text that is still a title dump or clipped fragment."""
    normalized = " ".join(text.split())
    if not normalized or "..." in normalized or len(normalized.split()) > 34:
        return False
    # A sentence-shaped caption must contain a predicate. Accessibility/model
    # failures often produce a comma-separated noun inventory such as
    # ``Here, Product Sections Controls Workspace.``; it can overlap evidence
    # yet still says nothing about what the viewer should understand.
    if not normalized.casefold().startswith("welcome to ") and not re.search(
        r"\b(?:is|are|was|were|has|have|lets|helps|shows|keeps|brings|groups|gathers|contains|connects|supports|organizes|tracks|lists|offers|provides|explains|uses|creates|draws|draw|moves|open|opens|opening|captures|gives|makes|enables|demonstrates|appears|remains|becomes|causes|caused|prevents|reduces|handles|processes|integrates|improves|requires|highlights|presents|introduces|focuses|describes|details|documents|covers|summarizes|summarises|includes|preserves|records|selects|select|selecting|choosing|choose|adds|completes|can|will|names|prepares|places|types|type|enters|enter|fills|fill|sketches|connects|stays|surfaces|reveals|confirms|distinguishes|carries|checks|switch|switches|label|labeled|labelled|engineer|builds|designs|develops|see|sees|contributing|contributes)\b",
        normalized,
        flags=re.IGNORECASE,
    ):
        return False
    if normalized[0].islower() or _looks_like_label_collection(normalized):
        return False
    title_words = re.findall(r"[a-z0-9]{3,}", title.lower())
    if title_words and title_words[0] == "the":
        title_words = title_words[1:]
    words = re.findall(r"[a-z0-9]{3,}", normalized.lower())
    title_prefix_words = title_words
    if len(title_words) >= 2 and words[: len(title_words) + 1] == ["the", *title_words]:
        title_prefix_words = ["the", *title_words]
    if len(title_words) >= 2 and words[: len(title_prefix_words)] == title_prefix_words:
        # Naming a project, company, or feature is useful when it is followed
        # by a real explanation. Reject only a title dump, not the natural
        # grammatical form "Feature X is ..." which prevents a presenter
        # from ever identifying the work being discussed.
        # Decorative emoji/bullets in headings make character offsets
        # unreliable; derive the remainder from normalized semantic tokens.
        # Compare semantic title tokens rather than punctuation (ampersands,
        # bullets, and decorative separators are common in real headings).
        # Match the semantic title even when the rendered sentence inserts
        # short grammatical words (for example, ``Add a remark input``).
        # ``\W*`` alone cannot cross the word ``a`` because it is itself a
        # word token, which made otherwise useful, evidence-grounded captions
        # fail the title-dump guard.
        separator = r"(?:\W+|\s+(?:a|an|the|of|to|for|and|in|on)\s+)"
        title_pattern = r"\s*" + separator.join(re.escape(word) for word in title_words)
        if title_prefix_words and title_prefix_words[0] == "the":
            title_pattern = (
                r"\s*the" + separator + separator.join(re.escape(word) for word in title_words)
            )
        title_match = re.match(title_pattern + r"\b", normalized.lower())
        if title_match is None:
            # Semantic title tokens can lead the token list after dropping a
            # short prefix such as ``On``/``In`` (length < 3). That is not a
            # title dump; the predicate check above already passed.
            return "visual exploration" not in normalized.lower()
        remainder = normalized.lower()[title_match.end() :].lstrip()
        remainder_words = re.findall(r"[a-z0-9]{3,}", remainder)
        return (
            len(remainder_words) >= 4
            and bool(
                re.match(
                    r"^(?:(?:is|are|was|were|focuses|provides|documents|combines|uses|connects|highlights|covers|organizes|keeps|offers|adds|explains|lists|groups|marks|gathers|exposes|records|preserves|captures|completes|gives|includes)\b|(?:view|page|section|workspace|area|option|field|input|form)\s+(?:is|are|was|were|focuses|provides|documents|combines|uses|connects|highlights|covers|organizes|keeps|offers|adds|explains|lists|groups|marks|gathers|exposes|records|preserves|captures|completes|gives|includes)\b)",
                    remainder.strip(),
                )
            )
            and "visual exploration" not in normalized.lower()
        )
    return True


def _readable_fact(value: str) -> str:
    """Keep the first descriptive sentence, discarding card labels/metadata."""
    had_structured_label = "::" in value
    heading, body = value.split("::", 1) if had_structured_label else ("", value)
    value = _strip_structured_heading(body, heading)
    value = _clean(value, 500)
    # Shell/accessibility inventories often begin with a capitalized menu
    # label, which can otherwise satisfy the explanatory-prefix regex below.
    # They are valid evidence for planning but must never become spoken prose.
    if (
        ("chevron" in value.casefold() or "notification" in value.casefold())
        and len(value.split()) > 14
    ) or (_looks_like_label_collection(value) and not re.search(r"[.!?]", value)):
        return ""
    # A structured capture sometimes contains a second, abbreviated heading
    # (for example "Send a Message" after "Message").  Prefer the first
    # complete sentence beginning with actual explanatory content.
    explanatory_start = re.search(
        r"\b(?:I\s+(?:engineer|build|design|focus)|This\s+(?:journal|project|page|system|section)|"
        r"Have\s+(?:an|a)|(?:An?|The)\s+[a-z][\w-]*|Real-world\s+|Generates\s+|"
        r"Proxies\s+|Webhooks\s+|Fill\s+out\s+|Here\s+is\s+how|Learn\s+how|How\s+to\s+|"
        r"Explore\s+(?:a|an)\s+|Open\s+(?:a|an|the)\s+|Tap\s+(?:the|a|an)\s+|"
        r"Saved\s+|Auto[- ]calculated\s+|Tracks\s+|Shows\s+|Provides\s+|You\s+see\s+|"
        r"Instead\s+of\s+|Architecting\s+|Building\s+|Designing\s+|"
        r"Integrating\s+|Translating\s+|Optimized\s+|Direct\s+|"
        r"Engineered\s+|Developed\s+|Improved\s+)",
        value,
    )
    if explanatory_start:
        candidate = value[explanatory_start.start() :]
        sentence = re.split(r"(?<=[.!?])\s+", candidate)[0]
        # Short but complete status statements (for example "Saved in ...")
        # remain useful evidence; callers can add connective context without
        # inventing a claim.
        if len(sentence.split()) >= 3:
            return _human_sentence(sentence)
    if _looks_like_label_collection(value):
        return ""
    # Card containers frequently begin with breadcrumb/heading metadata and
    # then contain a real first-person or explanatory sentence. Prefer that
    # prose anchor over the preceding UI labels.
    anchored = re.search(
        r"\b((?:I\s+(?:engineer|build|design|focus)|This\s+(?:journal|project|page|system|section)|"
        r"Have\s+(?:an|a)|A\s+\w+|An\s+\w+|Real-world\s+\w+|Generates\s+\w+|"
        r"Proxies\s+\w+|Webhooks\s+\w+|Fill\s+out\s+\w+|Here\s+is\s+how|Learn\s+how|"
        r"Open\s+(?:a|an|the)\s+\w+|Tap\s+(?:the|a|an)\s+\w+|Saved\s+\w+|"
        r"Auto[- ]calculated\s+\w+|Tracks\s+\w+|Shows\s+\w+|Provides\s+\w+)[\s\S]{18,}?[.!?])",
        value,
    )
    if anchored and len(anchored.group(1).split()) >= 7:
        return _human_sentence(anchored.group(1))
    sentences = re.split(r"(?<=[.!?])\s+", value)
    descriptive = next(
        (
            sentence
            for sentence in sentences
            if len(sentence.split()) >= 8
            and re.search(
                r"\b(is|are|was|were|build|built|design|designed|engineer|engineered|architecting|developed|improved|features|provides|shows|documents|explore|focus|have|learn|will|connects|coordinates|supports|combines|uses|delivers|helps|keeps|tracks|organizes|enables|powers|ingest(?:s|ing)?|process(?:es|ing)?|separat(?:es|ing)?|handl(?:es|ing)?|reduc(?:es|ing)|prevent(?:s|ing)?|improv(?:es|ing)|configur(?:es|ing)|validat(?:es|ing)|stream(?:s|ing)?|scal(?:es|ing)|monitor(?:s|ing)?|priorit(?:izes|izing)|group(?:s|ing)?|summariz(?:es|ing)|examin(?:e|es|ing)?|review(?:s|ed|ing)?|compar(?:e|es|ing)|analy[sz](?:e|es|ed|ing)?|mitigat(?:e|es|ing)|cache(?:s|d|ing)?|encapsulat(?:e|es|ing)|requir(?:es|ing)|guarantee(?:s|d|ing)?|rely(?:s|ing)|avoid(?:s|ing)?)\b",
                sentence,
                re.IGNORECASE,
            )
        ),
        None,
    )
    if descriptive:
        return _human_sentence(descriptive)
    embedded = re.search(
        r"(?:^|[.!?]\s+)((?:An|A|This|Built|Designed|Engineered|I\s+(?:engineer|build|design|focus)|"
        r"Architecting|Developed|Improved|Integrating|Building|Designing|"
        r"Translating|Optimized|Direct)\s+[A-Z]?[\s\S]{25,}?[.!?])",
        value,
        re.IGNORECASE,
    )
    if embedded:
        return _human_sentence(embedded.group(1))
    # Concise purpose labels are common in modern dashboards (for example,
    # “Manage and track appointments”). They are not a transcript when they
    # contain an observable action and object, so retain them as a compact
    # page-purpose fact for the editorial writer.
    if (
        not had_structured_label
        and 4 <= len(value.split()) <= 10
        and re.search(
            r"\b(?:manage|track|review|schedule|configure|organize|monitor|plan|create|compare|follow|coordinate|support|show|list|filter)\b",
            value,
            flags=re.IGNORECASE,
        )
    ):
        return _human_sentence(value)
    if not had_structured_label and 8 <= len(value.split()) <= 32:
        return _human_sentence(value)
    # Navigation grids and accessibility snapshots often contain repeated card
    # labels. They are evidence of what is on screen, but never prose to put
    # verbatim into a viewer-facing caption.
    # A collection of labels, dates, cards, or control text is useful evidence
    # for planning but must not be re-read as narration.  Returning no prose
    # lets the caller use a concise, truthful scene transition instead.
    return ""


def _concise_scene_subject(value: str) -> str:
    """Reduce a verbose accessible card label to its human-readable heading.

    A semantic target may legitimately contain a card's category, date, read
    time, description, and call to action. That is useful execution evidence,
    but using all of it as a narration subject creates an unreadable caption.
    Prefer the richest non-metadata line without inventing a replacement.
    """
    normalized = " ".join(value.split())
    # Accessibility labels for a selected card often collapse its index,
    # title, and completion percentage onto one line (for example
    # ``1 Week 1 28%``). Speak the semantic card name, not that metadata dump.
    compact_week = re.search(r"\b(Week\s+\d+)\b", normalized, flags=re.IGNORECASE)
    if compact_week and re.search(r"\b\d+%", normalized):
        return compact_week.group(1)
    if len(normalized) <= 96 and "\n" not in value:
        return normalized
    candidates = [" ".join(line.split()) for line in value.splitlines()]
    candidates = [
        line
        for line in candidates
        if 3 <= len(line.split()) <= 12
        and not re.fullmatch(r"[\d\s./-]+", line)
        and not re.search(r"\b(?:min|read)\b", line, flags=re.IGNORECASE)
        and not re.match(r"^(?:read|open|view)\b", line, flags=re.IGNORECASE)
    ]
    return max(candidates, key=len, default=normalized[:96])


def _observed_narration(context: ProductContext, operation, target: str) -> str:
    """Deterministic safe narration for when editorial generation is unavailable.

    It deliberately uses a visible fact, then says why this chapter matters. This
    means a provider outage cannot regress delivery to a route-label slideshow.
    """
    raw_target = target
    target = _concise_scene_subject(target)
    page = _page_for_operation(context, operation)
    source = _element_text(
        context,
        raw_target,
        getattr(getattr(operation, "target", None), "source_url", None)
        or getattr(page, "url", None),
    )
    facts = list(getattr(page, "visible_facts", []) or [])
    all_page_facts = " ".join(str(item) for item in facts)
    # Dense accessibility inventories stay as evidence only. Prefer a
    # viewer-ready readable fact; otherwise fall through to skeleton paths.
    # OperationKind only chooses which fact to prefer — never invents copy.
    if operation.kind in {
        OperationKind.NAVIGATE,
        OperationKind.OPEN_NAVIGATION_ITEM,
        OperationKind.SCROLL_TO,
        OperationKind.VERIFY_STATE,
    }:
        target_key = " ".join(re.findall(r"[a-z0-9]{3,}", target.casefold()))
        for fact in facts:
            prose = _readable_fact(fact)
            if not prose or _looks_like_screen_transcript(prose, fact):
                continue
            if target_key and not (
                target_key in fact.casefold()
                or any(word in fact.casefold() for word in target_key.split())
            ):
                continue
            if _viewer_ready(prose, target):
                return _human_sentence(prose)
            narrated = _subject_fact(target, prose)
            if narrated and _viewer_ready(narrated, target):
                return narrated
    if operation.kind is OperationKind.VERIFY_STATE:
        subject = _objective_subject(context) or target or _safe_page_subject(page) or "workspace"
        if (
            page is not None
            and "lorem ipsum" in all_page_facts.casefold()
            and subject.casefold() in {"heading", "title", "description"}
        ):
            # Objective inference may inherit a starter heading from the
            # page feature map. In that state the document title is the only
            # safe identity; never narrate template filler as product purpose.
            subject = _safe_page_subject(page)
        if re.fullmatch(r"(?:svg|canvas|element)-?\s*workspace", subject, flags=re.IGNORECASE):
            path_parts = [
                part.replace("-", " ").replace("_", " ")
                for part in urlsplit(str(getattr(page, "url", ""))).path.split("/")
                if part
            ]
            subject = _clean(path_parts[-1].title() if path_parts else "workspace", 72)
        purpose_fact = next(
            (
                prose
                for _, prose in _page_fact_records(page)
                if len(prose.split()) >= 4
                and not _looks_like_label_collection(prose)
                and not _is_metadata_fact(prose)
                and not _looks_like_screen_transcript(prose, prose)
            ),
            "",
        )
        if purpose_fact:
            if _viewer_ready(purpose_fact, subject):
                return _human_sentence(purpose_fact)
            narrated = _subject_fact(subject, purpose_fact)
            if narrated and _viewer_ready(narrated, subject):
                return narrated
        return _interaction_skeleton(subject)
    # Scroll digests may recover grouped page facts. Everything else — and any
    # scroll that finds no fact — uses observed evidence or a minimal skeleton.
    # Do not catalog OperationKind values here; new kinds must not need a code change.
    covered_groups = [
        " ".join(str(item).split())
        for item in (getattr(operation, "covered_content_groups", None) or [])
        if str(item).strip()
    ]
    if operation.kind is OperationKind.SCROLL_TO and page is not None:
        if "search" in target.casefold() and not _target_fact_narration(page, target):
            return _interaction_skeleton(target)
        if len(covered_groups) > 1:
            summaries: list[tuple[str, str]] = []
            for group_name in covered_groups:
                matching = next(
                    (
                        raw
                        for raw in facts
                        if raw.split("::", 1)[0].strip().casefold() == group_name.casefold()
                        or raw.casefold().startswith(f"{group_name.casefold()} ::")
                    ),
                    None,
                )
                prose = _readable_fact(matching) if matching else ""
                if not prose:
                    element = next(
                        (
                            item
                            for item in context.elements
                            if (item.source_url or context.url).rstrip("/") == page.url.rstrip("/")
                            and item.name.strip().casefold() == group_name.casefold()
                        ),
                        None,
                    )
                    prose = _readable_fact(element.text or "") if element else ""
                if prose and (not re.search(r"[.!?]", prose) and bool(re.search(r"\d", prose))):
                    prose = ""
                if prose:
                    summaries.append((group_name, prose))
            if summaries:
                specific = [
                    (name, fact)
                    for name, fact in summaries
                    if not re.match(
                        r"^(?:explore|a selection|featured|this section)\b",
                        fact,
                        flags=re.IGNORECASE,
                    )
                ]
                selected = specific or summaries
                if len(selected) == 2:
                    combined = " ".join(_subject_fact(name, fact) for name, fact in selected)
                    if _viewer_ready(combined):
                        return combined
                name, fact = selected[0]
                narrated = _subject_fact(name, fact)
                if narrated:
                    return narrated
        category_words = set(re.findall(r"[a-z0-9]{4,}", target.casefold()))
        if (
            category_words
            and any(
                raw.split("::", 1)[0].strip().casefold() == target.casefold()
                or target.casefold() in raw.casefold()
                for raw in facts
            )
            and (
                _looks_like_label_collection(" ".join(facts))
                or " / " in target
                or " & " in target
            )
        ):
            _local_fact_id, local_fact = _matching_page_fact(page, target)
            if local_fact:
                narrated = _subject_fact(target, local_fact)
                if narrated and _viewer_ready(narrated, target):
                    return narrated
            return _interaction_skeleton(target)
        for subject in [target, *covered_groups]:
            _fact_ref, local_fact = _matching_page_fact(page, subject)
            if local_fact:
                narrated = _subject_fact(subject, local_fact)
                if narrated and _viewer_ready(narrated, subject):
                    return narrated
        for subject in [target, *covered_groups]:
            element = next(
                (
                    item
                    for item in context.elements
                    if (item.source_url or context.url).rstrip("/") == page.url.rstrip("/")
                    and " ".join(item.name.split()).casefold()
                    == " ".join(subject.split()).casefold()
                ),
                None,
            )
            element_fact = _readable_fact((element.text if element else "") or "")
            if element_fact:
                narrated = _subject_fact(subject, element_fact)
                if narrated and _viewer_ready(narrated, subject):
                    return narrated

    # Universal fallback for every operation kind: observed fact, else skeleton.
    # Real demo sentences are written by the LLM enrich pass from scene evidence.
    # Prefer element/page text already resolved above so fixtures (and live runs)
    # without page_knowledge still ground on the observed control copy.
    element_prose = _readable_fact(source) if source else ""
    if element_prose:
        narrated = _subject_fact(target, element_prose)
        if narrated and _viewer_ready(narrated, target):
            return narrated
        grounded = _grounded_scene_fallback(target, element_prose)
        if grounded and _viewer_ready(grounded, target):
            return grounded
    if page is not None:
        target_fact = _target_fact_narration(page, target)
        if target_fact and _viewer_ready(target_fact, target):
            return target_fact
        _fact_ref, local_fact = _matching_page_fact(page, target)
        if local_fact:
            narrated = _subject_fact(target, local_fact)
            if narrated and _viewer_ready(narrated, target):
                return narrated
        for fact in facts:
            prose = _readable_fact(str(fact))
            if (
                len(prose.split()) >= 8
                and not _looks_like_label_collection(prose)
                and not _is_metadata_fact(str(fact))
                and not _looks_like_screen_transcript(prose, prose)
            ):
                subject = _clean(
                    str(getattr(page, "purpose", "") or getattr(page, "title", "") or target),
                    84,
                ) or target
                narrated = _subject_fact(subject, prose)
                if narrated and _viewer_ready(narrated, target):
                    return narrated
                break
    return _interaction_skeleton(
        target,
        observed_value=getattr(operation, "value", None),
    )



def _diversity_tokens(value: str) -> set[str]:
    """Return the semantic words used by the editorial repetition gate."""
    stop = {
        "this",
        "the",
        "view",
        "workspace",
        "keeps",
        "close",
        "hand",
        "so",
        "visible",
        "records",
        "can",
        "be",
        "narrowed",
        "and",
        "reviewed",
        "brings",
        "into",
        "current",
        "workflow",
        "with",
        "surrounding",
        "controls",
        "for",
        "context",
        "adds",
        "distinct",
        "beat",
        "walkthrough",
        "section",
        "opens",
        "its",
        "ready",
        "focused",
        "review",
        "observed",
        "information",
        "together",
        "exposes",
        "state",
        "grounding",
        "next",
        "step",
        "held",
        "long",
        "enough",
        "establish",
        "established",
        "before",
        "continues",
        "product",
        "available",
        "checkpoint",
        "shown",
        "within",
        "keeping",
        "details",
        "move",
    }
    return {word for word in re.findall(r"[a-z0-9]{4,}", value.casefold()) if word not in stop}


def _narration_repeats(candidate: str, previous: str) -> bool:
    """Mirror the hard quality gate so repair happens before rendering."""
    left, right = _diversity_tokens(candidate), _diversity_tokens(previous)
    if not left or not right:
        return False
    return len(left & right) >= 2 and len(left & right) / max(1, min(len(left), len(right))) >= 0.82


def _distinct_evidence_narration(
    context: ProductContext,
    operation: object | None,
    title: str,
    previous: str,
    *,
    page: object | None = None,
    interaction: str | None = None,
    story_phase: str | None = None,
) -> tuple[str, str | None]:
    """Select a different page-local fact when a model repeats an earlier beat.

    Returns fact-or-skeleton drafts only — no invented domain bridge prose.
    """
    page = page or (_page_for_operation(context, operation) if operation is not None else None)
    if page is None:
        return "", None
    prior_words = _diversity_tokens(previous)
    subject = _clean(title, 72) or "this area"
    records = _page_fact_records(page)
    variants_by_id: dict[str, list[str]] = {}
    for raw_fact in getattr(page, "visible_facts", []) or []:
        fact_id = _fact_id(page.url, raw_fact)
        body = raw_fact.split("::", 1)[-1]
        variants = [
            _human_sentence(part)
            for part in re.split(r"(?<=[.!?])\s+", _clean(body, 900))
            if len(part.split()) >= 7 and _readable_fact(part)
        ]
        if variants:
            variants_by_id[fact_id] = variants
    ranked: list[tuple[int, str, str]] = []
    for fact_id, prose in records:
        if not prose or _is_metadata_fact(prose):
            continue
        words = _diversity_tokens(prose)
        novel = len(words - prior_words)
        if novel <= 0:
            continue
        candidates = variants_by_id.get(fact_id) or [prose]
        for variant in candidates:
            candidate = _human_sentence(variant)
            if not candidate or _narration_repeats(candidate, previous):
                continue
            if not _viewer_ready(candidate, subject):
                continue
            ranked.append(
                (novel + len(_diversity_tokens(variant) - prior_words), fact_id, candidate)
            )
            break
    if ranked:
        _, fact_id, candidate = max(ranked, key=lambda item: item[0])
        return candidate, fact_id
    fallback_fact_id, fallback_fact = records[0] if records else (None, "")
    if fallback_fact:
        candidate = _human_sentence(fallback_fact)
        if (
            candidate
            and _viewer_ready(candidate, subject)
            and not _narration_repeats(candidate, previous)
        ):
            return candidate, fallback_fact_id
    skeleton = _interaction_skeleton(subject)
    if skeleton and not _narration_repeats(skeleton, previous):
        return skeleton, None
    return "", None


def _repair_repeated_narration(
    context: ProductContext,
    scenes: list[EditorialScene],
) -> list[EditorialScene]:
    """Repair model repeats while preserving immutable scene contracts."""
    repaired: list[EditorialScene] = []
    for scene in scenes:
        previous = next(
            (
                prior
                for prior in repaired
                if prior.interaction != "opening"
                and _narration_repeats(scene.narration, prior.narration)
            ),
            None,
        )
        if (
            previous is not None
            and scene.operation_id is not None
            and not re.search(
                r"architecture card|directional connection|chat flow|architecture map|text tool|visible controls and information|starting context|field is visible|open .*fields|type the observed|choose the observed|submit .*observed",
                scene.narration,
                flags=re.IGNORECASE,
            )
        ):
            page = next(
                (
                    item
                    for item in context.page_knowledge
                    if scene.page_url
                    and _canonical_page_url(item.url) == _canonical_page_url(scene.page_url)
                ),
                None,
            )
            local, fact_id = _distinct_evidence_narration(
                context,
                None,
                _clean(scene.title, 72) or "this area",
                previous.narration,
                page=page,
                interaction=scene.interaction,
                story_phase=scene.story_phase,
            )
            if local and _viewer_ready(local, scene.title):
                evidence = list(scene.evidence)
                if fact_id and fact_id not in evidence:
                    evidence.append(fact_id)
                scene = scene.model_copy(update={"narration": local, "evidence": evidence})
        repaired.append(scene)
    return repaired


def _repair_fragmented_editorial_copy(
    context: ProductContext, scenes: list[EditorialScene]
) -> list[EditorialScene]:
    """Normalize heading-led fragments left by a provider or DOM fallback.

    These rewrites are deliberately linguistic, not product-specific: they
    preserve the observed subject and clause while removing patterns that
    read like a route label (``X highlights ...`` or ``This view explains
    X, including ...``).  Evidence, timing, and scene identity remain
    unchanged.
    """
    repaired: list[EditorialScene] = []
    for scene in scenes:
        text = " ".join(scene.narration.split()).strip()
        if scene.operation_id is not None:
            repaired_provider_fragment = False
            # Structured providers occasionally splice the section heading
            # into a half-finished template (``X option includes we examine
            # ...``).  Repair the grammar while preserving the model's
            # observed subject and clause; never invent a product-specific
            # description here.
            malformed = re.match(
                r"^(?:The\s+)?(?P<subject>[^.]{2,100}?)\s+option\s+includes\s+(?P<rest>.+)$",
                text,
                re.IGNORECASE,
            )
            if malformed and len(malformed.group("rest").split()) >= 3:
                subject = malformed.group("subject").strip(" :.-")
                rest = malformed.group("rest").strip(" .")
                # ``option includes`` is a common model/template splice. A
                # subject-led predicate keeps the same observed clause while
                # satisfying the reader-ready sentence contract.
                rest = re.sub(r"^we\s+examine\s+", "", rest, flags=re.IGNORECASE)
                text = _human_sentence(f"The {subject} highlights {rest}")
                repaired_provider_fragment = True
            # Avoid doubled determiners caused by a title that already starts
            # with ``The`` (``The The Challenge ...``).
            text = re.sub(r"^The\s+The\s+", "The ", text)
            match = re.match(
                r"^(?P<subject>[^.]{1,80}?)\s+highlights\s+(?P<rest>.+)$", text, re.IGNORECASE
            )
            if match and len(match.group("rest").split()) >= 3 and not repaired_provider_fragment:
                subject = match.group("subject").strip(" :.-")
                rest = match.group("rest").strip(" .")
                text = _human_sentence(f"The {subject} option includes {rest}")
            else:
                match = re.match(
                    r"^This\s+view\s+explains\s+(?P<subject>[^,]{2,80}),\s+including\s+(?P<rest>.+)$",
                    text,
                    re.IGNORECASE,
                )
                if match and len(match.group("rest").split()) >= 3:
                    subject = match.group("subject").strip(" :.-")
                    rest = match.group("rest").strip(" .")
                    text = _human_sentence(f"The {subject} view brings together {rest}")
        repaired.append(scene.model_copy(update={"narration": text}) if text else scene)
    return repaired


def _target_fact_narration(page: object | None, target: str) -> str:
    """Build fact-or-skeleton prose from the fact whose heading matches a target."""
    if page is None:
        return ""
    normalized = " ".join(target.split()).casefold()
    facts = list(getattr(page, "visible_facts", []) or [])

    def label_key(value: str) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))

    normalized_key = label_key(normalized)
    exact = next(
        (fact for fact in facts if label_key(fact.split("::", 1)[0].strip()) == normalized_key),
        None,
    )
    if exact is None:
        return ""
    prose = _readable_fact(exact)
    if prose and _looks_like_screen_transcript(prose, exact):
        return ""
    if not prose or _looks_like_label_collection(prose):
        raw = exact.split("::", 1)[1].strip() if "::" in exact else exact.strip()
        raw = re.sub(r"\s+", " ", raw)
        raw = raw.split("←", 1)[0].strip()
        sentences = re.split(r"(?<=[.!?])\s+", raw)
        explanatory = next(
            (
                sentence.strip(" -—")
                for sentence in sentences
                if len(re.findall(r"[A-Za-z]{3,}", sentence)) >= 8
                and not _looks_like_label_collection(sentence)
            ),
            "",
        )
        if not explanatory and sentences:
            lead = " ".join(sentences[:2]).strip(" -—")
            if len(re.findall(r"[A-Za-z]{3,}", lead)) >= 8:
                explanatory = lead
        if explanatory:
            narrated = _subject_fact(target, explanatory)
            if narrated and _viewer_ready(narrated, target):
                return narrated
        return ""
    if prose and _viewer_ready(prose, target):
        return _human_sentence(prose)
    narrated = _subject_fact(target, exact)
    if narrated and _viewer_ready(narrated, target):
        return narrated
    return ""


def _facts(context: ProductContext) -> list[EditorialFact]:
    facts: list[EditorialFact] = []
    for item in context.elements:
        text = _safe_editorial_text(_clean(item.text or ""))
        if text and len(text) >= 18:
            facts.append(
                EditorialFact(
                    text=text,
                    evidence=[f"element:{item.name}", f"source:{item.source_url or context.url}"],
                )
            )
    visible_opening = _safe_editorial_text(_clean(context.visible_text))
    if visible_opening:
        facts.insert(0, EditorialFact(text=visible_opening, evidence=[f"page:{context.url}"]))
    for page in context.page_knowledge:
        for fact in page.visible_facts[:12]:
            prose = _safe_editorial_text(_readable_fact(fact))
            if prose:
                facts.append(
                    EditorialFact(
                        text=prose, evidence=[f"page:{page.url}", _fact_id(page.url, fact)]
                    )
                )
    return facts[:24]


def _editorial_evidence(context: ProductContext) -> str:
    """Serialize page-scoped facts for the writer without flattening their provenance."""
    pages = [
        {
            "url": page.url,
            "purpose": page.purpose,
            "sections": page.visible_sections[:12],
            "facts": [
                {"id": _fact_id(page.url, fact), "text": _safe_editorial_text(fact)}
                for fact in page.visible_facts[:16]
                if _safe_editorial_text(fact)
            ],
        }
        for page in context.page_knowledge
    ]
    return json.dumps(
        {
            "opening": {
                "url": context.url,
                "title": context.title,
                "text": _safe_editorial_text(context.visible_text[:3000]),
            },
            "pages": pages,
            "elements": [
                {
                    "name": item.name,
                    "text": _safe_editorial_text(item.text or ""),
                    "source_url": item.source_url or context.url,
                }
                for item in context.elements[:80]
                if _safe_editorial_text(item.text or item.name)
            ],
        },
        ensure_ascii=False,
    )


def _objective_subject(context: ProductContext) -> str:
    """Return the requested subject in presenter language, without copying a route.

    The objective is an explicit viewer requirement, so it is valid editorial
    context even where an application's opening DOM is a sparse data table.
    Prefer the structured primary entity and retain a concise user-requested
    feature phrase only when one exists.
    """
    objective = getattr(context, "objective", None)
    if objective is None:
        return ""
    primary = _clean(str(getattr(objective, "primary_entity", "") or ""), 72)
    primary = re.sub(
        r"^(?:(?:the|a|an|authenticated|operational|relevant|requested|actual|visible|current|primary)\s+)+",
        "",
        primary,
        flags=re.IGNORECASE,
    ).strip()
    generic_audience_terms = {
        "learner",
        "learners",
        "user",
        "users",
        "prospect",
        "prospects",
        "customer",
        "customers",
        "manager",
        "managers",
        "developer",
        "developers",
        "team",
        "teams",
        "person",
        "people",
        "audience",
    }
    if (
        primary
        and primary.casefold() not in generic_audience_terms
        and not re.fullmatch(
            r"(?:show|demonstrate|explore|understand|create|make|build|present|tour|"
            r"demo|walkthrough|workflow|flow|feature|module|area)",
            primary,
            re.IGNORECASE,
        )
    ):
        return primary
    features = [
        _clean(str(value), 48)
        for value in getattr(objective, "requested_features", []) or []
        if _clean(str(value), 48)
        and not re.fullmatch(
            r"(?:configuration|settings?|flow|workflow|demo|walkthrough|"
            r"show|demonstrate|explore|understand|create|make|build|"
            r"present|presenting|tour)",
            _clean(str(value), 48),
            re.IGNORECASE,
        )
    ]
    if not features:
        return ""
    # Audience adjectives (for example ``learner-facing``) are not the
    # subject of a product tour. Combine adjacent domain terms so the opening
    # says ``study planning experience`` rather than ``learner`` or ``study``.
    chosen = [
        value
        for value in features
        if value.casefold() not in generic_audience_terms
        and value.casefold()
        not in {
            "facing",
            "application",
            "experience",
            "view",
            "first",
            "what",
            "helps",
            "plan",
            "track",
        }
    ]
    if len(chosen) >= 2:
        return " ".join(chosen[:2]) + " experience"
    return chosen[0] if chosen else features[0]


def _fallback_presenter_intro(product: str, opening: str, subject: str = "") -> str:
    """Thin evidence draft for opening; LLM enrich owns shipped welcome prose.

    ``Welcome to {product}.`` plus a viewer-ready opening fact when available,
    otherwise an interaction skeleton. Not a polished presenter monologue.
    """
    product = _clean(product) or "this product"
    product = product.split("|", 1)[0].strip() or product
    opening = _safe_editorial_text(_clean(opening)) or ""
    opening = opening.rstrip(".")
    opening_words = opening.split()
    if len(opening_words) > 24:
        sentences = re.split(r"(?<=[.!?])\s+", opening)
        opening = (
            sentences[0].strip()
            if sentences and sentences[0].strip()
            else " ".join(opening_words[:24])
        )
    parts = [f"Welcome to {product}."]
    if (
        opening
        and _viewer_ready(opening)
        and "control in focus for this step on screen" not in opening.casefold()
    ):
        parts.append(_human_sentence(opening))
    elif subject:
        # A missing/weak page fact should not leak an internal locator
        # template ("control in focus") into the presenter opening.  Keep the
        # fallback product-neutral while naming the requested subject so the
        # audience knows what this walkthrough will establish.
        subject_text = _clean(subject, 72)
        parts.append(
            f"Today we'll walk through {subject_text} and show the visible workflow "
            "from its starting context to the verified result."
        )
    return " ".join(parts)


def _page_intro_from_fact(page: object, fact: str) -> str:
    """Fact-or-skeleton page intro; no specialty inventing branches."""
    prose = _viewer_fact(fact).strip()
    if not prose:
        return ""
    subject = _clean(
        str(getattr(page, "purpose", "") or getattr(page, "title", "") or "this workspace"), 72
    )
    if re.fullmatch(r"(?:svg|canvas|element)-?\s*workspace", subject, flags=re.IGNORECASE):
        path_parts = [
            part.replace("-", " ").replace("_", " ")
            for part in urlsplit(str(getattr(page, "url", ""))).path.split("/")
            if part
        ]
        subject = _clean(path_parts[-1].title() if path_parts else "workspace", 72)
    if _viewer_ready(prose, subject):
        return _human_sentence(prose)
    return _interaction_skeleton(subject)


def _configuration_bridge(context: ProductContext, page: object | None) -> str:
    """Stub: no invented configuration bridge prose."""
    return ""


def _exploration_context_bridge(
    context: ProductContext, page: object | None
) -> tuple[str, str] | None:
    """Stub: no invented exploration-context bridge prose."""
    return None

__all__ = [
    "_SENSITIVE_EDITORIAL_PATTERN",
    "_canonical_page_url",
    "_clean",
    "_concise_scene_subject",
    "_concise_title",
    "_configuration_bridge",
    "_distinct_evidence_narration",
    "_diversity_tokens",
    "_editorial_evidence",
    "_element_text",
    "_exploration_context_bridge",
    "_fact_id",
    "_facts",
    "_fallback_presenter_intro",
    "_grounded_scene_fallback",
    "_heading_caption",
    "_human_sentence",
    "_interaction_skeleton",
    "_is_metadata_fact",
    "_label_reference",
    "_looks_like_label_collection",
    "_looks_like_screen_transcript",
    "_matching_page_fact",
    "_meaningful_section_label",
    "_narration_repeats",
    "_needs_heading_rewrite",
    "_objective_subject",
    "_observed_narration",
    "_page_fact_records",
    "_page_for_operation",
    "_page_intro_from_fact",
    "_readable_fact",
    "_repair_fragmented_editorial_copy",
    "_repair_repeated_narration",
    "_safe_editorial_text",
    "_safe_page_subject",
    "_scene_fact",
    "_screen_transcript_summary",
    "_sentence",
    "_sentence_complete",
    "_strip_structured_heading",
    "_subject_fact",
    "_summary_from_categories",
    "_summary_from_collection",
    "_summary_from_intro",
    "_summary_from_repeated_items",
    "_summary_from_schedule",
    "_target_fact_narration",
    "_viewer_fact",
    "_viewer_ready",
    "_without_repeated_subject",
]
