"""Objective grounding and exploration-target helpers."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from pydantic import ValidationError

from app.contracts.models import (
    ActionCapability,
    ObservedElement,
    OperationKind,
    Postcondition,
    ProductContext,
    Target,
    WorkflowProposal,
)
from app.planning.candidates import (
    build_page_complete_proposal,
)
from app.planning.production.shared import *


class GroundingMixin:
    @staticmethod
    def _repair_missing_postconditions(proposal: WorkflowProposal) -> WorkflowProposal:
        """Add portable state witnesses when a structured action omitted one.

        Model output often captures the intended operation but forgets the
        small verification predicate required by ProductLens' executor.  The
        predicate can be derived from the universal operation contract for
        reversible actions; submit/create operations deliberately remain
        fail-closed because only an independently observed outcome may prove
        them successful.
        """
        repaired: list[SemanticOperation] = []
        value_kinds = {
            OperationKind.FILL_TEXT,
            OperationKind.FILL_EMAIL,
            OperationKind.FILL_PHONE,
            OperationKind.SEARCH,
            OperationKind.SELECT_OPTION,
            OperationKind.SELECT_DATE,
            OperationKind.SELECT_DATE_RANGE,
        }
        visible_kinds = {OperationKind.OPEN_MODAL}
        changed_kinds = {
            OperationKind.CLICK,
            OperationKind.OPEN_NAVIGATION_ITEM,
            OperationKind.CLOSE_MODAL,
            OperationKind.APPLY_FILTER,
            OperationKind.CHECK,
            OperationKind.UNCHECK,
            OperationKind.CHOOSE_RADIO,
            OperationKind.DRAG,
            OperationKind.KEY_PRESS,
        }
        for operation in proposal.steps:
            if operation.postconditions or operation.target is None:
                repaired.append(operation)
                continue
            condition: Postcondition | None = None
            if operation.kind in value_kinds and operation.value not in (None, ""):
                condition = Postcondition(
                    kind="value", expected=operation.value, target=operation.target
                )
            elif operation.kind in visible_kinds:
                condition = Postcondition(kind="visible", expected=True, target=operation.target)
            elif operation.kind in changed_kinds:
                condition = Postcondition(kind="changed", expected=True, target=operation.target)
            if condition is None:
                repaired.append(operation)
            else:
                repaired.append(operation.model_copy(update={"postconditions": [condition]}))
        return proposal.model_copy(update={"steps": repaired})

    @staticmethod
    def _repair_visual_gestures(
        proposal: WorkflowProposal, context: ProductContext
    ) -> WorkflowProposal:
        """Complete underspecified visual gestures from observed surface geometry.

        Structured models sometimes identify the right canvas and intent but
        omit the concrete path required by the execution contract.  Rejecting
        that plan is safer than drawing a guessed path, but silently falling
        back to a page tour is worse: it produces a video that claims to
        demonstrate an editor without changing it.  When the target is an
        observed canvas/SVG/application surface, derive a bounded, relative
        gesture from that surface and preserve the model's semantic intent.
        This is a universal browser gesture repair, not a product or route
        recipe; non-surface targets remain a hard planning failure.
        """
        visual_tags = {"canvas", "svg"}
        surface_items = [
            item
            for item in [*context.elements, *context.navigation]
            if item.tag.casefold() in visual_tags
            or (item.role or "").casefold() in {"application", "img"}
            or "workspace" in item.name.casefold()
            or "canvas" in item.name.casefold()
        ]
        if not surface_items:
            return proposal

        def match_surface(operation: SemanticOperation) -> ObservedElement | None:
            target = operation.target
            if target is None:
                return None
            candidates = [
                item
                for item in surface_items
                if (not target.source_url or item.source_url == target.source_url)
                and (
                    (target.selector and item.selector == target.selector)
                    or item.name.casefold() == target.name.casefold()
                )
            ]
            if len(candidates) == 1:
                return candidates[0]
            # Models may shorten an observed surface label (``canvas`` for
            # ``canvas workspace``) or choose the implementation tag as the
            # selector. Resolve that shorthand only when one surface has the
            # strongest semantic token overlap; never pick an arbitrary DOM
            # element from a dense editor.
            wanted = set(re.findall(r"[a-z0-9]{3,}", target.name.casefold()))
            scored = []
            for item in surface_items:
                if target.source_url and item.source_url != target.source_url:
                    continue
                observed = set(re.findall(r"[a-z0-9]{3,}", item.name.casefold()))
                if target.selector and target.selector.casefold() == item.tag.casefold():
                    observed.add(item.tag.casefold())
                score = len(wanted & observed)
                if score:
                    scored.append((score, item))
            if scored:
                best = max(score for score, _item in scored)
                best_items = [item for score, item in scored if score == best]
                if len(best_items) == 1:
                    return best_items[0]
                # A visual editor can expose an SVG accessibility overlay and
                # the underlying canvas with the same ``workspace`` label. In
                # that generic tie, the real canvas is the safer drawing
                # surface; this does not rely on a product name or selector.
                canvas_items = [item for item in best_items if item.tag.casefold() == "canvas"]
                if len(canvas_items) == 1:
                    return canvas_items[0]
            return None

        def visual_pattern(operation: SemanticOperation, value: dict) -> str:
            explicit = str(value.get("pattern") or "").strip()
            if explicit:
                return explicit
            intent = operation.intent.casefold()
            if re.search(r"\b(connect|arrow|link|flow|between)\b", intent):
                return "connector_segment"
            if re.search(r"\b(box|rectangle|component|shape|node|service)\b", intent):
                return "shape_box"
            if re.search(r"\b(label|text|name|title|type)\b", intent):
                return "text_placement"
            return "short_reversible_stroke"

        def path_signature(candidate: object) -> tuple[tuple[float, float], ...] | None:
            if not isinstance(candidate, list):
                return None
            points = [
                (round(float(point["x"]), 3), round(float(point["y"]), 3))
                for point in candidate
                if isinstance(point, dict)
                and isinstance(point.get("x"), (int, float))
                and isinstance(point.get("y"), (int, float))
            ]
            return tuple(points) if len(points) >= 2 else None

        shape_signatures = [
            path_signature(
                (operation.value or {}).get("relative_points")
                if isinstance(operation.value, dict)
                else None
            )
            for operation in proposal.steps
            if operation.kind is OperationKind.POINTER_SEQUENCE
            and visual_pattern(
                operation, operation.value if isinstance(operation.value, dict) else {}
            )
            == "shape_box"
        ]
        normalize_shape_layout = len(shape_signatures) > 1 and len(
            {signature for signature in shape_signatures if signature is not None}
        ) < len(shape_signatures)
        shape_index = 0
        shape_centers: list[tuple[float, float]] = []
        repaired: list[SemanticOperation] = []
        for operation in proposal.steps:
            if operation.kind is not OperationKind.POINTER_SEQUENCE:
                repaired.append(operation)
                continue
            value = operation.value if isinstance(operation.value, dict) else {}
            points = value.get("points")
            relative_points = value.get("relative_points")
            pattern = visual_pattern(operation, value)

            def valid_path(candidate: object) -> bool:
                return (
                    isinstance(candidate, list)
                    and len(candidate) >= 2
                    and all(
                        isinstance(point, dict)
                        and isinstance(point.get("x"), (int, float))
                        and isinstance(point.get("y"), (int, float))
                        for point in candidate
                    )
                )

            if valid_path(points) or valid_path(relative_points):
                path = points if valid_path(points) else relative_points
                if normalize_shape_layout and pattern == "shape_box":
                    # If a model repeats one gesture for every component,
                    # deterministically spread the observed components into
                    # a readable grid. This is a generic layout repair, not a
                    # product-specific diagram recipe.
                    column = shape_index % 3
                    row = shape_index // 3
                    center = (0.22 + column * 0.28, 0.28 + row * 0.26)
                    path = [
                        {"x": center[0] - 0.09, "y": center[1] - 0.08},
                        {"x": center[0] + 0.09, "y": center[1] + 0.08},
                    ]
                    shape_index += 1
                    shape_centers.append(center)
                elif normalize_shape_layout and pattern == "text_placement" and shape_centers:
                    center = shape_centers[-1]
                    path = [
                        {"x": center[0] - 0.01, "y": center[1]},
                        {"x": center[0] + 0.01, "y": center[1]},
                    ]
                elif normalize_shape_layout and pattern == "connector_segment" and len(shape_centers) >= 2:
                    source, target = shape_centers[-2:]
                    path = [{"x": source[0], "y": source[1]}, {"x": target[0], "y": target[1]}]
                updated_value = {**value}
                if path is not None and path is not points:
                    updated_value["relative_points"] = path
                    updated_value.pop("points", None)
                updated_value.setdefault("pattern", pattern)
                if not operation.postconditions:
                    repaired.append(
                        operation.model_copy(
                            update={
                                "value": updated_value,
                                "postconditions": [
                                    Postcondition(
                                        kind="surface_changed",
                                        expected=True,
                                        target=operation.target,
                                    )
                                ]
                            }
                        )
                    )
                else:
                    repaired.append(operation.model_copy(update={"value": updated_value}))
                continue
            surface = match_surface(operation)
            if surface is None:
                repaired.append(operation)
                continue
            intent = operation.intent.casefold()
            if re.search(r"\b(connect|arrow|link|flow|between)\b", intent):
                pattern = "connector_segment"
                path = [{"x": 0.22, "y": 0.50}, {"x": 0.78, "y": 0.50}]
            elif re.search(r"\b(box|rectangle|component|shape|node|service)\b", intent):
                pattern = "shape_box"
                path = [
                    {"x": 0.25, "y": 0.28},
                    {"x": 0.68, "y": 0.62},
                ]
            elif re.search(r"\b(label|text|name|title|type)\b", intent):
                pattern = "text_placement"
                path = [{"x": 0.38, "y": 0.42}, {"x": 0.40, "y": 0.43}]
            else:
                pattern = "short_reversible_stroke"
                path = [{"x": 0.30, "y": 0.50}, {"x": 0.70, "y": 0.50}]
            if normalize_shape_layout and pattern == "shape_box":
                column = shape_index % 3
                row = shape_index // 3
                center = (0.22 + column * 0.28, 0.28 + row * 0.26)
                path = [
                    {"x": center[0] - 0.09, "y": center[1] - 0.08},
                    {"x": center[0] + 0.09, "y": center[1] + 0.08},
                ]
                shape_index += 1
                shape_centers.append(center)
            elif normalize_shape_layout and pattern == "text_placement" and shape_centers:
                center = shape_centers[-1]
                path = [
                    {"x": center[0] - 0.01, "y": center[1]},
                    {"x": center[0] + 0.01, "y": center[1]},
                ]
            elif normalize_shape_layout and pattern == "connector_segment" and len(shape_centers) >= 2:
                source, target = shape_centers[-2:]
                path = [{"x": source[0], "y": source[1]}, {"x": target[0], "y": target[1]}]
            evidence = list(
                dict.fromkeys(
                    [
                        *operation.evidence_refs,
                        f"geometry:{surface.source_url or context.url}:{surface.name}",
                        f"surface:{surface.source_url or context.url}:{surface.selector}",
                    ]
                )
            )
            repaired.append(
                operation.model_copy(
                    update={
                        "value": {**value, "pattern": pattern, "relative_points": path},
                        "evidence_refs": evidence,
                        "postconditions": operation.postconditions
                        or [
                            Postcondition(
                                kind="surface_changed",
                                expected=True,
                                target=operation.target,
                            )
                        ],
                    }
                )
            )
        return proposal.model_copy(update={"steps": repaired})

    @staticmethod
    def _validate_objective_grounding(context: ProductContext, candidate) -> None:
        """Refuse a polished route tour when required objective evidence is absent.

        The old planner could select a reachable Settings shell and then claim
        to explain its relationship to an operational feature.  A relationship
        is now a planning precondition: both sides must be present in the
        selected candidate's freshly captured page evidence.
        """
        specification = context.objective
        if specification is None:
            return
        # Older/resumable ObjectiveSpecs can describe duration and audience
        # without making a semantic entity/context claim. They remain valid
        # compatibility inputs; only explicit grounding requirements demand
        # PageKnowledge.
        if (
            not specification.primary_entity
            and not specification.supporting_relationships
            and not specification.must_show
        ):
            return
        if candidate is None:
            raise PlanningValidationError(
                "no candidate flow is grounded in the requested objective"
            )
        selected = {
            _canonical_url(page.url): page
            for page in context.page_knowledge
            if _canonical_url(page.url)
            in {
                _canonical_url(url)
                for url in [*candidate.page_urls, *candidate.supporting_page_urls]
            }
        }
        if not selected:
            raise PlanningValidationError("candidate flow has no fresh page knowledge")

        def vocabulary(value: str) -> set[str]:
            # Requests are often editorial prose ("home identity and
            # capabilities"), while page evidence is terse UI language. Do
            # not make harmless function words literal grounding requirements;
            # require the meaningful concept overlap below instead.
            stopwords = {
                "the",
                "and",
                "for",
                "from",
                "with",
                "into",
                "that",
                "this",
                "its",
                "then",
                "than",
                "every",
                "each",
                "all",
                "one",
                "on",
                "in",
                "of",
                "to",
                "a",
                "an",
                "is",
                "are",
                "be",
                "by",
                "or",
                "including",
                "initial",
                "dashboard",
                "view",
                "views",
                "screen",
                "screens",
                "tab",
                "tabs",
                # Editorial qualifiers describe how to present evidence, not
                # a literal DOM label that must appear on the selected page.
                "representative",
                "meaningful",
                "visible",
                "actual",
                "relevant",
                "primary",
                "complete",
                "full",
                "current",
                "requested",
                "details",
                "detail",
                "outcome",
                "result",
                "management",
                "workflow",
                "flow",
                "experience",
                # Generic surface descriptors may be added by objective
                # understanding even when the product uses a different
                # visible label (for example ``public diagram editor`` vs
                # ``Untitled Diagram``).  They are not feature evidence.
                "public",
                "private",
                "app",
                "application",
                "editor",
                "workspace",
                "tool",
                "software",
                "platform",
                "product",
            }
            words = {
                word for word in re.findall(r"[a-z0-9]{3,}", value.lower()) if word not in stopwords
            }
            # Normalize ordinary English inflections so an objective such as
            # "study planning" grounds against a visible "study plan" label
            # without embedding a product-specific synonym table. Keep the
            # original token as well; these are evidence aids, not fuzzy
            # authorization to select an unrelated page.
            for word in tuple(words):
                if word.endswith("s") and len(word) > 3:
                    words.add(word[:-1])
                if word.endswith("ing") and len(word) > 5:
                    base = word[:-3]
                    words.add(base)
                    if len(base) > 2 and base[-1] == base[-2]:
                        words.add(base[:-1])
                if word.endswith("ed") and len(word) > 4:
                    words.add(word[:-2])
            if "configuration" in words:
                words.add("config")
            if "config" in words:
                words.add("configuration")
            # Do not embed product/domain vocabulary here.  Synonyms such as
            # “appointment”/“booking” belong in the model-produced objective
            # concepts (or page evidence), not in a runtime route rule.  This
            # keeps grounding generic for an unseen product while preserving
            # the evidence threshold below.
            if "setup" in words:
                words.update({"config", "configuration", "settings"})
            # DOM semantics can name a drawing surface by its implementation
            # element (SVG) while a request calls it a canvas. Treat the
            # equivalence as an evidence ontology, not a site-specific route
            # rule; both terms still require an observed visual-surface node.
            if "svg" in words:
                words.add("canvas")
            if "canvas" in words:
                words.add("svg")
            return words

        evidence_words = set()
        for page in selected.values():
            path = urlsplit(page.url).path
            evidence_words |= vocabulary(
                " ".join(
                    [
                        page.title,
                        page.purpose,
                        *page.visible_sections,
                        *page.scroll_landmarks,
                        *page.actionable_controls,
                        *page.visible_facts,
                        path.replace("/", " ").replace("-", " "),
                        "home" if path in {"", "/"} else "",
                    ]
                )
            )
        # Login is a requested presentation chapter, not a page-local content
        # landmark. Once discovery has authenticated the fresh browser, the
        # credential boundary itself is the authoritative evidence that this
        # requirement is satisfied; requiring the post-login DOM to repeat
        # the word "login" incorrectly rejects otherwise valid plans.
        if context.authentication_state == "authenticated":
            evidence_words.update({"login", "sign", "authentication", "authenticated"})
        # Objectives describe capabilities in human language, while a UI can
        # label the same workspace with the entity alone ("Customers" versus
        # "Customer Management").  Ground the meaningful entity terms here;
        # the relationship validation below enforces any requested setup or
        # outcome role separately.
        objective_generic = {
            "flow",
            "management",
            "workflow",
            "experience",
            "page",
            "pages",
            "module",
            "feature",
            "area",
            "screen",
            "view",
            "lifecycle",
            "journey",
            "progression",
            "context",
            # Objective parsers may add a generic product descriptor to
            # an otherwise grounded noun phrase (for example
            # ``public diagram editor``).  These words describe the
            # delivery surface, not the feature being demonstrated;
            # keeping them out of grounding prevents an unfamiliar app
            # from being rejected merely because its title says
            # ``Untitled Diagram`` rather than ``Diagram Editor``.
            "public",
            "private",
            "app",
            "application",
            "editor",
            "workspace",
            "tool",
            "software",
            "platform",
            "product",
        }
        primary_words = vocabulary(specification.primary_entity or "") - objective_generic
        primary_overlap = len(primary_words & evidence_words) / max(1, len(primary_words))
        # Objective understanding may add one descriptive modifier that is
        # not repeated verbatim by the UI (``collaborative drawing workspace``
        # vs. a visible ``Drawing`` surface). Require at least one concrete
        # noun and half of a multi-word entity; single-token requests remain
        # exact. This avoids rejecting unfamiliar products without accepting
        # an unrelated candidate on a generic word alone.
        primary_grounded = (
            not primary_words
            or primary_words.issubset(evidence_words)
            or (len(primary_words) >= 2 and primary_overlap >= 0.5)
        )
        # Model-parsed full-walkthrough requests often use an editorial
        # product description ("the study planning experience") instead of
        # the product's visible brand/title ("Interview Crack"). Do not turn
        # that wording mismatch into a false rejection when concrete
        # must-show requirements are independently grounded across multiple
        # discovered pages. Narrow feature/workflow requests remain strict.
        broad_walkthrough_grounded = (
            specification.demo_type == "full_walkthrough"
            and len(selected) >= 2
            and any(
                vocabulary(requirement) & evidence_words
                for requirement in specification.must_show
                if requirement != specification.primary_entity
            )
        )
        # For an action-led visual objective, the named artifact is the
        # output to create (for example, a chat architecture diagram), not a
        # noun that must already be present in the opening DOM.  Requiring
        # that phrase to be visible would reject unfamiliar canvas/graph
        # editors before the agent can create it.  The selected surface,
        # tools, and post-action topology remain evidence-grounded below.
        visual_artifact_objective = bool(
            re.search(
                r"\b(?:create|build|draw|design|make|edit|sketch)\b",
                specification.raw,
                re.IGNORECASE,
            )
            and re.search(
                r"\b(?:diagram|architecture|whiteboard|canvas|drawing|flowchart|graph)\b",
                specification.raw,
                re.IGNORECASE,
            )
        )
        if (
            primary_words
            and not primary_grounded
            and not broad_walkthrough_grounded
            and not visual_artifact_objective
        ):
            raise PlanningValidationError(
                f"requested entity is not grounded by the selected candidate: {specification.primary_entity}"
            )
        for required in specification.must_show:
            if (
                required == specification.primary_entity
                and not primary_grounded
                and broad_walkthrough_grounded
            ):
                continue
            required_words = vocabulary(required)
            # Results requested by an action-led visual objective are
            # intentionally absent from the opening page.  For example,
            # “completed design” is the post-action canvas state that
            # execution must create and verify, not content discovery should
            # expect to find before any drawing occurs.  Keep this exception
            # narrow to result-state language and only when the objective has
            # already established a visual artifact workflow above.
            result_state_words = {
                "completed",
                "created",
                "configured",
                "connected",
                "drawn",
                "filled",
                "generated",
                "resulting",
                "saved",
                "selected",
                "submitted",
                "updated",
            }
            if visual_artifact_objective and required_words & result_state_words:
                continue
            # Objective-understanding models sometimes promote an explanatory
            # sentence (for example, "what the product helps a learner plan
            # and track") into ``must_show``.  That is a story intent, not a
            # literal UI entity that can be grounded by a single label.  For
            # broad walkthroughs, page-purpose and section evidence already
            # enforce this intent; do not reject a valid product merely
            # because its UI uses different wording.  Concrete noun phrases
            # (Today, Progress, invoice export, etc.) remain strict below.
            if (
                specification.demo_type == "full_walkthrough"
                and re.match(r"^(?:what|how|why)\b", required.strip().casefold())
                and re.search(
                    r"\b(?:product|application|system|workspace|feature)\b", required.casefold()
                )
            ):
                continue
            # Preserve hard rejection for an entirely unsupported requirement,
            # but tolerate editorial wording where one descriptor is implicit
            # in the page title/layout (for example "identity" on a named
            # portfolio home page). A 60% meaningful-token threshold prevents
            # a single generic word from laundering an unrelated requirement.
            grounded_words = required_words & evidence_words
            required_threshold = max(1, int(len(required_words) * 0.6 + 0.999))
            editorial_descriptors = {
                "identity",
                "value",
                "proposition",
                "capability",
                "capabilities",
                "meaningful",
                "featured",
                "project",
                "projects",
                "career",
                "role",
                "roles",
                "contribution",
                "contributions",
                "architecture",
                "architectural",
                "work",
                "representative",
                "theme",
                "themes",
                "available",
                "path",
                "visible",
                "page",
                "pages",
                "operational",
                "state",
                "states",
                "sidebar",
                "handled",
                "current",
                "relevant",
                "actual",
            }
            if required_words and required_words.issubset(editorial_descriptors):
                # These words describe how the evidence should be presented,
                # not an additional product entity that can be matched
                # literally (for example, "visible states"). The page-level
                # evidence and scene completion gates still enforce that a
                # readable state was actually captured.
                continue
            if len(grounded_words) < required_threshold:
                # Walkthrough requests are often written as editorial briefs
                # ("Home identity", "Timeline with career roles") while the
                # site exposes concrete labels such as a person's name or
                # "Professional History". Accept the requirement when a
                # structural page/section token is grounded and the remaining
                # words are presentation descriptors, not a hidden product
                # claim. The selected page still has to carry real visible
                # evidence and is independently explored later.
                structural_overlap = set()
                for page in selected.values():
                    path = urlsplit(page.url).path
                    page_structural = vocabulary(
                        " ".join(
                            [
                                page.title,
                                page.purpose,
                                path.replace("/", " ").replace("-", " "),
                                "home" if path in {"", "/"} else "",
                                *page.actionable_controls,
                            ]
                        )
                    )
                    structural_overlap |= required_words & page_structural
                residual = required_words - structural_overlap
                if structural_overlap and residual.issubset(editorial_descriptors):
                    continue
            if required_words and len(grounded_words) < required_threshold:
                raise PlanningValidationError(
                    f"must-show requirement is not grounded by selected pages: {required}"
                )
        relationship_generic = objective_generic
        for relation in specification.supporting_relationships:
            source = vocabulary(relation.source)
            target = vocabulary(relation.target)
            if not relation.required:
                continue
            # Persisted objective artifacts may contain a model-produced
            # relationship that was inferred from ordinary prose (for
            # example, ``Home identity``).  A relationship is a workflow
            # dependency only when the original request explicitly connects
            # both sides; otherwise page-local evidence and the selected flow
            # remain the authority.  This guard also makes older runs safe to
            # resume after the objective-understanding filter is tightened.
            raw_objective = specification.raw.casefold()
            source_phrase = " ".join(re.findall(r"[a-z0-9]{3,}", relation.source.casefold()))
            target_phrase = " ".join(re.findall(r"[a-z0-9]{3,}", relation.target.casefold()))
            connector = r"(?:->|→|configures|explains|supports|in the context of|configured by|with context from|using)"
            if source_phrase and target_phrase:
                explicit_relation = bool(
                    re.search(
                        rf"{re.escape(source_phrase)}\s*{connector}\s*{re.escape(target_phrase)}",
                        raw_objective,
                    )
                    or re.search(
                        rf"{re.escape(target_phrase)}\s*{connector}\s*{re.escape(source_phrase)}",
                        raw_objective,
                    )
                )
                if not explicit_relation:
                    continue

            # A Settings shell can expose a configuration label without ever
            # showing that configuration's own detail state. Relationship
            # context is only useful when selected page *identity* (not its
            # navigation chrome) establishes both sides of the relationship.
            def page_identity_words(page) -> set[str]:
                path = urlsplit(page.url).path
                # The root route is the conventional Home page even when the
                # document title never contains the word "home". Preserve
                # that structural evidence for objective relationships such
                # as Home -> Timeline without introducing a product-specific
                # route exception.
                structural = "home" if path in {"", "/"} else ""
                return vocabulary(
                    " ".join(
                        [
                            page.title,
                            page.purpose,
                            path.replace("/", " ").replace("-", " "),
                            structural,
                        ]
                    )
                )

            shared_entity = (source & target) - relationship_generic
            # A relationship must first be tied to an observed entity.  Do
            # not require every prose descriptor in the request to appear
            # literally: products may call an operational workspace "Leads
            # V3" while the request calls it "Lead Management".  The
            # semantic detail checks below still require a separate concrete
            # setup/context page and an operational page.
            if shared_entity and not shared_entity.issubset(evidence_words):
                raise PlanningValidationError(
                    f"required objective relationship is not grounded by selected pages: {relation.source} -> {relation.target}"
                )

            def subject_pages(subject: set[str], counterpart: set[str]):
                meaningful = subject - relationship_generic
                # The setup side must prove the term that distinguishes it
                # from the operational feature. A shared entity noun alone
                # cannot establish a configuration relationship when it also
                # appears on the feature's workspace.
                distinguishing = meaningful - (counterpart - relationship_generic)
                required_words = distinguishing or meaningful or subject
                # Routes commonly use a plural entity while the request uses
                # a singular feature phrase. Vocabulary normalisation retains
                # both forms, so a concrete non-generic subject is sufficient.
                return [
                    page for page in selected.values() if page_identity_words(page) & required_words
                ]

            source_pages = subject_pages(source, target)
            target_pages = subject_pages(target, source)
            distinct_page_pair = any(
                source_page.url != target_page.url
                for source_page in source_pages
                for target_page in target_pages
            )
            if not source_pages or not target_pages or not distinct_page_pair:
                raise PlanningValidationError(
                    "required objective relationship lacks a selected semantic detail page: "
                    f"{relation.source} -> {relation.target}"
                )

    @staticmethod
    def _ground(proposal: WorkflowProposal, context: ProductContext) -> list:
        """Replace planner target hints with the exact observed locator evidence."""
        observed_items = [*context.elements, *context.navigation]
        capability_target_names: set[tuple[str | None, str]] = set()
        for raw in context.capabilities:
            try:
                capability = ActionCapability.model_validate(raw)
            except ValidationError:
                continue
            for field in capability.form_schema.fields if capability.form_schema else []:
                capability_target_names.add((capability.source_url, field.name.casefold()))
            for target in (capability.submit_target, capability.outcome_target):
                if target is not None:
                    capability_target_names.add((target.source_url, target.name.casefold()))
        by_selector = {item.selector: item for item in observed_items}
        by_name = {item.name.lower(): item for item in observed_items}
        roles = {
            "a": "link",
            "button": "button",
            "select": "combobox",
            "textarea": "textbox",
            "h1": "heading",
            "h2": "heading",
            "h3": "heading",
            "h4": "heading",
        }
        grounded = []
        for operation in proposal.steps:
            if operation.kind is OperationKind.NAVIGATE:
                grounded.append(
                    operation.model_copy(
                        update={"value": urljoin(context.url, str(operation.value))}
                    )
                )
                continue
            if operation.target is None:
                grounded.append(operation)
                continue
            selector = operation.target.selector or ""
            # A page-level verified hold is intentionally grounded by the
            # captured PageKnowledge record rather than a DOM element. This
            # is used for dense grids/transient states where every element is
            # a table cell or modal control and selecting one would create a
            # misleading editorial target.
            if (
                selector == "body"
                and operation.target.source_url
                and any(
                    _canonical_url(page.url) == _canonical_url(operation.target.source_url)
                    and (page.visible_facts or page.evidence_refs)
                    for page in context.page_knowledge
                )
            ):
                grounded.append(operation)
                continue
            # Discovery uses generic tag selectors only when a page does not
            # expose a stronger id/test id. Such selectors are not unique, so
            # prefer the observed accessible name for grounding in that case.
            # Page provenance is stronger than a generic selector or name.
            # In multi-page discovery the same label is often present in every
            # global navigation bar; falling back to a name map first can make
            # a planned local heading scroll on an entirely different page.
            # A deterministic tour has already selected a specific local
            # landmark. Preserve that exact selector/name/source triple before
            # applying the broad source/name fallback; otherwise a navbar link
            # that happens to share the heading text wins simply because it is
            # first in the discovery inventory.
            exact_source_match = next(
                (
                    candidate
                    for candidate in observed_items
                    if operation.target.source_url
                    and candidate.source_url == operation.target.source_url
                    and candidate.selector == selector
                    and candidate.name.lower() == operation.target.name.lower()
                ),
                None,
            )
            source_match = exact_source_match or next(
                (
                    candidate
                    for candidate in observed_items
                    if operation.target.source_url
                    and candidate.source_url == operation.target.source_url
                    and candidate.name.lower() == operation.target.name.lower()
                ),
                None,
            )
            item = source_match
            if item is None:
                item = (
                    by_name.get(operation.target.name.lower())
                    if selector in {"a", "button", "input", "select", "textarea"}
                    else by_selector.get(selector)
                ) or by_name.get(operation.target.name.lower())
            if item is None:
                if (
                    operation.target.source_url,
                    operation.target.name.casefold(),
                ) in capability_target_names:
                    # Fields inside a reversible modal are intentionally absent
                    # from the resting page DOM. Preserve their discovery-time
                    # semantic label; execution re-grounds only after the
                    # verified open-modal step has made the form visible.
                    grounded.append(operation)
                    continue
                raise PlanningValidationError(
                    f"Target is not grounded in current evidence: {operation.target.name}"
                )
            test_id = None
            if item.selector.startswith('[data-testid="'):
                test_id = item.selector.removeprefix('[data-testid="').removesuffix('"]')
            # A discovered canvas/SVG surface is often identified only by its
            # semantic tag. Preserve that selector when discovery proves it is
            # a unique actionable surface; stripping it would leave only the
            # synthetic label (for example ``svg workspace``), which cannot be
            # re-grounded after a tool changes the editor state.
            stable_selector = (
                item.selector
                if item.selector.startswith(("#", "["))
                or (
                    item.tag.casefold() in {"canvas", "svg"}
                    and item.selector.casefold() == item.tag.casefold()
                )
                else None
            )
            if item.href and item.tag == "a":
                stable_selector = f"a[href='{item.href.replace(chr(39), chr(92) + chr(39))}']"
            target = Target(
                name=item.name,
                test_id=test_id,
                role=item.role or roles.get(item.tag),
                selector=stable_selector,
                # Generic anchors/buttons may not expose a usable ARIA role
                # in real product DOMs. Retain exact observed text as a second
                # deterministic locator rather than falling back to geometry.
                text=item.name
                if item.selector in {"a", "button", "h1", "h2", "h3", "h4"}
                else (None if item.role or item.tag in roles else item.name),
                source_url=item.source_url,
                confidence_required=operation.target.confidence_required,
            )
            value = operation.value
            if operation.kind in {
                OperationKind.FILL_TEXT,
                OperationKind.FILL_EMAIL,
                OperationKind.FILL_PHONE,
                OperationKind.SELECT_OPTION,
                OperationKind.SELECT_DATE,
            }:
                expected = next(
                    (
                        condition.expected
                        for condition in operation.postconditions
                        if condition.kind == "value" and condition.target is not None
                    ),
                    None,
                )
                if expected is not None:
                    value = expected
            if operation.kind is OperationKind.SELECT_OPTION and value in (None, ""):
                selected = next(
                    (
                        option
                        for option in item.options
                        if option.strip()
                        and option.strip().lower()
                        not in {"select", "select an option", "choose", "choose an option"}
                    ),
                    None,
                )
                if selected is None:
                    raise PlanningValidationError(
                        f"Select option target has no observed selectable value: {item.name}"
                    )
                value = selected
            # Postconditions are executed through the same live grounding
            # adapter as actions.  Reusing the resolved semantic target keeps
            # a generic discovery selector (for example ``a``) from leaking
            # into a later visibility/value assertion.
            has_url_transition = any(
                condition.kind == "url" for condition in operation.postconditions
            )
            postconditions = []
            for condition in operation.postconditions:
                # A route transition is proven by its exact observed URL. Low-cost
                # planners sometimes add a second visible assertion copied from
                # the source page (for example the "Week 1" link after clicking
                # it). That assertion is neither destination-grounded nor needed
                # once the URL has passed, so never replay stale source evidence.
                if (
                    has_url_transition
                    and condition.kind == "visible"
                    and condition.target is not None
                    and condition.target.name.lower() != operation.target.name.lower()
                ):
                    continue
                postconditions.append(
                    condition.model_copy(update={"target": target})
                    if condition.target
                    and condition.target.name.lower() == operation.target.name.lower()
                    else condition
                )
            # The visible anchor's href is the authoritative destination for
            # semantic navigation. Models occasionally normalize away a query,
            # retain a stale SPA route, or copy a destination from another
            # repeated card. Once the target has been grounded to the current
            # evidence, align its URL postcondition with that observed href;
            # this prevents planning-time contradictions without inventing a
            # route or silently switching to direct navigation.
            if operation.kind is OperationKind.OPEN_NAVIGATION_ITEM and item.href:
                observed_destination = urljoin(item.source_url or context.url, item.href)
                postconditions = [
                    condition.model_copy(update={"expected": observed_destination})
                    if condition.kind == "url"
                    else condition
                    for condition in postconditions
                ]
            if operation.kind is OperationKind.SELECT_OPTION and not any(
                condition.kind in {"value", "changed"} for condition in postconditions
            ):
                postconditions.append(Postcondition(kind="value", expected=value, target=target))
            grounded.append(
                operation.model_copy(
                    update={"target": target, "value": value, "postconditions": postconditions}
                )
            )
        return grounded

    @staticmethod
    def _page_exploration_targets(
        context: ProductContext, destination: str, *, limit: int
    ) -> list[ObservedElement]:
        """Return readable, page-local landmarks for a chapter.

        A page visit is only narratively valid once material content is shown.
        This is deliberately DOM-backed rather than route-name driven: headings
        are preferred, then meaningful links/buttons with visible descriptive
        text. It also handles pages whose landmark heading is not exposed by
        selecting the strongest observed local controls as a conservative
        fallback.
        """
        canonical = destination.rstrip("/")
        local = [
            item
            for item in context.elements
            if (item.source_url or context.url).rstrip("/") == canonical and item.name.strip()
        ]
        page = next(
            (item for item in context.page_knowledge if item.url.rstrip("/") == canonical), None
        )
        # Some visual editors ship an untouched starter document whose sample
        # content is literally labelled "Heading"/"Title" and followed by
        # lorem-ipsum copy.  Those labels are implementation filler, not a
        # meaningful page-local story subject.  Filter them only when the
        # same page evidence proves the placeholder pattern; a real product
        # section named Heading remains eligible on all other pages.
        page_evidence_text = " ".join(
            str(value) for value in (getattr(page, "visible_facts", []) if page else [])
        ).casefold()
        placeholder_landmark = bool(
            "lorem ipsum" in page_evidence_text
            and any(token in page_evidence_text for token in ("heading", "title", "description"))
        )
        landmark_order = {
            " ".join(name.split()).lower(): index
            for index, name in enumerate(page.scroll_landmarks if page else [])
        }

        def priority(candidate: ObservedElement) -> tuple[int, int, int, int]:
            name = " ".join(candidate.name.split()).lower()
            return (
                0 if name in landmark_order else 1,
                landmark_order.get(name, 10_000),
                0 if candidate.tag in {"h1", "h2", "h3", "h4"} else 1,
            )

        excluded = {
            "reason",
            "message",
            "submit",
            "home",
            "back",
            "menu",
            "close",
            # Footer/navigation headings are persistent chrome, not the local
            # chapter content a full walkthrough should spend reading time on.
            "navigation",
            "connect",
            "footer",
        }
        navigation_labels = {" ".join(item.name.split()).lower() for item in context.navigation}
        seen: set[str] = set()
        repeated_series: set[str] = set()
        candidates: list[ObservedElement] = []
        for item in sorted(
            local,
            key=priority,
        ):
            label = " ".join(item.name.split())
            key = label.lower()
            if (
                key in seen
                or len(label) < 3
                or len(label) > 120
                # Composite responsive labels are not stable readable targets
                # in the production accessibility tree.
                or "%" in label
                or label.lower() in excluded
                or (
                    placeholder_landmark
                    and (
                        key in {"heading", "title", "description"}
                        or "lorem ipsum" in key
                        or any(
                            key.startswith(f"{value} ")
                            for value in ("heading", "title", "description")
                        )
                        or (
                            item.tag in {"a", "button"}
                            and not item.href
                            and len(re.findall(r"[a-z0-9]+", key)) <= 2
                        )
                    )
                )
                # A page-local chapter must never spend its reading beats on
                # the persistent global navbar.  Those labels are evidence for
                # transition, not the destination's content.
                or (item.tag == "a" and key in navigation_labels)
            ):
                continue
            # Repeated numbered schedule/list entries are usually equivalent
            # examples, not distinct story chapters.  A full walkthrough
            # establishes the parent collection and then shows one
            # representative week/day/module/lesson detail; expanding every
            # visible numbered sibling turns a human demo back into crawler
            # coverage.  This is deliberately semantic and product-agnostic.
            series_key = re.sub(r"\b\d+\b", "#", key)
            is_repeated_series = bool(
                re.search(r"\b(?:week|day|module|lesson|chapter|step)\s+#(?:\s|$)", series_key)
            )
            if is_repeated_series and series_key in repeated_series:
                continue
            seen.add(key)
            if is_repeated_series:
                repeated_series.add(series_key)
            candidates.append(item)

        # Repeated h3 cards directly beneath an h2 usually form a meaningful
        # collection (projects, case studies, releases, articles). A route
        # sweep previously consumed this area with whichever utility headings
        # appeared first. Establish the page, then include the largest visible
        # collection before filling remaining slots in document order.
        # Some modern sites use h4 labels for the cards that actually contain
        # the demonstrable system/module content.  Treat them as landmarks as
        # well; otherwise a page with one h1 and several rich h4 cards is
        # falsely considered explored after only its title is shown.
        heading_candidates = [item for item in candidates if item.tag in {"h1", "h2", "h3", "h4"}]
        # Inputs and buttons can be workflow targets, but are not reading
        # landmarks. Prefer semantic page headings whenever they are present.
        if heading_candidates:
            # Headings establish a section, but a dashboard/table/form page
            # often exposes only one heading while its actual interaction
            # surface is represented by descriptive rows, filters, or cards.
            # Keep a bounded set of those page-local witnesses as supplemental
            # reading landmarks instead of declaring the page complete after a
            # title-only scroll. This remains DOM/evidence-derived and works
            # for any product shape.
            original_candidates = list(candidates)
            candidates = list(heading_candidates)
            if len(heading_candidates) < 3:
                supplemental = [
                    item
                    for item in original_candidates
                    if item not in heading_candidates
                    and len(" ".join((item.text or item.name).split())) >= 24
                    and not (
                        item.tag == "a" and " ".join(item.name.split()).lower() in navigation_labels
                    )
                ]
                candidates.extend(supplemental[: max(0, 4 - len(candidates))])
        collections: list[tuple[ObservedElement, list[ObservedElement]]] = []
        for index, item in enumerate(heading_candidates):
            if item.tag != "h2":
                continue
            members: list[ObservedElement] = []
            for following in heading_candidates[index + 1 :]:
                if following.tag in {"h1", "h2"}:
                    break
                if following.tag == "h3":
                    members.append(following)
            if len(members) >= 3:
                collections.append((item, members))
        featured = max(collections, key=lambda item: len(item[1]), default=None)
        selected: list[ObservedElement] = []
        first_heading = next((item for item in heading_candidates if item.tag == "h1"), None)
        if first_heading is not None:
            selected.append(first_heading)
        # A featured collection is the page's concrete proof. Present its
        # heading and visible members before generic capability/metric
        # headings, otherwise a full walkthrough can consume the Home budget
        # without ever explaining the projects it claims to showcase.
        if featured is not None:
            collection_heading, members = featured
            if collection_heading not in selected:
                selected.append(collection_heading)
            selected.extend(item for item in members if item not in selected)
        # Keep section-level context compact after the featured proof.
        selected.extend(
            item for item in heading_candidates if item.tag == "h2" and item not in selected
        )
        selected.extend(item for item in candidates if item not in selected)
        return selected[:limit]

    @staticmethod
    def _evidence_fallback(candidate, context: ProductContext) -> WorkflowProposal:
        """Use a validated candidate, never navigation order, after model failure."""
        if candidate is None:
            raise PlanningValidationError("no evidence-grounded candidate flow is available")
        try:
            return build_page_complete_proposal(context, candidate)
        except ValueError as error:
            raise PlanningValidationError(str(error)) from error

    @staticmethod
    def _filter_unrequested_visual_gestures(
        proposal: WorkflowProposal, objective: str
    ) -> WorkflowProposal:
        """Prevent provider plans from inventing drawing actions on dashboards.

        Providers may over-read a generic SVG/chart as an invitation to draw,
        especially when the request contains words such as ``flow`` or
        ``shapes`` in ordinary prose.  Visual gestures are retained only when
        the user explicitly asks for a drawing/diagram/editor task; all other
        operations remain untouched and are still validated against evidence.
        """
        visual_terms = {
            "draw",
            "drawing",
            "diagram",
            "whiteboard",
            "canvas",
            "sketch",
            "paint",
        }
        if set(re.findall(r"[a-z0-9]{3,}", objective.casefold())) & visual_terms:
            return proposal
        filtered = [
            operation
            for operation in proposal.steps
            if operation.kind not in {OperationKind.POINTER_SEQUENCE, OperationKind.DRAG}
        ]
        if len(filtered) == len(proposal.steps):
            return proposal
        return proposal.model_copy(update={"steps": filtered})

__all__ = [
    "GroundingMixin",
]
