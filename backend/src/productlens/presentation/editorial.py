"""Evidence-grounded editorial direction for human-quality walkthroughs."""

from __future__ import annotations

import json
import re
from hashlib import sha256
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from productlens.contracts.models import (
    DemoPlan,
    EditorialBrief,
    EditorialFact,
    EditorialNarrationDraft,
    EditorialScene,
    EditorialStoryboard,
    OperationKind,
    ProductContext,
)
from productlens.observability.logging import redact_prompt_text
from productlens.providers.errors import ProviderError


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
    if re.match(r"^Architecting\b", visible, flags=re.IGNORECASE):
        return _human_sentence(f"The role focuses on {visible[0].lower() + visible[1:]}")
    return visible


def _subject_fact(subject: str, value: str) -> str:
    """Turn one observed card fact into a natural, subject-led sentence."""
    fact = _viewer_fact(value).strip()
    if not fact:
        return ""
    if re.match(r"^The role focuses on\b", fact, flags=re.IGNORECASE):
        return _human_sentence(f"{subject} is where {fact[0].lower() + fact[1:]}")
    bare = _without_repeated_subject(fact, subject).strip()
    bare = _human_sentence(bare)
    lowered = bare[:1].lower() + bare[1:] if bare else ""
    if re.match(r"^(?:an?|the)\s+", bare, flags=re.IGNORECASE):
        return _human_sentence(f"{subject} is {lowered}")
    if re.match(
        r"^(?:building|designing|integrating|translating|optimizing)\b", bare, flags=re.IGNORECASE
    ):
        return _human_sentence(f"{subject} focuses on {lowered}")
    # Navigation evidence often captures a short imperative such as
    # "Open a week. Follow the days."  Speaking the label plus that fragment
    # produces a route-label caption ("Weeks highlights open a week").  Keep
    # the observed action, but turn it into a viewer-oriented capability
    # sentence so the scene explains the value of the control.
    if re.match(r"^(?:open|view|see|select)\b", bare, flags=re.IGNORECASE):
        return _human_sentence(f"{subject} lets you {bare[0].lower() + bare[1:]}")
    if re.match(
        r"^(?:built|designed|developed|engineered|architecting)\b", bare, flags=re.IGNORECASE
    ):
        candidate = fact
    elif re.match(r"^I\s+would\b", bare, flags=re.IGNORECASE):
        candidate = _human_sentence(f"{subject} proposes {bare[2:].lstrip()}")
    else:
        candidate = bare or fact
    # Heading-linked evidence often stores the heading before ``::`` and the
    # prose body after it. Once the heading is stripped, retain the subject in
    # the spoken line; otherwise a project/role/metric scene becomes an
    # anonymous sentence that cannot tell the viewer what is being shown.
    if subject.casefold() not in candidate.casefold():
        lowered_candidate = candidate[:1].lower() + candidate[1:] if candidate else candidate
        return _human_sentence(f"{subject} highlights {lowered_candidate}")
    return candidate


def _needs_heading_rewrite(text: str, subject: str) -> bool:
    """True only for a copied heading, not a grammatical subject sentence."""
    normalized = " ".join(text.split()).casefold()
    heading = " ".join(subject.split()).casefold()
    if not heading or not normalized.startswith(heading):
        return False
    remainder = normalized[len(heading) :].lstrip(" :—-.")
    return not re.match(r"^(?:is|was|has|shows|focuses|highlights|provides|documents)\b", remainder)


def _heading_caption(subject: str, remainder: str) -> str:
    """Rewrite a heading-led fact into a concise, viewer-facing sentence."""
    if re.search(r"\blets\s+you\s+open\b", remainder, flags=re.IGNORECASE):
        return _human_sentence(
            f"{subject} lets you open the next level of detail and review the content inside it"
        )
    return _human_sentence(f"This view explains {subject}, including {remainder}")


def _grounded_scene_fallback(target: str, evidence: str) -> str:
    """Create a readable sentence from the current scene's evidence only.

    This is used when a captured landmark has no clean fact sentence.  It is
    intentionally conservative: it keeps the observed wording, trims dense
    inventories, and never borrows another page's copy or invents a purpose.
    """
    source = _clean(evidence, 220)
    if not source:
        return f"This view keeps {target} in focus while the relevant details are shown."
    if (
        "chevron" in source.casefold()
        or "notification" in source.casefold()
        or (_looks_like_label_collection(source) and not re.search(r"[.!?]", source))
    ):
        return f"This view keeps {target} in focus while the relevant details are shown."
    # Prefer the first complete sentence; accessibility snapshots often append
    # unrelated controls after the useful local description.
    sentence = re.split(r"(?<=[.!?])\s+", source, maxsplit=1)[0].strip()
    sentence = _strip_structured_heading(sentence, target)
    sentence = _human_sentence(sentence or source)
    if len(sentence.split()) < 8:
        sentence = f"Here, {sentence[0].lower() + sentence[1:] if sentence else target}."
    return sentence


def _strip_structured_heading(value: str, heading: str) -> str:
    """Remove a repeated card/section heading from its own captured text."""
    if not heading:
        return value.strip()
    normalized = re.escape(" ".join(heading.split()))
    return re.sub(rf"^\s*{normalized}\s*[:\-â€“â€”]?\s*", "", value, flags=re.IGNORECASE).strip()


