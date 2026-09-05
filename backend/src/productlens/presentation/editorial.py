"""Evidence-grounded editorial direction for human-quality walkthroughs."""

from __future__ import annotations

import json
import re
from hashlib import sha256

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
from productlens.providers.errors import ProviderError


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


def _concise_title(value: str) -> str:
    """Create a compact visible title from observed product identity."""
    value = _clean(value.replace("|", " ").replace("—", " ").replace("–", " "), 72)
    words = value.split()
    if not words:
        return "Product walkthrough"
    return " ".join(words[:6])


def _element_text(context: ProductContext, name: str) -> str:
    item = next((item for item in context.elements if item.name.strip().lower() == name.strip().lower()), None)
    return _clean((item.text if item else "") or "")


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
    expected = next((str(item.expected) for item in operation.postconditions if item.kind == "url"), None)
    if expected:
        destination = next(
            (page for page in context.page_knowledge if page.url.rstrip("/") == expected.rstrip("/")),
            None,
        )
        if destination is not None:
            return destination
    target_source = getattr(getattr(operation, "target", None), "source_url", None)
    if target_source:
        owned_page = next(
            (page for page in context.page_knowledge if page.url.rstrip("/") == target_source.rstrip("/")),
            None,
        )
        if owned_page is not None:
            return owned_page
    source = next(
        (
            item.source_url for item in context.elements
            if operation.target and item.name.strip().lower() == operation.target.name.strip().lower() and item.source_url
        ),
        None,
    )
    if source:
        return next((page for page in context.page_knowledge if page.url.rstrip("/") == source.rstrip("/")), None)
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
            raw_fact for raw_fact in getattr(page, "visible_facts", []) if page is not None
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
            ((0 if starts_with_target else 1, -overlap, -len(prose)), (_fact_id(page.url, raw_fact), prose))
        )
    if matching:
        return min(matching, key=lambda item: item[0])[1]
    # A local card/section with no readable matching prose must not borrow the
    # page's first fact. The caller can derive a constrained category summary
    # from the target's own observed labels instead.
    return (None, "")


def _sentence(value: str) -> str:
    # A caption-led scene needs one readable thought, not a DOM dump. The
    # structured editorial pass may improve this prose; this fallback remains
    # concise enough to be a valid on-screen sentence on its own.
    value = _clean(value, 200)
    if not value:
        return ""
    value = value.rstrip(".")
    if not value:
        return ""
    return value if value.endswith(("!", "?")) else f"{value}."


def _human_sentence(value: str) -> str:
    """Return safe, complete viewer-facing prose from a visible fact."""
    text = _sentence(value)
    if not text:
        return ""
    # Evidence frequently starts after a card heading with a lower-case article.
    # Captions should read as a sentence without changing the observed claim.
    return text[:1].upper() + text[1:]


def _viewer_fact(value: str) -> str:
    """Turn a page-local fact into natural caption copy without route labels.

    A fallback is still an editorial writer.  Prefixing every grounded fact
    with ``The X section is now visible`` technically names the current DOM
    target, but produces the crawler-like narration this layer exists to
    prevent.  Prefer the observed sentence itself; only repair the common
    participle form found on experience cards.
    """
    visible = _human_sentence(value)
    if not visible:
        return ""
    if re.match(r"^Architecting\b", visible, flags=re.IGNORECASE):
        return _human_sentence(f"The role focuses on {visible[0].lower() + visible[1:]}")
    return visible


def _grounded_scene_fallback(target: str, evidence: str) -> str:
    """Create a readable sentence from the current scene's evidence only.

    This is used when a captured landmark has no clean fact sentence.  It is
    intentionally conservative: it keeps the observed wording, trims dense
    inventories, and never borrows another page's copy or invents a purpose.
    """
    source = _clean(evidence, 220)
    if not source:
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


def _summary_from_categories(value: str, subject: str) -> str:
    """Summarise an observed label collection without reading the DOM aloud."""
    labels = re.findall(r"\b[A-Z][A-Z& /-]{3,}\b", value)
    labels = [" ".join(label.split()) for label in labels if len(label.split()) <= 5]
    unique = list(dict.fromkeys(label.title() for label in labels))[:2]
    if len(unique) >= 2:
        return f"Visible architecture cards connect {unique[0]} with {unique[1]} alongside related engineering tools."
    return ""


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
    count = len(set(numbers)) or len(matches)
    return f"The {subject} view organizes {count} {unit} stages and shows their current completion levels."


