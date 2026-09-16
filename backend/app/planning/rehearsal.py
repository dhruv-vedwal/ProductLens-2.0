"""Evidence rules for an authorised, non-recorded workflow rehearsal."""

from __future__ import annotations

import re

from app.contracts.models import ActionCapability, ObservedElement, ProductContext, Target

_SUCCESS_WORDS = {
    "success",
    "successful",
    "created",
    "saved",
    "added",
    "confirmed",
    "complete",
    "completed",
}
_NOISE_WORDS = {"close", "cancel", "save", "create", "submit", "back", "menu"}
_REQUEST_NOISE = {
    "about",
    "application",
    "complete",
    "context",
    "create",
    "demo",
    "detailed",
    "entire",
    "every",
    "flow",
    "full",
    "management",
    "minute",
    "overview",
    "product",
    "record",
    "show",
    "tour",
    "walkthrough",
    "with",
    "workflow",
}
_PAGE_NOISE = {
    "add",
    "create",
    "dashboard",
    "edit",
    "new",
    "page",
    "save",
    "settings",
    "the",
}


class CapabilitySelectionError(ValueError):
    """A discovered form is not grounded enough to mutate during rehearsal."""


def _words(value: str) -> set[str]:
    values = set(re.findall(r"[a-z0-9]{3,}", value.casefold()))
    values.update(value[:-1] for value in tuple(values) if value.endswith("s") and len(value) > 3)
    return values


def select_rehearsal_capability(
    context: ProductContext,
    capabilities: list[ActionCapability],
    *,
    require_verified_outcome: bool = False,
) -> ActionCapability:
    """Choose a form only when its source page matches the requested product area.

    Discovery intentionally probes several reversible forms.  Their order is
    an implementation detail, not a semantic decision: selecting the first
    form with a submit control can create an unrelated object.  Ranking is
    entirely evidence based and applies to any product.  Ambiguity is a safe
    precondition failure rather than permission to guess or mutate.
    """
    objective = context.objective
    # A parsed primary entity is deliberately narrower than the whole prose
    # request. Context pages (for example configuration) inform the story but
    # must not become accidental record-creation targets.
    requested_source = (
        objective.primary_entity
        if objective and objective.primary_entity
        else " ".join(
            [
                *(objective.must_show if objective else []),
                *(objective.requested_features if objective else []),
            ]
        )
    )
    requested = _words(requested_source) - _REQUEST_NOISE
    eligible = [
        capability
        for capability in capabilities
        if capability.kind == "form"
        and capability.safe_to_probe
        and capability.submit_target is not None
        and (
            not require_verified_outcome
            or (capability.verified and capability.outcome_target is not None)
        )
    ]
    # Older/public callers may explicitly authorise one isolated safe record
    # without naming its entity.  A unique fully observed form is then the
    # only non-guessing choice; multiple forms remain an ambiguity failure.
    if not requested:
        if len(eligible) == 1:
            return eligible[0]
        raise CapabilitySelectionError(
            "objective has no specific feature evidence for record creation"
        )

    page_by_url = {page.url.rstrip("/"): page for page in context.page_knowledge}
    ranked: list[tuple[int, ActionCapability, set[str], set[str]]] = []
    for capability in eligible:
        page = page_by_url.get(capability.source_url.rstrip("/"))
        page_evidence = " ".join(
            [
                page.title if page else "",
                page.purpose if page else "",
                *(page.visible_sections if page else []),
                capability.purpose,
            ]
        )
        source_terms = _words(page_evidence) - _PAGE_NOISE
        matched = requested & source_terms
        # A form on "Invoice templates" is not as relevant to an Invoice
        # walkthrough as one on "Invoices".  This generic penalty favours the
        # page whose visible purpose adds the least unrelated taxonomy.
        unrelated = source_terms - requested - _PAGE_NOISE
        if not matched:
            continue
        score = len(matched) * 10 - min(6, len(unrelated))
        ranked.append((score, capability, matched, unrelated))
    if not ranked:
        raise CapabilitySelectionError(
            "no safe submit-capable form is grounded in the requested feature"
        )
    ranked.sort(key=lambda entry: entry[0], reverse=True)
    best_score, best, _, _ = ranked[0]
    tied = [entry for entry in ranked if entry[0] == best_score]
    if len(tied) > 1:
        sources = ", ".join(sorted({entry[1].source_url for entry in tied}))
        raise CapabilitySelectionError(f"ambiguous form capability sources: {sources}")
    return best


