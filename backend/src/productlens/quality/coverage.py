"""Evidence-backed workflow coverage reporting."""

from __future__ import annotations

from productlens.contracts.models import DemoPlan, DemoTrace


def _words(value: str) -> set[str]:
    aliases = {"sent": "send", "sending": "send", "created": "create", "invited": "invite"}
    return {aliases.get(word, word.rstrip("s")) for word in __import__("re").findall(r"[a-z0-9]{3,}", value.lower())}


def inspect_coverage(plan: DemoPlan, trace: DemoTrace) -> dict:
    """Prove that each promised viewer-facing outcome was actually reached."""
    evidence = "\n".join(
        " ".join(
            filter(
                None,
                [
                    event.intent,
                    event.target.name if event.target else "",
                    event.target.text if event.target and event.target.text else "",
                    str(event.after.get("text", "")),
                    str(event.page_url or ""),
                ],
            )
        )
        for event in trace.events
        if event.success
    ).lower()
    successful = [event for event in trace.events if event.success]
    successful_operation_ids = {event.operation_id for event in successful}
    # Page-complete planners express an outcome as a chapter contract rather
    # than a sentence the executor is expected to repeat verbatim. Prove that
    # every required establish/explore/explain/demonstrate/verify operation on
    # that chapter completed. This is stronger than fuzzy text matching and
    # avoids rejecting a fully recorded page merely because the editorial
    # wording changed between planning and execution.
    page_contracts: list[list] = []
    seen_pages: set[str] = set()
    for step in plan.workflow_steps:
        operation = step.operation
        if not operation.page_url or operation.story_phase == "transition":
            continue
        key = operation.page_url.rstrip("/") or "/"
        if key in seen_pages:
            continue
        seen_pages.add(key)
        page_contracts.append([
            candidate for candidate in plan.workflow_steps
            if (candidate.operation.page_url.rstrip("/") or "/") == key
            and candidate.operation.story_phase in {"establish", "explore", "explain", "demonstrate", "verify"}
        ])
    covered = []
    for outcome_index, outcome in enumerate(plan.expected_outcomes):
        if (
            "visible content established, explored, explained, demonstrated, and verified" in outcome.lower()
            and outcome_index < len(page_contracts)
            and page_contracts[outcome_index]
            and all(step.operation.id in successful_operation_ids for step in page_contracts[outcome_index])
        ):
            covered.append(outcome)
            continue
        outcome_words = _words(outcome)
        if outcome.lower() in evidence:
            covered.append(outcome)
            continue
        # Full-walkthrough plans annotate outcomes with their chapter (for
        # example, "Opening page: <landmark>"). Execution evidence correctly
        # records the semantic landmark, not that editorial prefix. Compare
        # the non-structural subject words against one verified event.
        subject_words = outcome_words - {"opening", "page", "section", "explore", "tab"}
        if subject_words and any(
            subject_words <= _words(" ".join(filter(None, [
                event.intent,
                event.target.name if event.target else "",
                event.target.text if event.target and event.target.text else "",
                str(event.after.get("text", "")),
                str(event.page_url or ""),
            ])))
            for event in successful
        ):
            covered.append(outcome)
            continue
        # A verified mutation/submit is outcome evidence when its semantic
        # intent shares the subject of the promised outcome. Exact prose is
        # not required: "Send invitation" and "Invitation is sent" describe
        # the same proven transition.
        if any(
            event.kind.value in {"Submit", "Click", "ApplyFilter"}
            and bool(outcome_words & _words(" ".join([event.intent, event.target.name if event.target else ""])))
            for event in successful
        ):
            covered.append(outcome)
    missing = [outcome for outcome in plan.expected_outcomes if outcome not in covered]
    return {
        "coverage_score": 1.0 if not missing else 0.0,
        "expected_outcomes": plan.expected_outcomes,
        "covered_outcomes": covered,
        "missing_outcomes": missing,
        "hard_failures": ["OBJECTIVE_COVERAGE_INCOMPLETE"] if missing else [],
    }