def _summary_from_schedule(value: str, subject: str) -> str:
    """Describe a timed task grid without transcribing every cell."""
    if len(re.findall(r"\b\d{1,2}:\d{2}\b", value)) < 2:
        return ""
    return f"The {subject} view organizes timed task blocks across the day, so the next study session is clear."


def _summary_from_collection(value: str, subject: str) -> str:
    """Give a domain-neutral purpose sentence for dense category inventories.

    An earlier fallback called every dense label collection "problem-solving
    patterns" because it was first developed for a study dashboard. That is a
    fabricated claim for a portfolio, CRM, or any other product. The summary
    must stay useful without importing a domain the captured evidence did not
    establish.
    """
    if not _looks_like_label_collection(value):
        return ""
    return f"The {subject} section brings the visible capabilities and supporting details together for focused review."


def _summary_from_intro(value: str, subject: str) -> str:
    """Condense a dashboard introduction plus checklist into one thought."""
    lowered = value.lower()
    if "daily plan" in lowered and ("check" in lowered or "finish" in lowered):
        return f"The {subject} view presents a simple daily plan with a checklist for the next study blocks."
    if "hands-on challenge bank" in lowered or "no-ai challenges" in lowered:
        return f"The {subject} view presents hands-on practice challenges designed to be completed without AI."
    if "always last in the day" in lowered and "links open" in lowered:
        return f"The {subject} view keeps problem-solving practice visible and links each item to its external detail."
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
    return (
        len(title_tokens) >= 7 and value.count(".") == 0 and value.count(",") == 0
    ) or len(camel_breaks) >= 4


def _viewer_ready(text: str, title: str = "") -> bool:
    """Reject grounded text that is still a title dump or clipped fragment."""
    normalized = " ".join(text.split())
    if not normalized or "..." in normalized or len(normalized.split()) > 34:
        return False
    if normalized[0].islower() or _looks_like_label_collection(normalized):
        return False
    title_words = re.findall(r"[a-z0-9]{3,}", title.lower())
    words = re.findall(r"[a-z0-9]{3,}", normalized.lower())
    return not (len(title_words) >= 2 and words[: len(title_words)] == title_words)


def _readable_fact(value: str) -> str:
    """Keep the first descriptive sentence, discarding card labels/metadata."""
    had_structured_label = "::" in value
    heading, body = (value.split("::", 1) if had_structured_label else ("", value))
    value = _strip_structured_heading(body, heading)
    value = _clean(value, 500)
    # A structured capture sometimes contains a second, abbreviated heading
    # (for example "Send a Message" after "Message").  Prefer the first
    # complete sentence beginning with actual explanatory content.
    explanatory_start = re.search(
        r"\b(?:I\s+(?:engineer|build|design|focus)|This\s+(?:journal|project|page|system|section)|"
        r"Have\s+(?:an|a)|(?:An?|The)\s+[a-z][\w-]*|Real-world\s+|Generates\s+|"
        r"Proxies\s+|Webhooks\s+|Fill\s+out\s+|Here\s+is\s+how|Learn\s+how|How\s+to\s+|"
        r"Explore\s+(?:a|an)\s+|Open\s+(?:a|an|the)\s+|Tap\s+(?:the|a|an)\s+|"
        r"Saved\s+|Auto[- ]calculated\s+|Tracks\s+|Shows\s+|Provides\s+|"
        r"Instead\s+of\s+|Architecting\s+)",
        value,
    )
    if explanatory_start:
        candidate = value[explanatory_start.start():]
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
        (sentence for sentence in sentences if len(sentence.split()) >= 8 and re.search(r"\b(is|are|was|were|build|built|design|designed|engineer|engineered|architecting|developed|improved|features|provides|shows|documents|explore|focus|have|learn|will)\b", sentence, re.IGNORECASE)),
        None,
    )
    if descriptive:
        return _human_sentence(descriptive)
    embedded = re.search(
        r"(?:^|[.!?]\s+)((?:An|A|This|Built|Designed|Engineered|I\s+(?:engineer|build|design|focus)|"
        r"Architecting|Developed|Improved|Integrating)\s+[A-Z]?[\s\S]{25,}?[.!?])",
        value,
        re.IGNORECASE,
    )
    if embedded:
        return _human_sentence(embedded.group(1))
    if not had_structured_label and 8 <= len(value.split()) <= 32:
        return _human_sentence(value)
    # Navigation grids and accessibility snapshots often contain repeated card
    # labels. They are evidence of what is on screen, but never prose to put
    # verbatim into a viewer-facing caption.
    # A collection of labels, dates, cards, or control text is useful evidence
    # for planning but must not be re-read as narration.  Returning no prose
    # lets the caller use a concise, truthful scene transition instead.
    return ""