def derive_outcome_witness(
    capability: ActionCapability,
    *,
    before_text: str,
    observed: list[ObservedElement],
    submitted_values: list[str] | None = None,
) -> ActionCapability | None:
    """Promote a capability only with a visible independent success witness.

    The caller supplies controls observed *after* an authorised submit. A
    changed button, a still-open modal, or pre-existing page prose cannot
    become proof. We accept an explicit success/confirmation signal, or newly
    visible entity-specific detail that is not an action control.
    """
    before = _words(before_text)
    purpose = _words(capability.purpose) - _NOISE_WORDS
    submitted_values = submitted_values or []

    def matching_submitted_value(phrase: str) -> str | None:
        """Match a generated record value despite UI-only punctuation changes."""
        compact_phrase = re.sub(r"[^a-z0-9]", "", phrase.casefold())
        for value in submitted_values:
            compact_value = re.sub(r"[^a-z0-9]", "", str(value).casefold())
            if len(compact_value) >= 5 and compact_value in compact_phrase:
                return str(value)
        return None

    ranked: list[tuple[int, ObservedElement, str]] = []
    for item in observed:
        phrase = " ".join(part for part in (item.name, item.text or "") if part).strip()
        terms = _words(phrase)
        if not phrase or terms <= before:
            continue
        action_like = item.tag in {"button", "input"} and not item.text
        if action_like:
            continue
        success = bool(terms & _SUCCESS_WORDS)
        # Input forms are never outcomes. A dialog can be the product's
        # explicit success confirmation (for example “Booking created — saved
        # successfully”), so allow only that positive witness; validation and
        # error dialogs remain excluded.
        if item.role == "form" or item.tag == "form":
            continue
        if (item.role == "dialog" or item.tag == "dialog") and not success:
            continue
        related = bool(terms & purpose) if purpose else False
        record_value = matching_submitted_value(phrase)
        structural_result = (
            item.role in {"row", "gridcell", "status", "alert", "listitem"}
            or item.tag in {"tr", "td", "li"}
            # Modern card layouts often expose no list/table role. A visible,
            # stable container that contains the unique generated value is an
            # independent product result; a generic div without that locator
            # remains insufficient evidence.
            or (record_value is not None and item.selector.startswith(("#", "[data-testid=")))
        )
        if not success and not (record_value and structural_result):
            continue
        score = (
            (4 if success else 0)
            + (3 if record_value and structural_result else 0)
            + (2 if related else 0)
            + min(2, len(terms - before))
        )
        ranked.append((score, item, phrase))
    if not ranked:
        return None
    _, item, phrase = max(ranked, key=lambda candidate: candidate[0])
    # A structural row proves the isolated generated record exists, but its
    # full text can include personal/contact columns from the application.
    # Keep the locator tied to the generated value while making every human
    # facing artifact and narration evidence neutral and safe.
    selected_record_value = matching_submitted_value(phrase)
    selected_structural_result = (
        item.role in {"row", "gridcell", "status", "alert", "listitem"}
        or item.tag in {"tr", "td", "li"}
        or (selected_record_value is not None and item.selector.startswith(("#", "[data-testid=")))
    )
    is_structural_record = bool(selected_record_value and selected_structural_result)
    selected_name = item.name
    selected_text = item.text or item.name
    # Keep a success-dialog locator concise and semantic. Persisting the full
    # dialog transcript as an accessible-name equality target is brittle: a
    # status line, button labels, or localization can change between the
    # rehearsal and the clean production context. The evidence still retains
    # the full phrase in ``outcome_evidence``; grounding uses the shortest
    # observed success-bearing prefix.
    if (item.role == "dialog" or item.tag == "dialog") and not is_structural_record:
        success_word = re.search(
            r"\b(?:success(?:fully|ful)?|created|saved|added|confirmed|complete(?:d)?)\b",
            phrase,
            flags=re.IGNORECASE,
        )
        if success_word:
            selected_name = phrase[: success_word.end()].strip()
            selected_text = selected_name
    target = Target(
        name="verified created record" if is_structural_record else selected_name,
        role=item.role,
        selector=item.selector if item.selector.startswith(("#", "[")) else None,
        text=selected_record_value if is_structural_record else selected_text[:300],
        source_url=item.source_url or capability.source_url,
    )
    return capability.model_copy(
        update={
            "outcome_target": target,
            "outcome_evidence": [
                *capability.outcome_evidence,
                "rehearsal-visible-outcome:verified-created-record"
                if is_structural_record
                else f"rehearsal-visible-outcome:{phrase[:300]}",
            ],
            "verified": True,
        }
    )