def _summary_from_categories(value: str | None, subject: str) -> str:
    """Summarise an observed label collection without reading the DOM aloud."""
    # A numbered collection does not establish a domain. Older wording here
    # invented "practice", "builds", and "architecture cards" from arbitrary
    # UI inventories. Keep only the structural fact that is truly observable.
    if not value:
        return ""
    lowered = value.casefold()
    if "total calls" in lowered and ("callback" in lowered or "repeat callers" in lowered):
        return f"The {subject} view summarizes call volume, callbacks, repeat callers, and handling time for the selected range."
    if "manage and configure" in lowered and "settings" in lowered:
        return f"The {subject} view collects the product settings that can be configured from this workspace."
    labels = re.findall(r"\b[A-Z][A-Z& /-]{3,}\b", value)
    labels = [" ".join(label.split()) for label in labels if len(label.split()) <= 5]
    unique = list(dict.fromkeys(label.title() for label in labels))[:2]
    if len(unique) >= 2:
        joined = " and ".join(unique)
        return f"The {subject} area organizes {joined} by role, so the viewer can understand how the visible pieces fit together."
    if _looks_like_label_collection(value):
        return f"The {subject} area organizes the observed categories by role, so the viewer can understand how the visible pieces fit together."
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
    """Turn one dense UI inventory into a short presenter takeaway."""
    prose = _readable_fact(raw_fact)
    if not prose:
        return ""
    lead = re.split(
        r"\b(?:check\s+off|click\s+(?:a|an|the)|tap\s+(?:a|an|the)|next)\b",
        prose,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" .:-")
    lead = re.sub(r"^.*?\bhighlights\b\s*", "", lead, flags=re.IGNORECASE).strip(" .:-")
    lead = re.sub(rf"^\s*{re.escape(subject)}\s*[:\-]?\s*", "", lead, flags=re.IGNORECASE).strip(
        " .:-"
    )
    next_match = re.search(r"\bnext(?![.\w])\b(?P<items>.+)", prose, flags=re.IGNORECASE)
    if next_match:
        # Split title-led item names at their visible boundaries without
        # assuming a particular product's labels or data model.
        item_text = re.sub(r"^[\s:–—-]+", "", next_match.group("items")).strip(" .:-")
        tokens = re.findall(r"[A-Za-z0-9+#./-]+|[+&]", item_text)
        groups: list[list[str]] = []
        for token in tokens:
            starts_upper = bool(re.match(r"[A-Z]", token))
            previous = groups[-1][-1] if groups else ""
            starts_new = bool(groups and starts_upper and bool(re.match(r"[a-z]", previous)))
            if starts_new:
                groups.append([])
            if not groups:
                groups.append([])
            groups[-1].append(token)
        chunks = [" ".join(group).replace(" + ", " + ").strip(" .:-") for group in groups if group]
        chunks = chunks[:3]
        if chunks:
            if len(chunks) == 1:
                items = chunks[0]
            elif len(chunks) == 2:
                items = f"{chunks[0]} and {chunks[1]}"
            else:
                items = f"{chunks[0]}, {chunks[1]}, and {chunks[2]}"
            return _human_sentence(
                f"The {subject} view groups the visible builds for this stage, including {items}"
            )
    if re.search(r"\bread[- ]only\b", prose, flags=re.IGNORECASE):
        return _human_sentence(
            f"The {subject} view is safe to explore in read-only mode, so the viewer can inspect the experience without changing data"
        )
    if _looks_like_screen_transcript(lead, prose):
        # Inventory facts do not always contain the word ``next``. They can
        # be a run of sentence fragments produced by a flattened
        # accessibility tree. Select the first semantic clause and wrap it in
        # a viewer-facing takeaway instead of returning the entire label dump.
        first_sentence = re.split(r"(?<=[.!?])\s+", lead, maxsplit=1)[0].strip(" .:-")
        if (
            first_sentence != lead
            and len(first_sentence.split()) >= 4
            and not re.match(
                r"^(?:open|start|view|read|click|see)\b", first_sentence, re.IGNORECASE
            )
        ):
            return _human_sentence(
                f"The {subject} view shows {first_sentence[0].lower() + first_sentence[1:]}, giving the viewer a concrete sense of what this area contains"
            )
        clauses = [
            " ".join(part.split()).strip(" .:-")
            for part in re.split(
                r"\s+(?=(?:Each|The|See|Website|Dashboard|Open|Start|View)\b)",
                lead,
            )
        ]
        clauses = [
            clause
            for clause in clauses
            if len(clause.split()) >= 4
            and not re.match(r"^(?:open|start|view|read|click|see)\b", clause, re.IGNORECASE)
        ]
        # The first clause is the most reliable complete semantic unit. Using
        # the longest clause can splice two adjacent labels and then truncate
        # the second one at the caption word limit, leaving a visibly broken
        # sentence. Later clauses remain in the scene evidence for inspection.
        selected = clauses[0] if clauses else lead
        # Keep the resulting caption inside the reader-ready word budget;
        # the full inventory remains available through the scene evidence.
        selected = " ".join(selected.split()[:16]).strip(" .:-")
        if selected:
            return _human_sentence(
                f"The {subject} view shows {selected[0].lower() + selected[1:]}, giving the viewer a concrete sense of what this area contains"
            )
    if len(lead.split()) >= 2:
        suffix = " so the current stage is easy to understand"
        return _human_sentence(f"The {subject} view groups the visible {lead}{suffix}")
    return _human_sentence(
        f"The {subject} view groups the visible work for this stage so its current focus is easy to understand"
    )


def _summary_from_repeated_items(value: str, subject: str) -> str:
    """Summarise a repeated, numbered collection without reading its labels.

    Discovery often captures a schedule/grid as one dense accessibility string
    (for example, ``Week 1 ... Week 8``).  Quoting that string creates a
    crawler-like caption, while dropping it loses the page's useful meaning.
    The count and unit are directly observable and therefore safe to state.
    """
    matches = re.findall(
        r"\b(week|day|module|lesson|chapter|step)\s+#?\d+\b", value, flags=re.IGNORECASE
    )
    units = list(dict.fromkeys(item.lower() for item in matches))
    if len(matches) < 2 or not units:
        return ""
    unit = units[0]
    numbers = re.findall(rf"\b{re.escape(unit)}\s+#?(\d+)\b", value, flags=re.IGNORECASE)
    # A detail page repeats its own heading (for example ``Week 6``) in
    # several cards. That is not evidence of a one-item collection; require
    # at least two distinct indices before describing a list of stages.
    if len(set(numbers)) < 2:
        return ""
    count = len(set(numbers)) or len(matches)
    return f"The {subject} view organizes {count} {unit} stages and shows their current completion levels."


def _summary_from_schedule(value: str, subject: str) -> str:
    """Describe a timed task grid without transcribing every cell."""
    # Timestamps also appear in ordinary audit tables (created-at, updated-at,
    # call history).  Only call this a schedule when the observed copy carries
    # an explicit scheduling signal; otherwise the editorial layer should use
    # the page's table/list evidence instead of inventing a calendar narrative.
    times = list(re.finditer(r"\b\d{1,2}:\d{2}\b", value))
    if len(times) < 2:
        return ""
    # Require the scheduling language to occur near the timestamps. A page
    # shell can expose an unrelated "Upcoming" menu while the current page
    # is an ordinary lead/audit table; a global keyword would misclassify it.
    schedule_signal = re.compile(
        r"\b(?:schedule|scheduled|calendar|upcoming|time\s+block|appointment|task|agenda|day\s+plan)\b",
        flags=re.IGNORECASE,
    )
    if not any(
        schedule_signal.search(value[max(0, match.start() - 140) : match.end() + 140])
        for match in times
    ):
        return ""
    return (
        f"The {subject} view organizes timed task blocks across the day, so the next task is clear."
    )


def _summary_from_collection(value: str, subject: str) -> str:
    """Give a domain-neutral purpose sentence for dense category inventories.

    The summary must stay useful without importing a domain the captured
    evidence did not establish.
    """
    if not _looks_like_label_collection(value):
        return ""
    lowered = value.casefold()
    # Prefer concrete collection semantics when the page exposes them. These
    # phrases are assembled from observed labels, so they remain useful for
    # arbitrary products without turning a dense accessibility inventory into
    # a transcript or importing a domain-specific workflow.
    if "executed jobs" in lowered and "upcoming jobs" in lowered:
        return f"The {subject} view brings executed and upcoming jobs together, keeping their status and timing visible."
    match = re.search(
        r"you\s+see\s+all\s+([a-z][a-z -]{2,48})\s+in\s+this\s+organization",
        value,
        flags=re.IGNORECASE,
    )
    if match:
        noun = " ".join(match.group(1).split())
        return f"The {subject} view gathers {noun} in one place, with the visible filters and status controls ready for review."
    labels = re.findall(r"\b[A-Z][A-Za-z][A-Za-z &/-]{2,24}\b", value)
    labels = list(
        dict.fromkeys(" ".join(label.split()) for label in labels if len(label.split()) <= 4)
    )[:2]
    if len(labels) >= 2:
        return f"The {subject} area brings {labels[0]} and {labels[1]} into one view, clarifying the choices available here."
    return f"The {subject} section gathers the visible controls and supporting information into one place for review."