def _observed_narration(context: ProductContext, operation, target: str) -> str:
    """Deterministic safe narration for when editorial generation is unavailable.

    It deliberately uses a visible fact, then says why this chapter matters. This
    means a provider outage cannot regress delivery to a route-label slideshow.
    """
    page = _page_for_operation(context, operation)
    source = _element_text(context, target)
    facts = list(getattr(page, "visible_facts", []) or [])
    sections = list(getattr(page, "visible_sections", []) or [])
    target_words = re.findall(r"[a-z0-9]{4,}", target.lower())
    normalized_target = " ".join(target.split()).lower()
    inventory_target = "\n" in target or bool(re.search(r"\b\d+\b", target))
    # A form action is meaningful because of the visible workflow state it
    # establishes, not because a cursor happened to visit a control. Keep this
    # generic across products while avoiding the old crawler prose such as
    # "The email step is shown".
    if operation.kind in {OperationKind.FILL_TEXT, OperationKind.FILL_EMAIL, OperationKind.FILL_PHONE}:
        subject = re.sub(r"^(?:enter|fill|add|type)\s+", "", operation.intent, flags=re.IGNORECASE).rstrip(".")
        return _human_sentence(
            f"The form records {subject or target}, providing the information required for the next verified step"
        )
    if operation.kind is OperationKind.SELECT_OPTION:
        options = getattr(getattr(operation, "target", None), "text", "") or target
        readable_options = ", ".join(re.findall(r"[A-Z][A-Za-z]+", options)[:3])
        return _human_sentence(
            f"The visible {readable_options or target} choices set the option before the workflow continues"
        )
    if operation.kind is OperationKind.SUBMIT:
        return _human_sentence(
            f"The {target} control completes the prepared form and reveals the verified workflow result"
        )
    target_fact = next(
        (fact for fact in facts if fact.split("::", 1)[0].strip().lower() == normalized_target),
        next(
            (fact for fact in facts if target_words and all(word in fact.lower() for word in target_words)),
            None,
        ),
    )
    # Numbered cards commonly share words such as “week” with the opening
    # dashboard fact. Do not let that generic match hijack the destination
    # chapter; use the destination page's own descriptive evidence instead.
    if inventory_target:
        target_fact = None
    candidates = [
        _without_repeated_subject(fact, target) for fact in facts
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
    descriptive_page_fact = next((fact for fact in facts if _readable_fact(fact)), None)
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
                f"The {week_match.group(1).title()} card shows the daily plan checkpoint{progress}, "
                "so the viewer can choose a focused week before reviewing its details."
            )
    _, scene_prose = _scene_fact(context, operation, target)
    category_summary = _summary_from_categories(target_fact or "", target)
    repeated_summary = _summary_from_repeated_items(target_fact or "", target)
    schedule_summary = _summary_from_schedule(target_fact or "", target)
    collection_summary = _summary_from_collection(target_fact or "", target)
    intro_summary = _summary_from_intro(
        target_fact or descriptive_page_fact or (facts[0] if facts else ""), target
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
    if not scene_prose and target_fact and not _readable_fact(target_fact) and schedule_summary:
        return schedule_summary
    if not scene_prose and target_fact and not _readable_fact(target_fact) and intro_summary:
        return intro_summary
    visible = scene_prose or _readable_fact(relevant or target_fact or descriptive_page_fact or source or (facts[0] if facts else ""))
    page_purpose = getattr(page, "purpose", "this part of the product") if page else "this part of the product"
    # The operation target is the visible scene subject. Using a page heading
    # here can incorrectly replace a card or section's actual subject.
    section_hint = target or (sections[0] if sections else "this section")
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
    if not visible and operation.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM} and page:
        return f"The {section_hint} section opens {page_purpose}, where the visible content is organized for focused review."
    if visible and len(visible.split()) < 10 and not re.match(
        r"^(?:Have|Fill|Explore|Learn|How|Here)\b", visible
    ):
        concise = _viewer_fact(visible)
        if len(concise.split()) < 6:
            return _human_sentence(f"This note explains why {concise.rstrip('.').lower()}")
        return concise
    if operation.kind in {OperationKind.NAVIGATE, OperationKind.OPEN_NAVIGATION_ITEM, OperationKind.SCROLL_TO}:
        if visible:
            # Dense cards often expose their heading followed by a useful
            # description. Do not emit that heading verbatim at the start of
            # a caption: editorial QA treats it as a title-only route label.
            # Add a short viewer-oriented lead while retaining the observed
            # evidence unchanged.
            visible_normalized = " ".join(visible.split())
            if target_words and visible_normalized.lower().startswith(normalized_target):
                remainder = visible_normalized[len(normalized_target):].lstrip(" :—-.")
                if remainder:
                    return _human_sentence(
                        f"This view explains {target}, including {remainder}"
                    )
            return _viewer_fact(visible)
        return _human_sentence(
            f"The visible {section_hint} section opens {page_purpose} for the next part of the walkthrough."
        )
    if visible:
        # A provider outage must not turn the last-resort writer into an event
        # label such as "fill email" or "open Users". Name the visible target
        # and retain the page-local observed sentence instead. This remains
        # useful for arbitrary products while giving editorial QA two concrete
        # evidence anchors: the target and the visible page fact.
        return _human_sentence(
            f"The {section_hint} step is shown on {page_purpose}. {visible}"
        )
    # This is intentionally a factual, target-bound sentence rather than an
    # invented product outcome. If the target itself is not observed, later
    # evidence QA rejects the scene instead of allowing generic filler through.
    return _human_sentence(
        f"The visible {section_hint} control is the current step on {page_purpose}."
    )


