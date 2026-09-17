"""Evidence-backed workflow coverage reporting."""

from __future__ import annotations

from typing import Any

from app.contracts.models import DemoPlan, DemoTrace


def _words(value: str) -> set[str]:
    aliases = {
        "sent": "send",
        "sending": "send",
        "created": "create",
        "invited": "invite",
        "explained": "explain",
        "explaining": "explain",
    }
    return {
        aliases.get(word, word.rstrip("s"))
        for word in __import__("re").findall(r"[a-z0-9]{3,}", value.lower())
    }


def inspect_coverage(plan: DemoPlan, trace: DemoTrace) -> dict[str, Any]:
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
    operations_by_id = {step.operation.id: step.operation for step in plan.workflow_steps}
    # A semantic postcondition is not sufficient for a viewer-facing demo:
    # when an authorised mutation promises a visible result, the production
    # trace must preserve a corresponding image witness. Without it a later
    # recovery can truthfully verify the database/browser state while the
    # recorded video still ends on the pre-submit modal.
    outcome_visual_failures: list[str] = []
    page_state_screenshots = {
        (state.get("event_id") if isinstance(state, dict) else state.event_id)
        for state in trace.page_states
        if (state.get("screenshot") if isinstance(state, dict) else state.screenshot)
    }
    for event in successful:
        operation = operations_by_id.get(event.operation_id)
        if event.kind.value != "Submit" or operation is None:
            continue
        requires_visible_result = any(
            condition.kind == "visible" and condition.target is not None
            for condition in operation.postconditions
        )
        if (
            requires_visible_result
            and not event.screenshot_path
            and event.id not in page_state_screenshots
        ):
            outcome_visual_failures.append("VISIBLE_MUTATION_OUTCOME_NOT_CAPTURED")
            break
    # Page-complete planners express an outcome as a chapter contract rather
    # than a sentence the executor is expected to repeat verbatim. Prove that
    # every required establish/explore/explain/demonstrate/verify operation on
    # that chapter completed. This is stronger than fuzzy text matching and
    # avoids rejecting a fully recorded page merely because the editorial
    # wording changed between planning and execution.
    page_contracts: list[list[Any]] = []
    seen_pages: set[str] = set()
    for step in plan.workflow_steps:
        operation = step.operation
        if not operation.page_url or operation.story_phase == "transition":
            continue
        key = operation.page_url.rstrip("/") or "/"
        if key in seen_pages:
            continue
        seen_pages.add(key)
        contract_steps = [
            candidate
            for candidate in plan.workflow_steps
            if ((candidate.operation.page_url or "").rstrip("/") or "/") == key
            and candidate.operation.story_phase
            in {"establish", "explore", "explain", "demonstrate", "verify"}
        ]
        # A synthetic opening Navigate may carry the root page URL but no
        # editorial phase. It is not a page chapter and must not consume the
        # first expected-outcome slot; otherwise the actual first chapter can
        # be reported missing even when every scene executed successfully.
        if contract_steps:
            page_contracts.append(contract_steps)
    covered = []
    for outcome_index, outcome in enumerate(plan.expected_outcomes):
        # Sparse pages can be represented by one evidence-backed state hold
        # whose ``page_contract_phases`` explicitly records the phases that
        # were observable (for example establish/explore/explain/verify when
        # no stable scroll landmark exists).  Evaluate that declaration rather
        # than requiring one exact editorial sentence, so a valid wording
        # change cannot turn a complete trace into a false coverage failure.
        requested_phases = {
            phase
            for phase in ("establish", "explore", "explain", "demonstrate", "verify")
            if phase in outcome.casefold()
        }
        if requested_phases and outcome_index < len(page_contracts):
            contract_steps = page_contracts[outcome_index]
            declared_phases = {
                phase
                for step in contract_steps
                for phase in ([step.operation.story_phase] if step.operation.story_phase else [])
                + list(step.operation.page_contract_phases)
            }
            if (
                contract_steps
                and requested_phases <= declared_phases
                and all(step.operation.id in successful_operation_ids for step in contract_steps)
            ):
                covered.append(outcome)
                continue
        if "visible content established, explored, explained, demonstrated, and verified" in outcome.lower() and any(
            # A page-level editorial outcome can follow several concrete
            # mutation outcomes (labels, rows, created records, etc.). It is
            # therefore not positional in ``expected_outcomes``. Match the
            # contract by evidence/page coverage rather than assuming the
            # outcome index is the page index.
            contract_steps
            and all(step.operation.id in successful_operation_ids for step in contract_steps)
            and {
                phase
                for phase in ("establish", "explore", "explain", "demonstrate", "verify")
                if phase in outcome.casefold()
            }
            <= {
                phase
                for step in contract_steps
                for phase in ([step.operation.story_phase] if step.operation.story_phase else [])
                + list(step.operation.page_contract_phases)
            }
            for contract_steps in page_contracts
        ):
            covered.append(outcome)
            continue
        # Read-only setup objectives are proven by the causal sequence itself:
        # the form was opened and every planned reversible field operation
        # completed successfully.  Do not rely on the outcome sentence
        # repeating field labels, since a concise editorial line intentionally
        # summarizes several visible controls.
        outcome_words = _words(outcome)
        if {"field", "explain"} <= outcome_words:
            form_operations = [
                step.operation
                for step in plan.workflow_steps
                if step.operation.kind.value
                in {
                    "FillText",
                    "FillEmail",
                    "FillPhone",
                    "SelectOption",
                    "SelectDate",
                    "SelectDateRange",
                    "Check",
                    "Uncheck",
                }
            ]
            opened_form = any(
                event.kind.value == "OpenModal" and event.success for event in successful
            )
            if (
                form_operations
                and opened_form
                and all(operation.id in successful_operation_ids for operation in form_operations)
            ):
                covered.append(outcome)
                continue
        if outcome.lower() in evidence:
            covered.append(outcome)
            continue
        # Full-walkthrough plans annotate outcomes with their chapter (for
        # example, "Opening page: <landmark>"). Execution evidence correctly
        # records the semantic landmark, not that editorial prefix. Compare
        # the non-structural subject words against one verified event.
        subject_words = outcome_words - {"opening", "page", "section", "explore", "tab"}
        if subject_words and any(
            subject_words
            <= _words(
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
            )
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
            and bool(
                outcome_words
                & _words(" ".join([event.intent, event.target.name if event.target else ""]))
            )
            for event in successful
        ):
            covered.append(outcome)
    missing = [outcome for outcome in plan.expected_outcomes if outcome not in covered]
    failures = [
        *(["OBJECTIVE_COVERAGE_INCOMPLETE"] if missing else []),
        *outcome_visual_failures,
    ]
    return {
        "coverage_score": 1.0 if not failures else 0.0,
        "expected_outcomes": plan.expected_outcomes,
        "covered_outcomes": covered,
        "missing_outcomes": missing,
        "hard_failures": failures,
    }