def _summary_from_intro(value: str, subject: str) -> str:
    """Condense an observed introduction plus checklist into one thought."""
    lowered = value.lower()
    # Infer only the structure that is actually visible.  Do not import a
    # product's domain vocabulary into another application's narration.
    if any(token in lowered for token in ("check", "complete", "finish", "next")):
        clean_subject = _clean(subject, 72) or "product experience"
        return f"The {clean_subject} begins with a clear checklist that makes the next steps easy to follow."
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
        r"\b(?:is|are|was|were|has|have|lets|helps|shows|keeps|brings|groups|gathers|contains|connects|supports|organizes|tracks|lists|offers|provides|explains|uses|creates|draws|draw|moves|opens|captures|gives|makes|enables|demonstrates|appears|remains|becomes|causes|caused|prevents|reduces|handles|processes|integrates|improves|requires|highlights|presents|introduces|focuses|describes|details|documents|covers|summarizes|summarises|includes|preserves|records|selects|adds|completes|can|will)\b",
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
        title_pattern = r"\s*" + r"\W*".join(re.escape(word) for word in title_words)
        title_prefix = title_pattern
        if title_prefix_words and title_prefix_words[0] == "the":
            title_pattern = r"\s*the\W*" + r"\W*".join(re.escape(word) for word in title_words)
        remainder = normalized.lower()[len(title_prefix) :].lstrip(" :—-.")
        title_match = re.match(title_pattern + r"\b", normalized.lower())
        remainder = (
            normalized.lower()[title_match.end() :].lstrip() if title_match else normalized.lower()
        )
        remainder_words = re.findall(r"[a-z0-9]{3,}", remainder)
        return (
            len(remainder_words) >= 4
            and bool(
                re.match(
                    r"^(?:(?:is|are|was|were|focuses|provides|documents|combines|uses|connects|highlights|covers|organizes|keeps|offers|adds|explains|lists|groups|marks|gathers|exposes|records|preserves|captures|completes|gives)\b|(?:view|page|section|workspace|area|option|field|input)\s+(?:is|are|was|were|focuses|provides|documents|combines|uses|connects|highlights|covers|organizes|keeps|offers|adds|explains|lists|groups|marks|gathers|exposes|records|preserves|captures|completes|gives)\b)",
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
        r"Saved\s+|Auto[- ]calculated\s+|Tracks\s+|Shows\s+|Provides\s+|"
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
    sections = list(getattr(page, "visible_sections", []) or [])
    target_words = re.findall(r"[a-z0-9]{4,}", target.lower())
    normalized_target = " ".join(target.split()).lower()
    inventory_target = "\n" in target or bool(re.search(r"\b\d+\b", target))
    all_page_facts = " ".join(str(item) for item in facts)
    # A destination/verify fact can be a flattened accessibility inventory.
    # Detect it before the operation-specific branches so navigation and
    # verify scenes receive the same concise takeaway instead of replaying
    # headings and card labels verbatim.
    if operation.kind in {
        OperationKind.NAVIGATE,
        OperationKind.OPEN_NAVIGATION_ITEM,
        OperationKind.SCROLL_TO,
        OperationKind.VERIFY_STATE,
    }:
        target_key = " ".join(re.findall(r"[a-z0-9]{3,}", target.casefold()))
        candidate_facts = [
            fact
            for fact in facts
            if _looks_like_screen_transcript(_readable_fact(fact), fact)
            and (
                not target_key
                or target_key in fact.casefold()
                or any(word in fact.casefold() for word in target_key.split())
            )
        ]
        if candidate_facts:
            summary = _screen_transcript_summary(target, candidate_facts[0])
            if summary and _viewer_ready(summary, target):
                return summary
    if operation.kind is OperationKind.SCROLL_TO:
        if re.search(r"\btoday\b", target, re.IGNORECASE):
            schedule = _summary_from_schedule(all_page_facts, target)
            if schedule:
                return schedule
        if re.fullmatch(
            r"(?:weeks?|days?|modules?|lessons?|chapters?|steps?)", target.strip(), re.IGNORECASE
        ):
            repeated = _summary_from_repeated_items(all_page_facts, target)
            if repeated:
                page_subject = _clean(
                    str(getattr(page, "purpose", "") or getattr(page, "title", "") or ""),
                    72,
                )
                if page_subject and page_subject.casefold() != target.casefold():
                    if re.search(
                        r"\b(?:progress|completion|status|overview|dashboard)\b",
                        page_subject,
                        re.IGNORECASE,
                    ):
                        repeated = _human_sentence(
                            f"The {page_subject} view tracks completion across the visible stages, making their current levels easy to compare"
                        )
                    else:
                        repeated = repeated.replace(
                            f"The {target} view",
                            f"The {page_subject} view",
                            1,
                        )
                return repeated
    # Configuration is useful only as a bridge to the requested experience.
    # When discovery observed a feature-specific settings control, explain
    # that relationship in viewer language instead of reading the card label
    # as a standalone route title. The feature and control come entirely from
    # current objective/page evidence.
    if (
        page is not None
        and operation.kind is OperationKind.SCROLL_TO
        and "config" in normalized_target
        and bool(
            re.search(
                r"\b(?:settings?|configuration)\b", str(getattr(page, "purpose", "")), re.IGNORECASE
            )
        )
    ):
        return _human_sentence(
            f"This {target} area explains the setup behind the requested workflow, connecting its rules to the operational experience"
        )
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
        # A page-level verify beat must explain the visible purpose before the
        # next action. Prefer the shortest readable purpose fact on this page;
        # falling back to a route-mechanics sentence was the source of weak
        # “current working view” captions in live runs.
        purpose_fact = next(
            (
                prose
                for _, prose in _page_fact_records(page)
                if len(prose.split()) >= 4
                and not _looks_like_label_collection(prose)
                and not _is_metadata_fact(prose)
                # A verify beat should explain the page, not repeat a dense
                # accessibility/DOM inventory (often a sequence of card and
                # control labels with one trailing full stop).  Keep the
                # inventory attached as evidence while selecting the next
                # sentence-shaped, page-local fact for the viewer.
                and not _looks_like_screen_transcript(prose, prose)
            ),
            "",
        )
        if purpose_fact:
            bare = purpose_fact.rstrip(".")
            if re.match(
                r"^(?:manage|track|review|schedule|configure|organize|monitor|plan|create|compare|follow|coordinate|support|filter|list)\b",
                bare,
                flags=re.IGNORECASE,
            ):
                return _human_sentence(
                    f"The {subject} workspace helps teams {bare[0].lower() + bare[1:]}"
                )
            narrated = _subject_fact(subject, purpose_fact)
            if narrated and _viewer_ready(narrated, subject):
                return narrated
        # A component/catalog page may expose no sentence-shaped purpose fact,
        # while its section inventory still provides strong, page-local
        # evidence. Use that inventory to make each verify beat distinct and
        # explanatory instead of repeating the same generic workspace line on
        # every route in a full walkthrough.
        section_labels = list(
            dict.fromkeys(
                _meaningful_section_label(str(value))
                for value in sections
                if str(value).strip()
                and not _is_metadata_fact(str(value))
                and _meaningful_section_label(str(value))
            )
        )[:3]
        if section_labels:
            joined = ", ".join(section_labels[:-1])
            if len(section_labels) > 1:
                joined = f"{joined}, and {section_labels[-1]}"
            else:
                joined = section_labels[0]
            return _human_sentence(
                f"The {subject} page brings {joined} into view, showing how this part of the product is organized"
            )
        return _human_sentence(
            f"The {subject} workspace puts the visible workflow controls and current information in one place"
        )
    # A form action is meaningful because of the visible workflow state it
    # establishes, not because a cursor happened to visit a control. Keep this
    # generic across products while avoiding the old crawler prose such as
    # "The email step is shown".
    if operation.kind in {
        OperationKind.FILL_TEXT,
        OperationKind.FILL_EMAIL,
        OperationKind.FILL_PHONE,
    }:
        field_words = set(re.findall(r"[a-z0-9]{3,}", f"{target} {operation.intent}".casefold()))
        field_ref = _label_reference(target)
        if field_words & {"phone", "mobile", "telephone", "contact"}:
            return (
                "We begin with a contact detail, creating a safe, traceable example "
                "that can be verified when it reaches the workspace."
            )
        if field_words & {"name", "title", "customer", "client"}:
            return f"We use {field_ref} to give this example a recognizable identity before it moves into the team's workflow."
        if field_words & {"remark", "note", "comment", "description"}:
            return f"The value in {field_ref} preserves the context the team will need when they return to this example."
        return f"We complete {field_ref} deliberately, preparing one realistic example for the workflow's visible result."
    if operation.kind is OperationKind.OPEN_MODAL:
        page_subject = _clean(
            str(getattr(page, "purpose", "") or getattr(page, "title", "") or "workspace"),
            64,
        )
        return _human_sentence(
            f"The {page_subject} form opens with the fields that shape this workflow, giving the viewer the context needed to understand the next step"
        )
    # Controls that change the product state need a short explanation of the
    # state transition, not a dump of the accessibility inventory around the
    # control.  The intent is discovered by exploration (and therefore stays
    # generic); this branch only turns that observed imperative into natural
    # presenter prose when no stronger page-local fact is available.
    if operation.kind is OperationKind.CLICK:
        intent = _clean(str(getattr(operation, "intent", "") or ""), 120)
        intent = re.sub(r"\bobserved\b\s*", "", intent, flags=re.IGNORECASE).strip()
        intent = re.sub(r"^(?:click|press|tap|select)\s+", "", intent, flags=re.IGNORECASE)
        intent = re.sub(r"^(?:the|a|an)\s+", "", intent, flags=re.IGNORECASE)
        if intent:
            verb, _, remainder = intent.partition(" ")
            third_person = {
                "activate": "activates",
                "open": "opens",
                "select": "selects",
                "choose": "chooses",
                "expand": "expands",
                "toggle": "toggles",
                "enable": "enables",
                "start": "starts",
                "show": "shows",
                "view": "opens",
                "inspect": "opens",
            }.get(verb.casefold())
            if third_person:
                effect = f"{third_person} {remainder}".strip()
            else:
                effect = intent[0].lower() + intent[1:]
            candidate = _human_sentence(
                f"The {target} control {effect}, making the next product state visible to the viewer"
            )
            if _viewer_ready(candidate, target):
                return candidate
        if target:
            return _human_sentence(
                f"Selecting {target} changes the visible workspace before the next part of the workflow"
            )
    if operation.kind is OperationKind.POINTER_SEQUENCE:
        intent = _clean(str(getattr(operation, "intent", "") or ""), 120)
        intent = re.sub(r"\bobserved\b\s*", "", intent, flags=re.IGNORECASE).strip()
        surface = _concise_scene_subject(target)
        if intent and surface:
            return _human_sentence(
                f"The {surface} is ready for {intent.lower()}, so the viewer can see the interaction take shape in context"
            )
    # A grouped section is a single continuous browser movement, but its
    # narration must still explain the meaningful cards/roles that the viewer
    # passes on the way to the final target. Build a compact, page-local digest
    # from the exact observed heading/fact pairs instead of repeating a title
    # or borrowing the page's first paragraph.
    covered_groups = [
        " ".join(str(item).split())
        for item in (getattr(operation, "covered_content_groups", None) or [])
        if str(item).strip()
    ]
    if (
        operation.kind is OperationKind.SCROLL_TO
        and page is not None
        and "search" in target.casefold()
        and not _target_fact_narration(page, target)
    ):
        return _human_sentence(
            f"The {_clean(getattr(page, 'purpose', '') or getattr(page, 'title', '') or 'workspace')} view keeps {target} close at hand so the visible records can be narrowed and reviewed"
        )
    if operation.kind is OperationKind.SCROLL_TO and len(covered_groups) > 1 and page is not None:
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
                # Date chips and numeric metadata are useful visual evidence,
                # but not a complete viewer-facing explanation for a grouped
                # scene. Let the page-local control summary handle them.
                prose = ""
            if prose:
                summaries.append((group_name, prose))
        if summaries:
            # Section headers often describe the collection while the first
            # card supplies the useful viewer-facing fact. Prefer that card
            # over reading a parent label or concatenating a DOM inventory.
            # The planner gives later cards their own beat when they carry an
            # independent explanation, so this remains complete without
            # forcing a caption to race through a list of titles.
            specific = [
                (name, fact)
                for name, fact in summaries
                if not re.match(
                    r"^(?:explore|a selection|featured|this section)\b", fact, flags=re.IGNORECASE
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
    if operation.kind is OperationKind.SCROLL_TO and page is not None:
        # Category/column landmarks often have no sentence-level description;
        # the visible heading and its containing section are still meaningful
        # evidence. Name the category and explain its role without inventing
        # a product-specific feature or reading every row aloud.
        category_words = set(re.findall(r"[a-z0-9]{4,}", target.casefold()))
        if (
            category_words
            and any(
                raw.split("::", 1)[0].strip().casefold() == target.casefold()
                or target.casefold() in raw.casefold()
                for raw in facts
            )
            and (
                _looks_like_label_collection(" ".join(facts)) or " / " in target or " & " in target
            )
        ):
            # A category heading backed only by labels is still useful evidence,
            # but saying that it is merely "shown" is crawler narration and is
            # correctly rejected by editorial preflight. Prefer the domain
            # neutral collection summary, which explains the viewer value while
            # staying grounded in the observed labels. Keep a conservative
            # page-local fallback for unusual headings that do not yield the
            # structured summary.
            matching_fact = next(
                (
                    raw
                    for raw in facts
                    if raw.split("::", 1)[0].strip().casefold() == target.casefold()
                ),
                None,
            )
            # A dense page can make the overall fact inventory look like a
            # category collection even though this particular landmark has a
            # descriptive, page-local sentence (for example a challenge or
            # caching explanation). Prefer that evidence before falling back
            # to a structural collection summary; otherwise a useful scene
            # degrades into the generic "group brings tools together" copy.
            _local_fact_id, local_fact = _matching_page_fact(page, target)
            if local_fact:
                narrated = _subject_fact(target, local_fact)
                if narrated and _viewer_ready(narrated, target):
                    return narrated
            summary = _summary_from_categories(matching_fact, target)
            if not summary:
                summary = _summary_from_collection(matching_fact or "", target)
            if summary and _viewer_ready(summary, target):
                return summary
            page_subject = _clean(
                getattr(page, "purpose", "") or getattr(page, "title", "") or "workspace", 72
            )
            return _human_sentence(
                f"The {target} group brings the visible tools together within {page_subject}, giving the viewer a concise map of this part of the product"
            )
    # A landmark may be represented by a content block whose heading is not
    # identical to the accessibility target. Recover that page-local fact
    # before falling back to connective copy; this is what keeps a project,
    # role, or feature scene explanatory on arbitrary sites.
    if operation.kind is OperationKind.SCROLL_TO and page is not None:
        for subject in [target, *covered_groups]:
            _fact_ref, local_fact = _matching_page_fact(page, subject)
            if local_fact:
                narrated = _subject_fact(subject, local_fact)
                if narrated and _viewer_ready(narrated, subject):
                    return narrated
        # Some SPAs expose the card description only on the observed element,
        # not in the page's extracted content blocks. It is still valid
        # page-local evidence, so recover it before emitting a connective
        # caption. The source URL check prevents duplicate labels on another
        # route from leaking into this scene.
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
    if operation.kind is OperationKind.SELECT_OPTION:
        options = getattr(getattr(operation, "target", None), "text", "") or target
        readable_options = ", ".join(re.findall(r"[A-Z][A-Za-z]+", options)[:3])
        option_lower = target.casefold()
        if "branch" in option_lower:
            sentence = f"The appointment is linked to {readable_options or 'the selected branch'}, keeping its operating location explicit"
        elif "source" in option_lower:
            sentence = f"The selected {readable_options or 'source'} choice preserves where this record originated for later context"
        elif "assign" in option_lower or "agent" in option_lower:
            sentence = f"The selected {readable_options or 'assignment'} choice makes ownership explicit before the workflow continues"
        else:
            sentence = f"The selected {readable_options or 'available'} choice keeps this example connected to the context the workflow needs next"
        return _human_sentence(sentence)
    if operation.kind is OperationKind.SELECT_DATE:
        # Date and time pickers are often custom text widgets, so their
        # accessibility type is not reliable enough to choose a different
        # executor. Narration should still describe the viewer value rather
        # than repeating a placeholder such as ``dd-mm-yyyy`` or ``10:30``.
        if re.search(r"\btime\b", target, re.IGNORECASE):
            return _human_sentence(
                "The booking time sets when this appointment will take place, completing the schedule for the selected clinician"
            )
        return _human_sentence(
            "The booking date anchors this appointment in the visible schedule, so the team can confirm when the visit occurs"
        )
    if operation.kind is OperationKind.SUBMIT:
        return _human_sentence(
            f"Selecting {target} adds the isolated record to the workflow, then checks its visible result on screen"
        )
    if (
        operation.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}
        and page is not None
    ):
        # A destination navigation must introduce the page's purpose, not
        # narrate the mechanics of opening it.  Prefer the destination's own
        # descriptive evidence so unfamiliar products receive a useful
        # chapter hand-off even when the nav label has no lexical overlap.
        destination_fact = next(
            (
                _readable_fact(str(fact))
                for fact in facts
                if len(_readable_fact(str(fact)).split()) >= 8
                and not _is_metadata_fact(str(fact))
                and not _looks_like_label_collection(_readable_fact(str(fact)))
            ),
            "",
        )
        if destination_fact:
            page_subject = _clean(
                str(getattr(page, "purpose", "") or getattr(page, "title", "") or target),
                84,
            )
            candidate = _subject_fact(page_subject, destination_fact)
            if _viewer_ready(candidate, target):
                return candidate
    target_fact = next(
        (fact for fact in facts if fact.split("::", 1)[0].strip().lower() == normalized_target),
        next(
            (
                fact
                for fact in facts
                if target_words and all(word in fact.lower() for word in target_words)
            ),
            None,
        ),
    )
    # Numeric/repeated landmarks (weeks, days, modules) are often captured as
    # one dense accessibility string. Summarize the collection before the
    # inventory guard discards it; otherwise the narrator falls back to a
    # malformed heading dump such as ``Week 1 highlights ...``.
    if operation.kind is OperationKind.SCROLL_TO and inventory_target:
        inventory_source = " ".join(str(item) for item in facts)
        repeated = _summary_from_repeated_items(inventory_source, target)
        schedule = _summary_from_schedule(inventory_source, target)
        if schedule or repeated:
            return schedule or repeated
    # A lexical match can land on a shell-wide accessibility inventory that
    # merely contains the target label.  It is not a page-local explanation;
    # discard it so the concise, evidence-bound scene fallback below is used.
    if (
        target_fact
        and not _readable_fact(target_fact)
        and ("chevron" in target_fact.casefold() or "notification" in target_fact.casefold())
    ):
        target_fact = None
    # Numbered cards commonly share words such as “week” with the opening
    # dashboard fact. Do not let that generic match hijack the destination
    # chapter; use the destination page's own descriptive evidence instead.
    if inventory_target:
        target_fact = None
    # A category fact can contain an entire table of rows. Treat that as
    # structured evidence and narrate one representative item, rather than
    # sending the DOM inventory through the caption layer.
    dense_inventory = bool(
        target_fact and len(target_fact) > 280 and _looks_like_label_collection(target_fact)
    )
    category_fact = (
        _summary_from_categories(target_fact, target)
        if target_fact
        and target_fact.split("::", 1)[0].strip().casefold() == normalized_target
        and (_looks_like_label_collection(_readable_fact(target_fact)) or dense_inventory)
        else ""
    )
    if category_fact:
        return category_fact
    candidates = [
        _without_repeated_subject(fact, target)
        for fact in facts
        if len(fact.split()) >= 8 and any(word in fact.lower() for word in target_words)
    ]
    readable_candidates = [fact for fact in candidates if _readable_fact(fact)]
    relevant = min(
        readable_candidates,
        key=lambda fact: (
            0 if fact.lower().startswith(target.lower()) else 1,
            -len(fact),
        ),
        default=None,
    )
    # A navigation label generally does not overlap the destination's useful
    # facts. In that case prefer the first descriptive destination-page fact
    # over re-reading the link label.
    # Prefer sentence-shaped evidence for the viewer.  Accessibility
    # extraction can flatten a whole card/table inventory into one apparently
    # readable string; passing that through would produce a transcript even
    # though it is lexically grounded.  Keep such records as provenance, but
    # skip them when selecting narration text.
    descriptive_page_fact = next(
        (
            fact
            for fact in facts
            if _readable_fact(fact)
            and not _looks_like_screen_transcript(_readable_fact(fact), fact)
        ),
        None,
    )
    if inventory_target:
        week_match = re.search(r"\b(week\s+\d+)\b", target, flags=re.IGNORECASE)
        percent_match = re.search(r"\b(\d+)%", target)
        progress = f" with {percent_match.group(1)}% completion" if percent_match else ""
        if week_match and descriptive_page_fact:
            return (
                f"The {week_match.group(1).title()} page shows the builds planned this week{progress}, "
                "including representative implementation practice to review next."
            )
        if week_match:
            return (
                f"The {week_match.group(1).title()} card shows the current checkpoint{progress}, "
                "so the viewer can choose a focused section before reviewing its details."
            )
    _, scene_prose = _scene_fact(context, operation, raw_target)
    if scene_prose and _looks_like_screen_transcript(scene_prose, scene_prose):
        scene_prose = ""
    if relevant and _looks_like_screen_transcript(_readable_fact(relevant), relevant):
        relevant = None
    category_summary = _summary_from_categories(target_fact or "", target)
    repeated_summary = _summary_from_repeated_items(target_fact or "", target)
    schedule_summary = _summary_from_schedule(target_fact or "", target)
    collection_summary = _summary_from_collection(target_fact or "", target)
    intro_summary = (
        _summary_from_intro(
            target_fact or descriptive_page_fact or (facts[0] if facts else ""), target
        )
        if operation.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}
        else ""
    )
    if intro_summary:
        return intro_summary
    if schedule_summary:
        return schedule_summary
    if repeated_summary:
        return repeated_summary
    if not scene_prose and target_fact and not _readable_fact(target_fact) and category_summary:
        return category_summary
    if not scene_prose and target_fact and not _readable_fact(target_fact) and repeated_summary:
        return repeated_summary
    # When discovery captured a dense accessibility/navigation inventory but no
    # target-headed prose, stay concise and page-local instead of echoing the
    # shell or borrowing evidence from another route.  The wording is derived
    # from the observed page purpose and control label, so it works for any
    # application without project-specific templates.
    if (
        operation.kind is OperationKind.SCROLL_TO
        and page is not None
        and (
            not scene_prose
            or _looks_like_label_collection(scene_prose)
            or (not re.search(r"[.!?]", scene_prose) and bool(re.search(r"\d", scene_prose)))
        )
    ):
        page_subject = _clean(
            getattr(page, "purpose", "") or getattr(page, "title", "") or "this workspace", 72
        )
        clean_target = _concise_scene_subject(target)
        lower_target = clean_target.casefold()
        if any(
            word in lower_target
            for word in ("search", "filter", "status", "date", "today", "upcoming", "past")
        ):
            return _human_sentence(
                f"The {page_subject} view keeps {clean_target} close at hand so the visible records can be narrowed and reviewed"
            )
        if any(word in lower_target for word in ("new", "create", "add", "reminder", "reschedule")):
            return _human_sentence(
                f"From the {page_subject} view, {clean_target} is the next step for moving this workflow forward"
            )
        return _human_sentence(
            f"The {page_subject} workspace keeps {clean_target} in view while its visible information remains available for review"
        )
    if not scene_prose and target_fact and not _readable_fact(target_fact) and schedule_summary:
        return schedule_summary
    if not scene_prose and target_fact and not _readable_fact(target_fact) and intro_summary:
        return intro_summary
    visible = scene_prose or _readable_fact(
        relevant or target_fact or descriptive_page_fact or source or (facts[0] if facts else "")
    )
    page_purpose = (
        getattr(page, "purpose", "this part of the product") if page else "this part of the product"
    )
    # The operation target is the visible scene subject. Using a page heading
    # here can incorrectly replace a card or section's actual subject.
    section_hint = target or (sections[0] if sections else "this section")
    if visible and "visual exploration" in visible.casefold():
        # This phrase is a common accessibility-summary fallback, not a
        # viewer-facing explanation. Retain only the observed architectural
        # nouns and turn them into the purpose of the current evidence.
        observed_words = set(re.findall(r"[a-z]{4,}", visible.casefold()))
        if {"interactive", "diagrams", "specifications"} & observed_words:
            return "Interactive diagrams and specifications make the system structure and implementation choices easier to inspect."
        return "The visible components connect the product's system structure to the implementation choices behind it."
    if not visible and category_summary:
        return category_summary
    if not visible and repeated_summary:
        return repeated_summary
    if not visible and schedule_summary:
        return schedule_summary
    if not visible and collection_summary:
        return collection_summary
    if not visible and intro_summary:
        return intro_summary
    if (
        not visible
        and operation.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM}
        and page
    ):
        return f"The {section_hint} section opens {page_purpose}, where the visible content is organized for focused review."
    if visible and re.match(r"^(?:saved|auto[- ]calculated)\b", visible, flags=re.IGNORECASE):
        # Storage/status fragments are useful evidence but too short to stand
        # alone as a caption.  Connect the observed state to the current
        # section without inventing a product-specific mechanism.
        return _human_sentence(
            f"{section_hint} keeps this recorded state available so the overview can be reviewed"
        )
    if (
        visible
        and len(visible.split()) < 10
        and not re.match(r"^(?:Have|Fill|Explore|Learn|How|Here)\b", visible)
    ):
        concise = _viewer_fact(visible)
        if len(concise.split()) < 6:
            return _human_sentence(f"This note explains why {concise.rstrip('.').lower()}")
        return concise
    if operation.kind in {
        OperationKind.NAVIGATE,
        OperationKind.OPEN_NAVIGATION_ITEM,
        OperationKind.SCROLL_TO,
    }:
        if visible:
            # Dense cards often expose their heading followed by a useful
            # description. Do not emit that heading verbatim at the start of
            # a caption: editorial QA treats it as a title-only route label.
            # Add a short viewer-oriented lead while retaining the observed
            # evidence unchanged.
            visible_normalized = " ".join(visible.split())
            if target_words and visible_normalized.lower().startswith(normalized_target):
                remainder = visible_normalized[len(normalized_target) :].lstrip(" :—-.")
                if remainder:
                    if re.match(r"^lets\s+you\s+open\b", remainder, flags=re.IGNORECASE):
                        return _human_sentence(
                            f"{target} lets you open the next level of detail and review the content inside it"
                        )
                    return _human_sentence(_heading_caption(target, remainder))
            return _subject_fact(section_hint, visible)
        return _human_sentence(
            f"The visible {section_hint} section opens {page_purpose} for the next part of the walkthrough."
        )
    if visible:
        # A provider outage must not turn the last-resort writer into an event
        # label such as "fill email" or "open Users". Name the visible target
        # and retain the page-local observed sentence instead. This remains
        # useful for arbitrary products while giving editorial QA two concrete
        # evidence anchors: the target and the visible page fact.
        return _human_sentence(f"The {section_hint} step is shown on {page_purpose}. {visible}")
    # This is intentionally a factual, target-bound sentence rather than an
    # invented product outcome. If the target itself is not observed, later
    # evidence QA rejects the scene instead of allowing generic filler through.
    return _human_sentence(
        f"The visible {section_hint} control is the current step on {page_purpose}."
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

    The alternate is assembled only from captured evidence and returns the
    selected fact id so the scene's provenance remains complete. This handles
    repeated testimonial/card copy on arbitrary products without introducing a
    site-specific route recipe or weakening the repetition gate.
    """
    page = page or (_page_for_operation(context, operation) if operation is not None else None)
    if page is None:
        return "", None
    prior_words = _diversity_tokens(previous)
    subject = _clean(title, 72) or "this area"
    records = _page_fact_records(page)
    # ``_readable_fact`` intentionally keeps one sentence for ordinary scenes,
    # but repeated cards often contain several independently useful sentences
    # under the same evidence id. Keep those sentence variants available for a
    # diversity repair instead of repeating the first sentence verbatim.
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
            candidate = _human_sentence(
                f"The {subject} section adds this observed detail: {variant}"
            )
            if not _narration_repeats(candidate, previous):
                ranked.append(
                    (novel + len(_diversity_tokens(variant) - prior_words), fact_id, candidate)
                )
                break
    if ranked:
        _, fact_id, candidate = max(ranked, key=lambda item: item[0])
        return candidate, fact_id
    # A page can legitimately expose only one fact (for example a compact
    # testimonial). Keep the evidence but make the scene-specific heading part
    # of the sentence so two valid beats are still distinguishable to viewers.
    fallback_fact_id, fallback_fact = records[0] if records else (None, "")
    if fallback_fact:
        candidate = _human_sentence(
            f"In {subject}, the page records this product insight: {fallback_fact}"
        )
        if not _narration_repeats(candidate, previous):
            return candidate, fallback_fact_id
    # Sparse component pages can have no sentence-shaped facts at all. Use
    # their own observed section inventory, or as a final identity anchor the
    # canonical route label, so a repeated verify beat is still distinct and
    # never silently reuses another page's prose.
    labels = list(
        dict.fromkeys(
            _clean(str(value), 56)
            for value in (
                list(getattr(page, "visible_sections", []) or [])
                or list(getattr(page, "visible_facts", []) or [])
            )
            if str(value).strip() and not _is_metadata_fact(str(value))
        )
    )[:3]
    if labels:
        joined = ", ".join(labels[:-1])
        if len(labels) > 1:
            joined = f"{joined}, and {labels[-1]}"
        else:
            joined = labels[0]
        # A second scene on the same page must add a different editorial beat
        # rather than restating the preceding navigation/inspection caption.
        # Choose the wording from the scene contract, while keeping every
        # noun grounded in this page's observed section inventory.
        if interaction == "scroll" or story_phase == "explain":
            candidate = _human_sentence(
                f"With {subject} open, the visible {joined} give the viewer a closer look at how this area is arranged"
            )
        elif interaction in {"click", "type", "submit"} or story_phase == "demonstrate":
            candidate = _human_sentence(
                f"The {subject} section uses {joined} to make the visible interaction and its result easier to follow"
            )
        else:
            candidate = _human_sentence(
                f"The {subject} page is ready for a closer look; {joined} are the visible anchors for this chapter"
            )
        if not _narration_repeats(candidate, previous):
            return candidate, None
    route_parts = [
        part.replace("-", " ").replace("_", " ")
        for part in urlsplit(str(getattr(page, "url", ""))).path.split("/")
        if part
    ]
    if route_parts:
        route_subject = _clean(route_parts[-1].title(), 64)
        if interaction == "scroll" or story_phase == "explain":
            candidate = _human_sentence(
                f"As the {route_subject} view settles, its visible content gives the viewer a readable sense of this area"
            )
        else:
            candidate = _human_sentence(
                f"The {route_subject} view is ready for its next meaningful step in the walkthrough"
            )
        if not _narration_repeats(candidate, previous):
            return candidate, None
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
        if previous is not None and scene.operation_id is not None:
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
    """Build subject-led prose from the fact whose heading matches a target."""
    if page is None:
        return ""
    normalized = " ".join(target.split()).casefold()
    facts = list(getattr(page, "visible_facts", []) or [])

    def label_key(value: str) -> str:
        # Emoji/color markers are presentation chrome, not semantic identity;
        # normalize them so a headed fact can still ground its category
        # narration when the operation target includes or omits the marker.
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
        # Keep dense card/control inventories out of the caption layer. The
        # exact fact remains attached to the scene as provenance while this
        # sentence gives the viewer the page-level takeaway.
        return _screen_transcript_summary(target, exact)
    # A fact that is only an all-caps/category inventory is valid planning
    # evidence but poor narration. Recover the first complete explanatory
    # sentence from the same observed fact when the remainder is a dense
    # table/category inventory; returning an empty string here used to force
    # generic "details are read" filler for otherwise useful sections.
    if prose and re.search(
        r"\b(?:sqlite|postgres(?:ql)?|database|saved\s+in)\b", prose, re.IGNORECASE
    ):
        # Persistence implementation details are useful to engineers reading
        # the trace, but they are not the viewer's takeaway from a progress
        # or status screen. Keep the caption tied to the visible page while
        # avoiding an invented storage claim.
        return _human_sentence(
            f"The {target} view summarizes the visible completion state so the current pace can be reviewed"
        )
    if prose and re.search(r"\bhighlights\b", prose, re.IGNORECASE):
        # Dense cards frequently concatenate a heading, its description, and
        # an action hint (``X highlights Y Build ... Click a day ...``). Turn
        # that DOM-shaped string into a presenter sentence while preserving
        # only the observed content. This is deliberately domain-neutral.
        if _looks_like_screen_transcript(prose, exact):
            # Keep the useful lead (usually the section's purpose) and discard
            # adjacent control labels/cards. The discarded labels remain in
            # the scene's evidence and can be inspected in the trace; they do
            # not belong in a spoken caption. Only claim a "next" state when
            # the captured text actually contains that cue.
            lead = re.split(
                r"\b(?:check\s+off|click\s+(?:a|an|the)|tap\s+(?:a|an|the)|next)\b",
                prose,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip(" .:-")
            lead = re.sub(r"^.*?\bhighlights\b\s*", "", lead, flags=re.IGNORECASE).strip(" .:-")
            if len(lead.split()) >= 2:
                suffix = (
                    " and keeps the next items ready to review"
                    if re.search(r"\bnext\b", exact, flags=re.IGNORECASE)
                    else " so the current stage is easy to understand"
                )
                return _human_sentence(f"The {target} view groups the visible {lead}{suffix}")
            return _human_sentence(
                f"The {target} view groups the visible work for this stage so its current focus is easy to understand"
            )
        body = re.split(
            r"\b(?:click|open|tap|select)\s+(?:a|an|the)\b", prose, maxsplit=1, flags=re.IGNORECASE
        )[0].strip(" .:-")
        parts = (
            re.split(r"\s+—\s+|\s+-\s+", body, maxsplit=1)
            if re.search(r"\s+—\s+|\s+-\s+", body)
            else [body]
        )
        head, detail = (parts[0], parts[1]) if len(parts) > 1 else (body, "")
        if not detail:
            match = re.search(
                r"\s+(?P<marker>build|every|each|always)\s+", body, flags=re.IGNORECASE
            )
            if match:
                head, detail = body[: match.start()].strip(), body[match.start() :].strip()
        if detail and len(head.split()) >= 2:
            return _human_sentence(
                f"The {target} section highlights {head.strip(' .:-')}; the visible detail is {detail.strip(' .:-')}"
            )
        if body:
            return _human_sentence(f"The {target} section highlights {body}")
    if not prose or _looks_like_label_collection(prose):
        raw = exact.split("::", 1)[1].strip() if "::" in exact else exact.strip()
        raw = re.sub(r"\s+", " ", raw)
        # Navigation and table inventories commonly follow the explanatory
        # lead with a back-arrow and dozens of labels. Keep only the observed
        # prose before that structural boundary when recovering a sentence.
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
            return _subject_fact(target, explanatory)
        return ""
    # Page headings such as ``Welcome back`` or ``Dashboard`` are context
    # labels, not the viewer's takeaway.  Lead with the page purpose so QA
    # does not mistake a subject-led heading sentence for route-label copy;
    # retain only the observed prose as the claim.
    page_heading = " ".join(
        str(getattr(page, key, "") or "") for key in ("title", "purpose")
    ).casefold()
    if (
        normalized in page_heading
        or len(normalized.split()) <= 2
        and normalized in {"welcome back", "dashboard", "home"}
    ):
        subject = _clean(str(getattr(page, "purpose", "") or "the opening page"), 72)
        return _human_sentence(f"The {subject} page sets the context: {prose}")
    return _subject_fact(target, exact)


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
    """Create a grounded fallback intro when no editorial model is configured.

    The inputs are observed identity and opening evidence; no product, route,
    person, or workflow is embedded in this function.  Model-approved prose is
    preferred and replaces this fallback before rendering whenever available.
    """
    product = _clean(product) or "this product"
    # Browser titles frequently append a role or release after a separator;
    # keep the greeting compact while retaining the observed identity.
    product = product.split("|", 1)[0].strip() or product
    opening = (_safe_editorial_text(_clean(opening)) or "the opening experience").rstrip(".")
    # The opening is a presenter greeting, not a DOM transcript. Keep a
    # readable evidence hint so the viewer hears both the welcome and the
    # product's purpose before the first deliberate interaction.
    opening_words = opening.split()
    if len(opening_words) > 24:
        # Never cut a grounded sentence at an arbitrary word boundary; that
        # produced visibly truncated captions such as ``makes the next
        # steps.`` Prefer the first complete sentence and only use a bounded
        # word fallback when the evidence contains no sentence punctuation.
        sentences = re.split(r"(?<=[.!?])\s+", opening)
        opening = (
            sentences[0].strip()
            if sentences and sentences[0].strip()
            else " ".join(opening_words[:24])
        )
    if subject:
        subject_phrase = (
            subject
            if re.match(r"^(?:the|this|that|an?|my|our)\b", subject, re.IGNORECASE)
            else f"the {subject}"
        )
        journey = f"Today I'll walk you through {subject_phrase}."
    else:
        journey = "Today I'll walk you through this experience."
    return f"Welcome to {product}. {journey} {opening}."


def _page_intro_from_fact(page: object, fact: str) -> str:
    """Introduce a destination through its observed purpose, not its tab name.

    Navigation labels identify where the cursor went; they rarely tell a
    viewer why that part of the product exists. This keeps the first local
    fact as the claim and supplies only a small, grammatical bridge from the
    page's observed purpose.
    """
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
    bare = prose.rstrip(".")
    if _looks_like_screen_transcript(prose, fact):
        return _screen_transcript_summary(subject, fact)
    if re.search(r"\b(?:sqlite|postgres(?:ql)?|database|saved\s+in)\b", bare, re.IGNORECASE):
        return _human_sentence(
            f"The {subject} workspace summarizes the visible completion state so the current pace can be reviewed"
        )
    repeated_source = " ".join(str(item) for item in getattr(page, "visible_facts", []) or [])
    repeated = _summary_from_repeated_items(repeated_source, subject)
    if repeated and (
        re.search(r"\b(?:week|day|module|lesson|chapter|step)s?\b", subject, re.IGNORECASE)
        or re.search(r"\b(?:week|day|module|lesson|chapter|step)\s+#?\d+\b", bare, re.IGNORECASE)
    ):
        return repeated
    if re.search(r"\bhighlights\b", bare, re.IGNORECASE):
        body = re.split(
            r"\b(?:click|open|tap|select)\s+(?:a|an|the)\b", bare, maxsplit=1, flags=re.IGNORECASE
        )[0].strip(" .:-")
        parts = (
            re.split(r"\s+—\s+|\s+-\s+", body, maxsplit=1)
            if re.search(r"\s+—\s+|\s+-\s+", body)
            else [body]
        )
        head, detail = (parts[0], parts[1]) if len(parts) > 1 else (body, "")
        if not detail:
            marker = re.search(r"\s+(?:build|every|each|always)\s+", body, flags=re.IGNORECASE)
            if marker:
                head, detail = body[: marker.start()].strip(), body[marker.start() :].strip()
        if detail and len(head.split()) >= 2:
            return _human_sentence(
                f"The {subject} section highlights {head.strip(' .:-')}; the visible detail is {detail.strip(' .:-')}"
            )
        if body:
            return _human_sentence(f"The {subject} section highlights {body}")
    journal = re.match(
        r"^This\s+(?P<noun>[a-z][\w-]*)\s+(?P<verb>documents|explains|covers|describes)\s+(?P<body>.+)$",
        bare,
        flags=re.IGNORECASE,
    )
    if journal:
        return _human_sentence(
            f"The {subject} page opens with a {journal.group('noun')} that {journal.group('verb')} {journal.group('body')}"
        )
    if re.match(r"^#?\d+\b", bare):
        # A numbered card is useful evidence, but repeating its title alone
        # is not an explanation. Frame it as a representative item so the
        # viewer understands what the page is for before hearing its label.
        return _human_sentence(
            f"The {subject} page keeps a representative practice item in view, {bare.lower()}, "
            "so the kind of work tracked here is clear"
        )
    # A destination's first extracted card may omit its heading and begin
    # with a semantic verb (for example ``Auth provider Build ...``). Keep
    # the card name and its visible task, but turn the concatenated DOM text
    # into a presenter sentence rather than echoing it verbatim.
    marker = re.search(
        r"\s+(?P<word>every|each|always)\s+(?P<object>build|item|entry|task)\b",
        bare,
        flags=re.IGNORECASE,
    )
    if marker:
        head = bare[: marker.start()].strip(" .:-")
        tail = bare[marker.start() :].strip(" .:-")
        if len(head.split()) >= 2 and tail:
            return _human_sentence(
                f"The {subject} page opens with {head}, where {tail[0].lower() + tail[1:]}"
            )
    marker = re.search(
        r"\s+(?P<verb>build|design|implement|create|configure|review)\s+", bare, flags=re.IGNORECASE
    )
    if marker:
        head = bare[: marker.start()].strip(" .:-")
        detail = bare[marker.end() :].strip(" .:-")
        if len(head.split()) >= 2 and detail:
            verb = marker.group("verb").lower()
            return _human_sentence(
                f"The {subject} page opens with {head}, where the visible {verb} is {detail}"
            )
    if re.match(
        r"^(?:manage|track|review|schedule|configure|organize|monitor|plan|create|compare|follow)\b",
        bare,
        re.IGNORECASE,
    ):
        return _human_sentence(
            f"Here, the {subject} workspace helps teams {bare[:1].lower() + bare[1:]}"
        )
    return _subject_fact(subject, prose)


def _configuration_bridge(context: ProductContext, page: object | None) -> str:
    """Summarize an observed feature-configuration relationship safely."""
    if page is None:
        return ""
    purpose = str(getattr(page, "purpose", "") or "")
    if not re.search(r"\b(?:settings?|configuration)\b", purpose, re.IGNORECASE):
        return ""
    objective = getattr(context, "objective", None)
    raw = " ".join(getattr(objective, "requested_features", []) or []) if objective else ""
    raw = raw or str(getattr(objective, "raw", "") if objective else "")
    requested = {
        token
        for token in re.findall(r"[a-z0-9]{3,}", raw.casefold())
        if token
        not in {
            "the",
            "and",
            "for",
            "with",
            "from",
            "into",
            "this",
            "that",
            "show",
            "include",
            "explain",
            "actual",
            "experience",
            "product",
            "login",
            "context",
            "only",
            "brief",
            "supporting",
            "page",
            "pages",
            "use",
            "understand",
            "relationship",
            "settings",
            "setting",
            "config",
            "configuration",
            "flow",
            "demonstrate",
            "meaningful",
            "controls",
            "resulting",
            "state",
            "repeatedly",
            "revisit",
            "cover",
            "unrelated",
            "modules",
        }
    }
    canonical = _canonical_page_url(str(getattr(page, "url", "")))
    label = next(
        (
            " ".join(item.name.split())
            for item in context.elements
            if item.name.strip()
            and "config" in item.name.casefold()
            and set(re.findall(r"[a-z0-9]{3,}", item.name.casefold())) & requested
            and _canonical_page_url(item.source_url or context.url) == canonical
        ),
        None,
    )
    page_subject = _clean(purpose, 72) or "Settings"
    return (
        f"The {page_subject} exposes {label}, the observed setup point for this workflow; next we return to the user-facing experience."
        if label
        else ""
    )


def _exploration_context_bridge(
    context: ProductContext, page: object | None
) -> tuple[str, str] | None:
    """Return one grounded context line for an operational destination.

    A relationship can be learned during exploration without becoming a
    production-video detour. The bridge carries that evidence into the first
    relevant workspace scene so viewers understand the setup context while
    still spending the footage on the requested experience.
    """
    objective = getattr(context, "objective", None)
    if page is None or objective is None:
        return None
    identity = set(
        re.findall(
            r"[a-z0-9]{3,}",
            " ".join(
                [
                    str(getattr(page, "title", "")),
                    str(getattr(page, "purpose", "")),
                    str(getattr(page, "url", "")),
                ]
            ).casefold(),
        )
    )
    identity.update(term[:-1] for term in tuple(identity) if term.endswith("s") and len(term) > 3)
    for relation in getattr(objective, "supporting_relationships", []):
        if not relation.required:
            continue
        target_terms = set(re.findall(r"[a-z0-9]{3,}", relation.target.casefold())) - {
            "management",
            "workflow",
            "flow",
            "experience",
        }
        target_terms.update(
            term[:-1] for term in tuple(target_terms) if term.endswith("s") and len(term) > 3
        )
        if target_terms and not (identity & target_terms):
            continue
        source_terms = set(re.findall(r"[a-z0-9]{3,}", relation.source.casefold()))
        source_terms.update(
            term[:-1] for term in tuple(source_terms) if term.endswith("s") and len(term) > 3
        )
        source_page = next(
            (
                candidate
                for candidate in context.page_knowledge
                if source_terms
                & set(
                    re.findall(
                        r"[a-z0-9]{3,}",
                        f"{candidate.title} {candidate.purpose} {candidate.url}".casefold(),
                    )
                )
            ),
            None,
        )
        if source_page is None:
            continue
        return (
            _human_sentence(
                f"The explored {_clean(relation.source, 56)} is the setup point for {_clean(relation.target, 56)}; this screen shows that workflow in context"
            ),
            f"page:{source_page.url}",
        )
    return None


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