def _facts(context: ProductContext) -> list[EditorialFact]:
    facts: list[EditorialFact] = []
    for item in context.elements:
        text = _clean(item.text or "")
        if text and len(text) >= 18:
            facts.append(EditorialFact(text=text, evidence=[f"element:{item.name}", f"source:{item.source_url or context.url}"]))
    if context.visible_text:
        facts.insert(0, EditorialFact(text=_clean(context.visible_text), evidence=[f"page:{context.url}"]))
    for page in context.page_knowledge:
        for fact in page.visible_facts[:12]:
            prose = _readable_fact(fact)
            if prose:
                facts.append(EditorialFact(text=prose, evidence=[f"page:{page.url}", _fact_id(page.url, fact)]))
    return facts[:24]


def _editorial_evidence(context: ProductContext) -> str:
    """Serialize page-scoped facts for the writer without flattening their provenance."""
    pages = [
        {
            "url": page.url,
            "purpose": page.purpose,
            "sections": page.visible_sections[:12],
            "facts": [
                {"id": _fact_id(page.url, fact), "text": fact}
                for fact in page.visible_facts[:16]
            ],
        }
        for page in context.page_knowledge
    ]
    return json.dumps(
        {
            "opening": {"url": context.url, "title": context.title, "text": context.visible_text[:3000]},
            "pages": pages,
            "elements": [
                {"name": item.name, "text": item.text or "", "source_url": item.source_url or context.url}
                for item in context.elements[:80]
            ],
        },
        ensure_ascii=False,
    )


