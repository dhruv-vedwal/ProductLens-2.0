"""Production plan validation."""

from __future__ import annotations

from urllib.parse import urljoin

from pydantic import ValidationError

from app.contracts.models import (
    ActionCapability,
    OperationKind,
    ProductContext,
    Target,
    WorkflowProposal,
)
from app.planning.production.shared import *
from app.planning.side_effects import (
    SideEffectPolicyError,
    authorize_operation,
)


class ValidationMixin:
    @staticmethod
    def _validate(
        proposal: WorkflowProposal,
        context: ProductContext,
        allow_side_effects: bool,
    ) -> None:
        observed_selectors = {item.selector for item in [*context.elements, *context.navigation]}
        observed_names = {item.name.lower() for item in [*context.elements, *context.navigation]}
        capability_targets: set[tuple[str | None, str]] = set()
        for raw in context.capabilities:
            try:
                capability = ActionCapability.model_validate(raw)
            except ValidationError:
                continue
            for field in capability.form_schema.fields if capability.form_schema else []:
                capability_targets.add((capability.source_url, field.name.casefold()))
            for target in (capability.submit_target, capability.outcome_target):
                if target is not None:
                    capability_targets.add((target.source_url, target.name.casefold()))
        observed_routes = {
            _canonical_url(urljoin(item.source_url or context.url, item.href))
            for item in context.navigation
            if item.href
        }
        # A page-local element can be the only captured witness for a route
        # during an incremental discovery snapshot.  Its source URL is still
        # observed evidence and must be accepted when the compiler inserts the
        # required page entry before a local scroll.
        observed_routes.update(
            _canonical_url(item.source_url)
            for item in context.elements
            if item.source_url and item.source_url != context.url
        )
        observed_route_keys = {_route_key(route) for route in observed_routes}
        allowed_routes = {
            _canonical_url(value) for value in (context.url, *context.relevant_routes)
        } | observed_routes
        allowed_route_keys = {
            _route_key(value) for value in (context.url, *context.relevant_routes)
        } | observed_route_keys
        source_by_selector = {item.selector: item.source_url for item in context.elements}
        navigated_to: set[str] = {_canonical_url(context.url)}
        for operation in proposal.steps:
            if operation.kind in SIDE_EFFECTING:
                try:
                    authorize_operation(operation, allow_side_effects)
                except SideEffectPolicyError as error:
                    raise PlanningValidationError(str(error)) from error
            if operation.kind is OperationKind.NAVIGATE:
                destination = _canonical_url(urljoin(context.url, str(operation.value)))
                if (
                    destination not in allowed_routes
                    and _route_key(destination) not in allowed_route_keys
                ):
                    raise PlanningValidationError(
                        "Navigation target is not observed and same-origin"
                    )
            elif operation.target is None and operation.kind not in {
                OperationKind.READ_VALUE,
                OperationKind.WAIT_FOR_STATE,
                OperationKind.VERIFY_STATE,
                OperationKind.POINTER_SEQUENCE,
                OperationKind.KEY_PRESS,
            }:
                raise PlanningValidationError(f"Target is required for {operation.kind}")
            elif operation.kind is OperationKind.POINTER_SEQUENCE:
                points = (
                    operation.value.get("points") if isinstance(operation.value, dict) else None
                )
                relative_points = (
                    operation.value.get("relative_points")
                    if isinstance(operation.value, dict)
                    else None
                )
                pattern = (
                    operation.value.get("pattern") if isinstance(operation.value, dict) else None
                )
                # A generic surface gesture may derive a stroke from the
                # observed canvas bounds, but connector paths must always be
                # supplied as concrete points captured during exploration.
                # The former implementation accepted ``connector_segment``
                # with no points and let the executor draw a synthetic grid
                # path, producing false success on empty editors.
                generated_surface_pattern = (
                    pattern in {"short_reversible_stroke", "text_placement", "connector_segment"}
                    and operation.target is not None
                    and isinstance(relative_points, list)
                    and len(relative_points) >= 2
                ) or (pattern == "short_reversible_stroke" and operation.target is not None)
                if (
                    not isinstance(points, list) or len(points) < 2
                ) and not generated_surface_pattern:
                    raise PlanningValidationError(
                        "PointerSequence requires observed points or an evidence-backed surface pattern"
                    )
                if not operation.evidence_refs:
                    raise PlanningValidationError(
                        "PointerSequence requires evidence references for its observed path"
                    )
                if pattern == "connector_segment" and not isinstance(points, list) and not generated_surface_pattern:
                    raise PlanningValidationError(
                        "Connector paths require geometry observed during exploration"
                    )
            elif operation.kind is OperationKind.DRAG:
                payload = operation.value if isinstance(operation.value, dict) else None
                destination = payload.get("destination") if isinstance(payload, dict) else None
                if not isinstance(destination, dict):
                    raise PlanningValidationError("Drag requires an observed semantic destination")
                try:
                    destination_target = Target.model_validate(destination)
                except ValidationError as error:
                    raise PlanningValidationError(
                        "Drag destination must be a valid semantic target"
                    ) from error
                if not operation.evidence_refs:
                    raise PlanningValidationError(
                        "Drag requires evidence references for source and destination geometry"
                    )
                if not any(
                    destination_target.selector == item.selector
                    or destination_target.name.casefold() == item.name.casefold()
                    for item in [*context.elements, *context.navigation]
                ):
                    raise PlanningValidationError(
                        f"Drag destination is not grounded in current evidence: {destination_target.name}"
                    )
            elif operation.target is not None and (
                operation.target.selector not in observed_selectors
                and operation.target.name.lower() not in observed_names
                and (operation.target.source_url, operation.target.name.casefold())
                not in capability_targets
                and not (
                    operation.target.selector == "body"
                    and operation.target.source_url
                    and any(
                        _canonical_url(page.url) == _canonical_url(operation.target.source_url)
                        and (page.visible_facts or page.evidence_refs)
                        for page in context.page_knowledge
                    )
                )
            ):
                raise PlanningValidationError(
                    f"Target is not grounded in current evidence: {operation.target.name}"
                )
            if operation.kind is OperationKind.NAVIGATE:
                navigated_to.add(_canonical_url(urljoin(context.url, str(operation.value))))
            elif operation.kind is OperationKind.OPEN_NAVIGATION_ITEM:
                destination = next(
                    (
                        str(condition.expected)
                        for condition in operation.postconditions
                        if condition.kind == "url"
                    ),
                    None,
                )
                if destination:
                    navigated_to.add(_canonical_url(urljoin(context.url, destination)))
            elif operation.target is not None:
                # Prefer provenance carried on the operation target. A
                # selector is not globally unique in SPAs and may otherwise
                # resolve to a later page's repeated card/control.
                source = (
                    operation.target.source_url
                    if operation.target.selector
                    else source_by_selector.get(operation.target.selector or "")
                )
                if source and (
                    _canonical_url(source) not in navigated_to
                    and not any(_route_key(route) == _route_key(source) for route in navigated_to)
                ):
                    raise PlanningValidationError(
                        f"Target {operation.target.name} requires an observed navigation to {source}"
                    )
                # Names and selectors are only unique within a page state.
                # Prefer the operation's source/selector provenance before
                # falling back to a name match; otherwise a repeated sidebar
                # label from another discovered page can make a valid href
                # appear to contradict the intended navigation.
                target_source = operation.target.source_url
                target_selector = operation.target.selector
                item = next(
                    (
                        candidate
                        for candidate in context.elements
                        if target_source
                        and candidate.source_url
                        and _canonical_url(candidate.source_url) == _canonical_url(target_source)
                        and target_selector
                        and candidate.selector == target_selector
                    ),
                    next(
                        (
                            candidate
                            for candidate in context.elements
                            if target_source
                            and candidate.source_url
                            and _canonical_url(candidate.source_url)
                            == _canonical_url(target_source)
                            and candidate.name.casefold() == operation.target.name.casefold()
                        ),
                        next(
                            (
                                candidate
                                for candidate in context.elements
                                if target_selector and candidate.selector == target_selector
                            ),
                            next(
                                (
                                    candidate
                                    for candidate in context.elements
                                    if candidate.name.casefold() == operation.target.name.casefold()
                                ),
                                None,
                            ),
                        ),
                    ),
                )
                expected_url = next(
                    (
                        str(condition.expected)
                        for condition in operation.postconditions
                        if condition.kind == "url"
                    ),
                    None,
                )
                if item and item.href and expected_url:
                    observed_destination = urljoin(item.source_url or context.url, item.href)
                    expected_destination = urljoin(context.url, expected_url)
                    matches_expected = (
                        observed_destination.endswith(expected_url.removeprefix("**"))
                        if expected_url.startswith("**/")
                        else (
                            _canonical_url(expected_destination)
                            == _canonical_url(observed_destination)
                            or _route_key(expected_destination) == _route_key(observed_destination)
                        )
                    )
                    if not matches_expected:
                        raise PlanningValidationError(
                            "Navigation postcondition contradicts observed target href"
                        )
            if operation.kind not in NO_POSTCONDITION_REQUIRED and not operation.postconditions:
                raise PlanningValidationError(f"Postcondition required for {operation.kind}")

__all__ = [
    "ValidationMixin",
]