def build_editorial_storyboard(context: ProductContext, plan: DemoPlan) -> EditorialStoryboard:
    """Build a deterministic story from observed text and the validated plan.

    Model-created prose may improve this later, but this artifact is intentionally
    useful on its own: every statement is traceable to current discovery evidence.
    """
    opening_page = context.page_knowledge[0] if context.page_knowledge else None
    opening_fact = next(
        (prose for _, prose in _page_fact_records(opening_page)),
        None,
    )
    opening = (
        _summary_from_intro(opening_fact or context.visible_text, "opening")
        or _summary_from_schedule(opening_fact or context.visible_text, "opening")
        or opening_fact
        or _readable_fact(context.visible_text)
        or _clean(context.title)
        or context.title
    )
    purpose = _clean(context.title or "Product walkthrough")
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
            narration=f"Welcome to {purpose}. Today I’ll show how it works. {opening}",
            evidence=[f"page:{context.url}"],
            interaction="opening",
            required_dwell_seconds=5.0,
            completion_criteria=["initial product view is stable", "opening message is readable"],
        )
    ]
    for index, step in enumerate(plan.workflow_steps, start=1):
        operation = step.operation
        if index == 1 and operation.kind is OperationKind.NAVIGATE and (
            str(operation.value or "").rstrip("/") == context.url.rstrip("/")
        ):
            # The opening scene already establishes the initial route; do not
            # create a duplicate generic "next view" chapter for its load.
            continue
        target = operation.target.name if operation.target else "the next view"
        target_words = re.findall(r"[a-z0-9]{4,}", target.lower())
        normalized_target = " ".join(target.split()).lower()
        observed = _observed_narration(context, operation, target)
        if operation.kind is OperationKind.SCROLL_TO:
            narration = observed
            interaction, dwell = "scroll", 5.0
        elif operation.kind in {OperationKind.OPEN_NAVIGATION_ITEM, OperationKind.NAVIGATE}:
            narration = observed
            interaction, dwell = "navigate", 4.5
        elif operation.kind is OperationKind.CLICK:
            narration = observed
            interaction, dwell = "click", 4.0
        else:
            narration = observed
            interaction, dwell = "observe", 3.5
        # Keep heading-plus-description extractions from becoming crawler-like
        # captions. Lead with a viewer-oriented explanation whenever the
        # observed prose starts with the exact scene title.
        narration_normalized = " ".join(narration.split())
        if target_words and narration_normalized.lower().startswith(normalized_target):
            remainder = narration_normalized[len(normalized_target):].lstrip(" :—-.")
            if remainder:
                narration = _human_sentence(
                    f"This view explains {target}, including {remainder}"
                )
        page = _page_for_operation(context, operation)
        fact_id, cited_prose = _scene_fact(context, operation, target)
        scene_evidence = [f"operation:{operation.id}", f"element:{target}"]
        if page is not None:
            scene_evidence.append(f"page:{page.url}")
        if fact_id:
            scene_evidence.append(fact_id)
        provisional = EditorialScene(
            id=f"scene-{index}", operation_id=operation.id, title=_clean(target, 120),
            purpose=_clean(operation.intent), narration=narration,
            evidence=scene_evidence, interaction=interaction,
            required_dwell_seconds=dwell,
            completion_criteria=["target state is visible", "caption evidence is readable"],
        )
        # The fallback writer can find a useful nearby sentence on a dense
        # page. Never retain it when it is not also in this scene's immutable
        # evidence: the result sounds polished but tells the viewer about the
        # next card while the current card is on screen.
        evidence_words = set(re.findall(r"[a-z0-9]{4,}", _scene_source(context, provisional).lower()))
        narration_words = set(re.findall(r"[a-z0-9]{4,}", narration.lower()))
        if len(evidence_words) >= 3 and len(narration_words & evidence_words) < 2:
            narration = _viewer_fact(cited_prose)
            if len(narration.split()) < 10:
                narration = _grounded_scene_fallback(target, _scene_source(context, provisional))
            normalized_fallback = " ".join(narration.split())
            if target_words and normalized_fallback.lower().startswith(normalized_target):
                remainder = normalized_fallback[len(normalized_target):].lstrip(" :—-.")
                if remainder:
                    narration = _human_sentence(
                        f"This view explains {target}, including {remainder}"
                    )
            provisional = provisional.model_copy(update={"narration": narration})
        # Apply the same guard after evidence recovery, which may replace the
        # initial narration with a cited heading-plus-description fragment.
        normalized_final = " ".join(provisional.narration.split())
        if target_words and normalized_final.lower().startswith(normalized_target):
            remainder = normalized_final[len(normalized_target):].lstrip(" :—-.")
            if remainder:
                provisional = provisional.model_copy(update={
                    "narration": _human_sentence(
                        f"This view explains {target}, including {remainder}"
                    )
                })
        scenes.append(provisional)
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
    chapter_dwell = min(4.75, max(2.25, reading_budget / len(scenes)))
    scenes = [
        scene.model_copy(update={
            "required_dwell_seconds": chapter_dwell if scene.operation_id else max(scene.required_dwell_seconds, chapter_dwell)
        })
        for scene in scenes
    ]
    scene_floor = int(sum(scene.required_dwell_seconds for scene in scenes))
    return EditorialStoryboard(brief=brief, scenes=scenes, minimum_duration_seconds=max(45, scene_floor))


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
)


def _supported(text: str, source: str) -> bool:
    """Require a meaningful phrase overlap before accepting model prose."""
    words = set(re.findall(r"[a-z0-9]{4,}", text.lower()))
    evidence = set(re.findall(r"[a-z0-9]{4,}", source.lower()))
    if any(pattern in text.lower() for pattern in _GENERIC_EDITORIAL_PATTERNS):
        return False
    # Accessibility snapshots frequently contain a numbered/card inventory.
    # A writer that simply copies it has produced a transcript, not an
    # explanation. Allow an occasional metric, but reject inventory-shaped
    # prose before it can replace the deterministic editorial fallback.
    if len(re.findall(r"\b\d+\b", text)) >= 3:
        return False
    if len(re.findall(r"\b[A-Z]{2,}\b", text)) >= 5:
        return False
    # Two shared words permits a model to smuggle a fabricated claim into a
    # scene. Requiring three content words makes the sentence recognisably
    # anchored in the specific card/page evidence it is allowed to describe.
    return len(words) >= 6 and len(words & evidence) >= 3


async def enrich_editorial_brief(context: ProductContext, storyboard: EditorialStoryboard, provider: object) -> EditorialStoryboard:
    """Optional first OpenRouter pass, rejected unless grounded in visible evidence."""
    structured = getattr(provider, "structured", None)
    if structured is None:
        return storyboard
    source = _editorial_evidence(context)
    prompt = (
        "Extract a concise product-demo editorial brief from this observed website evidence. "
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
        *[_fact_id(page.url, fact) for page in context.page_knowledge for fact in page.visible_facts],
        *[f"element:{item.name}" for item in context.elements],
        *[f"source:{item.source_url or context.url}" for item in context.elements],
    }
    if any(not set(fact.evidence).issubset(allowed_evidence) or not _supported(fact.text, source) for fact in candidate.facts):
        return storyboard
    if (
        len(candidate.title.split()) > 7
        or len(candidate.title) > 64
        or len(candidate.opening_message.split()) > 36
        or not _readable_fact(candidate.opening_message)
        or not _supported(candidate.opening_message, source)
        or not _supported(candidate.product_purpose, source)
    ):
        return storyboard
    return storyboard.model_copy(update={"brief": candidate})


async def enrich_editorial_storyboard(context: ProductContext, storyboard: EditorialStoryboard, provider: object) -> EditorialStoryboard:
    """Optional second pass: turn the accepted brief into scene narration."""
    structured = getattr(provider, "structured", None)
    if structured is None:
        return storyboard
    source = _editorial_evidence(context)
    scene_evidence = {
        scene.id: {
            "allowed_evidence": scene.evidence,
            "observed_text": _scene_source(context, scene),
        }
        for scene in storyboard.scenes
    }
    prompt = (
        "Write a polished product-demo narration for the supplied immutable scenes. Return only {lines:[{id,narration}]}. "
        "Return exactly one line for every non-opening scene id; do not return a brief, timing, evidence, operation, title, or any other field. "
        "Use only visible evidence. Every line must be one or two complete sentences (at least 10 words): explain what is visible, why it matters, and what the viewer learns next. Never output a route label, project name, click instruction, or generic phrase by itself. "
        "Good: 'This project is described as a real-time collaboration tool, showing how the product supports end-to-end teamwork.' "
        "Bad: 'Project name.' Bad: 'The Projects section is now visible.' Bad: 'Open Timeline.' "
        "Every narration must share at least two meaningful words with ITS OWN scene evidence, never another page's evidence. "
        "Do not describe a fact from a different scene even if it appears elsewhere in the product. "
        f"Scenes: {json.dumps({key: value for key, value in scene_evidence.items() if key != 'opening'}, ensure_ascii=False)}\nEvidence: {source[:12000]}"
    )
    try:
        candidate = await structured(prompt, EditorialNarrationDraft)
    except (ProviderError, ValueError, TypeError, AssertionError, IndexError, KeyError):
        return storyboard
    original_by_id = {scene.id: scene for scene in storyboard.scenes if scene.operation_id is not None}
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
        return inventory_title and bool(words) and len(words & opening_words) / max(len(words), 1) >= 0.72

    # A structured model is free to reorder an array even when it preserves
    # scene ids. Validate each proposed line against its *own* immutable scene
    # id, rather than zipping array positions; position-based validation can
    # approve a grounded line and then attach it to another scene by id.
    if (
        set(original_by_id) != set(proposed_by_id)
        or any(
            not _supported(proposed_by_id[scene_id], _scene_source(context, original_scene))
            or not _viewer_ready(proposed_by_id[scene_id], original_scene.title)
            or repeats_opening(original_scene)
            for scene_id, original_scene in original_by_id.items()
        )
    ):
        return storyboard
    # The model is an editorial writer, not a workflow editor. Keep the
    # validated evidence, scene timing, action semantics, and completion
    # contract owned by ProductLens; accept only grounded narration prose.
    narration_by_id = proposed_by_id
    scenes = [
        scene.model_copy(update={"narration": narration_by_id[scene.id]})
        if scene.operation_id is not None
        else scene
        for scene in storyboard.scenes
    ]
    return storyboard.model_copy(update={"scenes": scenes})


def _scene_source(context: ProductContext, scene: EditorialScene) -> str:
    """Return only the observed text explicitly assigned to a scene."""
    allowed = set(scene.evidence)
    chunks: list[str] = []
    for page in context.page_knowledge:
        if f"page:{page.url}" not in allowed:
            continue
        chunks.extend(
            fact for fact in page.visible_facts
            if _fact_id(page.url, fact) in allowed
        )
        if not chunks:
            chunks.extend(page.visible_sections[:4])
    for element in context.elements:
        if f"element:{element.name}" in allowed:
            chunks.append(element.text or element.name)
    return " ".join(chunks)


def bind_storyboard_events(storyboard: EditorialStoryboard, trace_event_ids: set[str]) -> EditorialStoryboard:
    """Discard only planned scenes that never produced visible execution evidence."""
    scenes = [scene for scene in storyboard.scenes if scene.operation_id is None or scene.operation_id in trace_event_ids]
    return storyboard.model_copy(update={"scenes": scenes})


def editorial_script(storyboard: EditorialStoryboard, event_by_operation: dict[str, str]) -> list[dict[str, object]]:
    """Map proven operations to a presenter-led, evidence-backed narration.

    The opening storyboard scene has no browser operation, but a caption-only
    demo still needs its welcome while the opening page is on screen.  Attach
    that welcome to the first proved opening operation instead of discarding
    it during the scene-to-event conversion.
    """
    lines: list[dict[str, object]] = []
    opening = next((scene for scene in storyboard.scenes if scene.operation_id is None), None)
    first_bound = True
    for scene in storyboard.scenes:
        if scene.operation_id is None:
            continue
        event_id = event_by_operation.get(scene.operation_id)
        if event_id:
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
                intro = (
                    f"Welcome to {storyboard.brief.product_purpose}. "
                    f"We begin with {scene.title}, the first visible area in this story."
                )
                # The opening scene and first proved scene frequently share
                # the same hero evidence. Keep the welcome, but do not make a
                # silent video repeat its first sentence word-for-word.
                text = intro if scene.narration.casefold() in intro.casefold() else f"{intro} {scene.narration}"
                facts = [*opening.evidence, *facts]
                scene_id = opening.id
                first_bound = False
            lines.append({"event_id": event_id, "text": text, "facts": facts, "scene_id": scene_id})
    return lines
