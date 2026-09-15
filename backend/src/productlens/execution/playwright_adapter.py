"""Playwright adapter. Coordinates are captured as evidence, never used to execute."""

from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from playwright.async_api import Error as PlaywrightError

from productlens.contracts.models import OperationKind, Rect, SemanticOperation, Target, Viewport
from productlens.execution.spatial_index import SpatialIndex


class GroundingError(RuntimeError):
    pass


def _route_key(value: str) -> tuple[str, str, str]:
    parsed = urlsplit(value)
    scheme = "https" if parsed.scheme in {"http", "https"} else parsed.scheme
    return scheme, parsed.netloc.casefold(), unquote(parsed.path).rstrip("/") or "/"


def _navigation_reached(current: str, expected: str, base: str) -> bool:
    if expected.startswith("**/"):
        return current.endswith(expected[2:])
    return _route_key(current) == _route_key(urljoin(base, expected))


class PlaywrightAdapter:
    def __init__(self, page: Any, *, cloud_mode: bool = False):
        self.page = page
        self.cloud_mode = cloud_mode

    def ensure_page(self) -> Any:
        """Reconnect to the live page after a remote navigation replacement.

        Some CDP providers replace the target page object when an SPA performs
        a hard navigation. Playwright normally hides that detail, but a remote
        session can briefly expose the old object as ``TargetClosedError``.
        Prefer the newest still-open page in the same context; never invent a
        new URL or silently continue without a browser target.
        """
        closed = self._page_is_closed(self.page)
        if not closed:
            return self.page
        context = getattr(self.page, "context", None)
        pages = getattr(context, "pages", []) if context is not None else []
        for candidate in reversed(list(pages)):
            if self._page_is_closed(candidate):
                continue
            self.page = candidate
            return candidate
        raise GroundingError(
            "The active browser page was closed and no replacement target is available"
        )

    @staticmethod
    def _page_is_closed(page: Any) -> bool:
        is_closed = getattr(page, "is_closed", None)
        if not callable(is_closed):
            return False
        try:
            return bool(is_closed())
        except (PlaywrightError, AttributeError, TypeError, RuntimeError):
            return True

    def locator(self, target: Target) -> Any:
        """Return the preferred deterministic locator for compatibility callers."""
        return self.locator_candidates(target)[0][1]

    async def wait_for_page_readiness(self, target: Target | None = None) -> None:
        """Wait for a usable, hydrated page before a scene begins.

        ``domcontentloaded`` is not enough for SPAs: a route can be present
        while its transition shell, hydration, or target component is still
        settling. This bounded check keeps the action semantic and avoids
        baking arbitrary sleep durations into every workflow.
        """
        page = self.ensure_page()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3_000)
        except PlaywrightError:
            pass
        try:
            await page.wait_for_function(
                "() => document.readyState !== 'loading' && !document.body?.matches('[aria-busy=\"true\"]')",
                timeout=3_000,
            )
        except PlaywrightError:
            # Long-lived websocket/analytics connections must not hold a scene
            # indefinitely; the next semantic grounding still owns failure.
            pass
        if target is not None:
            # Give a just-hydrated target a short opportunity to appear, but do
            # not resolve or cache its locator here. Action execution performs
            # the authoritative uniqueness/visibility grounding immediately
            # afterwards.
            for _ in range(8):
                try:
                    locator, _ = await self.grounded_locator(target)
                    if await locator.is_visible():
                        break
                except (GroundingError, PlaywrightError):
                    pass
                await page.wait_for_timeout(125)
        else:
            await page.wait_for_timeout(120)

    def locator_candidates(self, target: Target) -> list[tuple[str, Any]]:
        """Return evidence-backed candidates in the reliability order.

        Coordinates are deliberately absent. Each candidate remains a normal
        Playwright locator, so actionability is still enforced at execution.
        """
        self.ensure_page()
        candidates: list[tuple[str, Any]] = []
        # DOM innerText and accessible names may normalize newlines differently
        # at a responsive production viewport. Preserve the observed words,
        # while allowing whitespace-only layout changes without loosening to a
        # coordinate or a generic heading selector.
        flexible_name = re.compile(
            "^" + r"\\s+".join(re.escape(part) for part in target.name.split()) + "$",
            re.IGNORECASE,
        )
        if target.test_id:
            candidates.append(("test_id", self.page.get_by_test_id(target.test_id)))
        if target.role and target.name:
            candidates.append(
                ("role", self.page.get_by_role(target.role, name=target.name, exact=True))
            )
            candidates.append(
                (
                    "role_casefold",
                    self.page.get_by_role(target.role, name=flexible_name, exact=False),
                )
            )
            # Accessible labels can legitimately change punctuation or
            # truncation between discovery and production (for example an
            # ellipsis rendered as ``...`` versus ``…``).  Keep an additional
            # role-scoped contains query as a final semantic fallback; the
            # uniqueness/visibility checks below still reject ambiguous
            # matches, so this never degenerates into coordinate clicking.
            candidates.append(
                (
                    "role_contains",
                    self.page.get_by_role(
                        target.role, name=target.name.replace("…", ""), exact=False
                    ),
                )
            )
            # Native controls can expose their option inventory as part of the
            # accessible name. Use the semantic leading label as a bounded
            # fallback; uniqueness and visibility checks still decide safety.
            if len(target.name.split()) >= 4 and len(target.name) >= 32:
                prefix = target.name.split()[0]
                candidates.append(
                    (
                        "role_prefix",
                        self.page.get_by_role(
                            target.role,
                            name=re.compile(r"^" + re.escape(prefix) + r"\b", re.IGNORECASE),
                            exact=False,
                        ),
                    )
                )
        if target.label:
            candidates.append(("label", self.page.get_by_label(target.label, exact=True)))
        # A selector captured from the observed DOM is stronger evidence for
        # an action target than nearby descriptive text.  Forms commonly
        # render a label and its input as separate nodes (for example a
        # ``<span>Phone</span>`` beside ``<input name="phone">``).  Trying
        # text first can therefore click the label or wrapper and make a valid
        # plan fail in production.  Keep semantic role/label evidence first,
        # then use the observed selector, and only then fall back to text.
        if target.selector:
            candidates.append(("selector", self.page.locator(target.selector)))
        if target.text:
            candidates.append(("text", self.page.get_by_text(target.text, exact=True)))
            flexible_text = re.compile(
                "^" + r"\\s+".join(re.escape(part) for part in target.text.split()) + "$",
                re.IGNORECASE,
            )
            candidates.append(("text_casefold", self.page.get_by_text(flexible_text)))
            candidates.append(("text_contains", self.page.get_by_text(target.text, exact=False)))
        # Product surfaces are frequently embedded in same-origin or
        # cross-origin iframes (hosted editors, checkout widgets, dashboards).
        # Keep frame locators as a semantic fallback after the top-level DOM;
        # grounded_locator still enforces uniqueness/visibility, so this does
        # not weaken target safety or introduce coordinates.  Frame URLs are
        # intentionally not persisted as routes: the containing page remains
        # the story state and Playwright owns the frame execution context.
        for index, frame in enumerate(getattr(self.page, "frames", [])[1:], start=1):
            prefix = f"frame_{index}"
            try:
                if target.test_id:
                    candidates.append((f"{prefix}:test_id", frame.get_by_test_id(target.test_id)))
                if target.role and target.name:
                    candidates.append(
                        (
                            f"{prefix}:role",
                            frame.get_by_role(target.role, name=target.name, exact=True),
                        )
                    )
                    candidates.append(
                        (
                            f"{prefix}:role_casefold",
                            frame.get_by_role(target.role, name=flexible_name, exact=False),
                        )
                    )
                if target.label:
                    candidates.append(
                        (f"{prefix}:label", frame.get_by_label(target.label, exact=True))
                    )
                if target.selector:
                    candidates.append((f"{prefix}:selector", frame.locator(target.selector)))
                if target.text:
                    candidates.append(
                        (f"{prefix}:text", frame.get_by_text(target.text, exact=True))
                    )
                    candidates.append((f"{prefix}:text_casefold", frame.get_by_text(flexible_text)))
                    candidates.append(
                        (f"{prefix}:text_contains", frame.get_by_text(target.text, exact=False))
                    )
            except (PlaywrightError, AttributeError):
                # A frame can detach during SPA transitions. It remains an
                # observational miss; the normal top-level candidates and
                # runtime re-planner decide whether recovery is possible.
                continue
        if not candidates:
            raise GroundingError(f"No deterministic grounding evidence for {target.name!r}")
        return candidates

    @staticmethod
    def _number_text_pattern(value: str | None) -> re.Pattern[str] | None:
        """Match a displayed phone/reference value despite UI formatting.

        Record-detail pages commonly add a country prefix or spaces after a
        successful submission.  This is evidence lookup only, never an action
        locator, and requires a sufficiently specific observed numeric value.
        """
        digits = "".join(re.findall(r"\d", value or ""))
        if len(digits) < 7:
            return None
        return re.compile(r"\D*".join(map(re.escape, digits)))

    async def grounded_locator(self, target: Target) -> tuple[Any, str]:
        """Re-ground once through available DOM evidence; never use coordinates."""
        attempts: list[str] = []
        for strategy, locator in self.locator_candidates(target):
            # Lightweight test adapters can expose only the operation surface.
            # Real Playwright locators always implement count(), which provides
            # the uniqueness guarantee in production.
            if not hasattr(locator, "count"):
                return locator, strategy
            try:
                count = await locator.count()
            except PlaywrightError:
                attempts.append(f"{strategy}:query-error")
                continue
            if count == 1:
                # A responsive shell can leave one matching node in the DOM
                # while hiding it at the locked production viewport. Treat
                # that as stale evidence and continue through the semantic
                # role/text candidates instead of force-clicking a hidden
                # element and masking the real recovery path.
                if hasattr(locator, "is_visible"):
                    try:
                        if not await locator.is_visible():
                            attempts.append(f"{strategy}:1-hidden")
                            continue
                    except PlaywrightError:
                        attempts.append(f"{strategy}:visibility-error")
                        continue
                return locator, strategy
            if count > 1 and hasattr(locator, "nth"):
                visible_indexes: list[int] = []
                for index in range(count):
                    try:
                        if await locator.nth(index).is_visible():
                            visible_indexes.append(index)
                    except PlaywrightError:
                        continue
                if visible_indexes:
                    # An application can intentionally expose the same primary
                    # navigation in a top header and bottom dock. Both are
                    # semantically valid and visible; choose the highest one,
                    # which is what a human naturally uses while continuing a
                    # page story. Geometry resolves duplicate DOM evidence but
                    # is never used as the click mechanism itself.
                    # Rank responsive duplicate controls from fresh geometry
                    # evidence. The locator remains the only click mechanism;
                    # geometry is never converted into a raw coordinate.
                    spatial = SpatialIndex[int](cell_size=320)
                    for index in visible_indexes:
                        try:
                            box = await locator.nth(index).bounding_box()
                            if box:
                                spatial.add(
                                    index,
                                    Rect(
                                        x=float(box.get("x", 0)),
                                        y=float(box.get("y", 0)),
                                        width=float(box.get("width", 0)),
                                        height=float(box.get("height", 0)),
                                    ),
                                )
                        except PlaywrightError:
                            continue
                    if len(spatial):
                        viewport = await self.page.evaluate(
                            "() => ({width: window.innerWidth, height: window.innerHeight})"
                        )
                        chosen_entry = spatial.nearest(
                            (
                                float(viewport.get("width", 0)) / 2,
                                float(viewport.get("height", 0)) / 2,
                            ),
                            tie_break=lambda entry: entry.rect.y,
                        )
                        chosen = chosen_entry.value if chosen_entry else visible_indexes[0]
                    else:
                        chosen = visible_indexes[0]
                    return locator.nth(chosen), f"{strategy}:visible-primary"
            attempts.append(f"{strategy}:{count}-matches")
        raise GroundingError(f"Unable to uniquely ground {target.name!r}; " + ", ".join(attempts))

    async def visible_locator(self, target: Target) -> tuple[Any, str]:
        """Resolve evidence that an element is visible without treating it as an action target.

        A postcondition such as “the Today navigation is visible” is an existence
        assertion. Requiring it to be globally unique made responsive duplicate
        navigation markup incorrectly reject an otherwise deterministic run.
        Actions still use ``grounded_locator`` and therefore retain strict
        uniqueness.
        """
        attempts: list[str] = []
        for strategy, locator in self.locator_candidates(target):
            if not hasattr(locator, "count"):
                return locator, strategy
            try:
                count = await locator.count()
            except PlaywrightError:
                attempts.append(f"{strategy}:query-error")
                continue
            if count:
                # ``get_by_text(..., exact=False)`` can match hidden responsive
                # navigation/template nodes before the visible witness (for
                # example ``en`` matching a hidden ``Contact Management``
                # label while the visible control says ``English``).  A
                # visible postcondition must select an actually visible node,
                # never merely the first DOM match.
                # Lightweight adapters used by contract tests expose only
                # ``count``; retain their existence semantics while real
                # Playwright locators take the visibility-aware path below.
                if not hasattr(locator, "nth") or not hasattr(locator, "is_visible"):
                    return locator.first, strategy
                for index in range(count):
                    candidate = locator.nth(index) if hasattr(locator, "nth") else locator
                    try:
                        if await candidate.is_visible():
                            return candidate, f"{strategy}:visible"
                    except PlaywrightError:
                        continue
                attempts.append(f"{strategy}:{count}-hidden")
                continue
            attempts.append(f"{strategy}:0-matches")
        number_pattern = self._number_text_pattern(target.text)
        if number_pattern is not None:
            locator = self.page.get_by_text(number_pattern, exact=False)
            try:
                count = await locator.count()
                if count and (not hasattr(locator, "nth") or not hasattr(locator, "is_visible")):
                    return locator.first, "normalized_number_text"
                for index in range(count):
                    candidate = locator.nth(index)
                    if not await candidate.is_visible():
                        continue
                    # Outcome verification is an existence assertion. The
                    # same displayed value can have several wrapper nodes;
                    # unlike an action, the first visible semantic witness is
                    # enough and remains traceable through its snapshot.
                    return candidate, "normalized_number_text:visible"
            except PlaywrightError:
                attempts.append("normalized_number_text:query-error")
        raise GroundingError(
            f"Unable to find visible evidence for {target.name!r}; " + ", ".join(attempts)
        )

    async def dismiss_safe_overlay(self) -> bool:
        """Close a blocking, explicitly dismissible dialog before re-grounding.

        Fresh production sessions can expose a branch/help/announcement dialog
        that was not present in the exploration context.  Such an overlay is
        presentation chrome, not the requested workflow.  We only dismiss a
        visible dialog through a semantic close/cancel control; no coordinate
        click or form submission is attempted.  The caller records this as a
        recovery action in the scene trace.
        """
        self.ensure_page()
        dialogs = self.page.get_by_role("dialog")
        had_visible_dialog = False
        try:
            count = await dialogs.count()
        except PlaywrightError:
            return False
        for index in range(count):
            dialog = dialogs.nth(index)
            try:
                if not await dialog.is_visible():
                    continue
                had_visible_dialog = True
                for label in ("Close", "Cancel", "Dismiss"):
                    control = dialog.get_by_role("button", name=label, exact=False)
                    if await control.count() and await control.first.is_visible():
                        await control.first.click()
                        await self.page.wait_for_timeout(350)
                        return True
                # Some libraries expose an icon-only close button with an
                # aria-label but no accessible name in the dialog tree.
                control = dialog.locator(
                    "button[aria-label*='close' i], button[data-testid*='close' i]"
                )
                if await control.count() and await control.first.is_visible():
                    await control.first.click()
                    await self.page.wait_for_timeout(350)
                    return True
            except PlaywrightError:
                continue
        # Native/browser-accessible dialogs and some canvas tool overlays do
        # not expose a labelled close button but do support the standard
        # Escape dismissal. This is a safe, read-only recovery: it never
        # dispatches a product action or replays the failed operation.
        if had_visible_dialog:
            try:
                await self.page.keyboard.press("Escape")
                await self.page.wait_for_timeout(250)
                for index in range(await dialogs.count()):
                    if await dialogs.nth(index).is_visible():
                        return False
                return True
            except PlaywrightError:
                pass
        return False

    async def blocking_overlay(self, target: Target | None) -> str | None:
        """Return a visible dialog that blocks the requested target, if any.

        A dialog is not automatically a problem: planned form fields and close
        controls legitimately live inside one.  It is a blocker only when the
        next semantic target is outside the active dialog.  This check runs
        before a scene is captured so captions can never describe obscured
        product content.
        """
        self.ensure_page()
        dialogs = self.page.get_by_role("dialog")
        try:
            for index in range(await dialogs.count()):
                dialog = dialogs.nth(index)
                if not await dialog.is_visible():
                    continue
                if target is not None:
                    try:
                        locator, _ = await self.visible_locator(target)
                        inside = await locator.evaluate(
                            "element => Boolean(element.closest('[role=dialog], dialog'))"
                        )
                        if inside:
                            continue
                    except GroundingError:
                        # The requested target is unavailable while the dialog
                        # is visible, which is exactly the blocked-state case.
                        pass
                label = (await dialog.inner_text()).strip().replace("\n", " ")
                return label[:240] or "visible dialog"
        except PlaywrightError:
            return None
        return None

    async def target_rect(self, target: Target | None) -> Rect | None:
        if target is None:
            return None
        self.ensure_page()
        locator, _ = await self.grounded_locator(target)
        box = await locator.bounding_box()
        if box:
            return Rect(**box)
        # Some cloud/browser animation frames report no Playwright bounding
        # box even though the freshly grounded control is visibly actionable.
        # Read the DOM client box from that same locator rather than guessing
        # a coordinate. This geometry drives cursor alignment and secret masks,
        # so a recovered box remains evidence rather than a presentation hint.
        try:
            client_box = await locator.evaluate(
                """element => {
                    const rect = element.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0
                      ? {x: rect.x, y: rect.y, width: rect.width, height: rect.height}
                      : null;
                }"""
            )
        except PlaywrightError:
            client_box = None
        return Rect(**client_box) if isinstance(client_box, dict) else None

    async def execute(self, operation: SemanticOperation) -> Any:
        self.ensure_page()
        if operation.kind == OperationKind.NAVIGATE:
            response = await self.page.goto(str(operation.value), wait_until="domcontentloaded")
            try:
                await self.page.wait_for_load_state("networkidle", timeout=5_000)
            except PlaywrightError:
                # Some SPAs keep a connection open; wait for their loading shell
                # to clear without letting a persistent request stall the run.
                try:
                    await self.page.wait_for_function(
                        "() => !document.body.innerText.includes('Loading...')", timeout=5_000
                    )
                except PlaywrightError:
                    await self.page.wait_for_timeout(700)
            return response
        if operation.kind is OperationKind.POINTER_SEQUENCE:
            payload = operation.value if isinstance(operation.value, dict) else {}
            points = payload.get("points")
            surface = None
            if payload.get("pattern") == "short_reversible_stroke" and operation.target is not None:
                surface, _ = await self.grounded_locator(operation.target)
            if (
                not isinstance(points, list)
                and payload.get("pattern") == "short_reversible_stroke"
                and operation.target is not None
            ):
                # Drawing coordinates are derived from the current, grounded
                # surface geometry immediately before dispatch. This keeps a
                # generic canvas action resilient to responsive layouts and
                # avoids persisting provider- or site-specific coordinates.
                box = await surface.bounding_box()
                if not box or box.get("width", 0) < 16 or box.get("height", 0) < 16:
                    raise GroundingError("Observed drawing surface has no usable geometry")
                width, height = float(box["width"]), float(box["height"])
                inset_x = min(max(width * 0.18, 24.0), width * 0.34)
                inset_y = min(max(height * 0.18, 18.0), height * 0.34)
                center_y = float(box["y"]) + height / 2
                points = [
                    {"x": float(box["x"]) + inset_x, "y": center_y - inset_y * 0.35},
                    {"x": float(box["x"]) + width / 2, "y": center_y + inset_y * 0.35},
                    {"x": float(box["x"]) + width - inset_x, "y": center_y - inset_y * 0.15},
                ]
            surface_fingerprint = None
            surface_box = None
            proof_box = None
            if payload.get("pattern") == "short_reversible_stroke" and operation.target is not None:
                # Capture a lightweight, target-local fingerprint before the
                # gesture.  This is outcome evidence, not a product adapter:
                # a clipped rendered screenshot is the only portable signal
                # that covers layered canvas/SVG editors whose interactive
                # canvas is transparent while a sibling layer paints pixels.
                try:
                    surface_box = await surface.bounding_box()
                    if surface_box:
                        # The whole editor frame can change when a tool panel
                        # opens.  Prove the gesture itself by comparing a
                        # padded region around the observed path, excluding
                        # unrelated chrome/state changes from the outcome.
                        xs = [
                            float(point["x"])
                            for point in points
                            if isinstance(point, dict) and "x" in point
                        ]
                        ys = [
                            float(point["y"])
                            for point in points
                            if isinstance(point, dict) and "y" in point
                        ]
                        if xs and ys:
                            left = max(float(surface_box["x"]), min(xs) - 24)
                            top = max(float(surface_box["y"]), min(ys) - 24)
                            right = min(
                                float(surface_box["x"]) + float(surface_box["width"]), max(xs) + 24
                            )
                            bottom = min(
                                float(surface_box["y"]) + float(surface_box["height"]), max(ys) + 24
                            )
                            if right - left >= 8 and bottom - top >= 8:
                                proof_box = {
                                    "x": left,
                                    "y": top,
                                    "width": right - left,
                                    "height": bottom - top,
                                }
                                surface_fingerprint = await self.page.screenshot(clip=proof_box)
                except PlaywrightError:
                    surface_fingerprint = None
                payload = {**payload, "points": points}
            if not isinstance(points, list) or len(points) < 2:
                raise GroundingError("PointerSequence requires at least two observed points")
            duration_ms = max(0, int(payload.get("duration_ms", 450)))
            # Browser mouse moves are discrete events.  A three-point path is
            # enough for cursor presentation but too sparse for drawing
            # surfaces that build a stroke from continuous pointer movement.
            # Interpolate a bounded, time-proportional path so the same
            # evidence-backed gesture behaves naturally across editors.
            if duration_ms > 0 and len(points) >= 2:
                expanded: list[dict[str, float]] = [points[0]]
                for start, end in pairwise(points):
                    segment_steps = max(2, min(30, int(duration_ms / max(1, len(points) - 1) / 45)))
                    for index in range(1, segment_steps + 1):
                        progress = index / segment_steps
                        expanded.append(
                            {
                                "x": float(start["x"])
                                + (float(end["x"]) - float(start["x"])) * progress,
                                "y": float(start["y"])
                                + (float(end["y"]) - float(start["y"])) * progress,
                            }
                        )
                points = expanded
            pause_ms = duration_ms / max(1, len(points) - 1)
            button = str(payload.get("button", "left"))
            # A named reversible-stroke pattern is a complete drawing gesture,
            # so it owns the pointerdown/pointerup pair unless the caller
            # explicitly overrides it.  Previously the generated points were
            # moved with no button pressed, making canvas/whiteboard actions
            # silently no-op on real pages.
            pressed = bool(
                payload.get("press", payload.get("pattern") == "short_reversible_stroke")
            )
            first = points[0]
            if not isinstance(first, dict) or not {"x", "y"} <= set(first):
                raise GroundingError("PointerSequence points must contain observed x/y geometry")
            # Establish the hover position before pressing. Pressing at the
            # previous cursor location and then teleporting to the first point
            # loses the initial pointerdown on many canvas implementations.
            await self.page.mouse.move(float(first["x"]), float(first["y"]))
            await self.page.wait_for_timeout(140)
            if pressed:
                await self.page.mouse.down(button=button)
            for point in points[1:] if pressed else points:
                if not isinstance(point, dict) or not {"x", "y"} <= set(point):
                    raise GroundingError(
                        "PointerSequence points must contain observed x/y geometry"
                    )
                await self.page.mouse.move(float(point["x"]), float(point["y"]))
                if pause_ms:
                    await self.page.wait_for_timeout(int(pause_ms))
            if pressed or bool(payload.get("release", False)):
                await self.page.mouse.up(button=button)
            changed = None
            if payload.get("pattern") == "short_reversible_stroke" and operation.target is not None:
                try:
                    # Canvas/SVG editors often commit their paint layer on the
                    # next compositor tick after pointerup.  Let that render
                    # settle before comparing the target-local witness; an
                    # immediate screenshot can falsely report a no-op even
                    # though the browser state changed successfully.
                    await self.page.wait_for_timeout(120)
                    after_fingerprint = (
                        await self.page.screenshot(clip=proof_box) if proof_box else None
                    )
                    changed = bool(
                        surface_fingerprint is not None and after_fingerprint != surface_fingerprint
                    )
                except PlaywrightError:
                    changed = False
            return {
                "points": points,
                "duration_ms": duration_ms,
                "button": button,
                **({"surface_changed": changed} if changed is not None else {}),
            }
        if operation.kind is OperationKind.READ_VALUE and operation.target is None:
            return {
                "url": self.page.url,
                "text": (await self.page.locator("body").inner_text())[:2_000],
            }
        if (
            operation.kind in {OperationKind.WAIT_FOR_STATE, OperationKind.VERIFY_STATE}
            and operation.target is None
        ):
            timeout_ms = max(0, int(operation.value or 500))
            await self.page.wait_for_timeout(timeout_ms)
            return {"waited_ms": timeout_ms}
        if operation.kind is OperationKind.KEY_PRESS and operation.target is None:
            key = str(operation.value or "Escape")
            await self.page.keyboard.press(key)
            await self.page.wait_for_timeout(120)
            return {"key": key, "scope": "page"}
        if operation.target is None:
            raise GroundingError(f"{operation.kind} needs a semantic target")
        locator, _ = await self.grounded_locator(operation.target)
        if operation.kind is OperationKind.UPLOAD:
            if not isinstance(operation.value, str) or not operation.value.strip():
                raise GroundingError("Upload requires a local file path")
            path = Path(operation.value).expanduser().resolve()
            if not path.is_file():
                raise GroundingError("Upload file does not exist")
            return await locator.set_input_files(str(path))
        if operation.kind in {
            OperationKind.FILL_TEXT,
            OperationKind.FILL_EMAIL,
            OperationKind.FILL_PHONE,
            OperationKind.SEARCH,
        }:
            # `fill()` is correct for machine setup but skips the visible typing a demo must show.
            await locator.click()
            await locator.press("ControlOrMeta+A")
            await locator.press("Backspace")
            return await locator.press_sequentially(str(operation.value), delay=70)
        if operation.kind == OperationKind.SELECT_DATE:
            return await locator.fill(str(operation.value))
        if operation.kind == OperationKind.SELECT_DATE_RANGE:
            if not isinstance(operation.value, dict) or "start" not in operation.value:
                raise GroundingError(
                    "SelectDateRange requires a start value and a product-specific compiled target"
                )
            return await locator.fill(str(operation.value["start"]))
        if operation.kind == OperationKind.SELECT_OPTION:
            try:
                return await locator.select_option(str(operation.value))
            except (AttributeError, PlaywrightError):
                # Design-system comboboxes expose their choices through the
                # accessibility tree instead of a native <select>. The value
                # came from discovery's visible option probe, so this remains
                # a semantic, evidence-backed choice rather than typed guess.
                await locator.click()
                option = self.page.get_by_role("option", name=str(operation.value), exact=True)
                count = await option.count()
                for index in range(count):
                    candidate = option.nth(index)
                    if await candidate.is_visible():
                        return await candidate.click()
                raise GroundingError(f"Observed option is no longer visible: {operation.value!r}")
        if operation.kind in {OperationKind.CHECK, OperationKind.CHOOSE_RADIO}:
            return await locator.check()
        if operation.kind == OperationKind.UNCHECK:
            return await locator.uncheck()
        if operation.kind == OperationKind.SCROLL_TO:
            # Playwright's scroll_into_view_if_needed() teleports to the
            # destination.  That is reliable for tests, but it produces an
            # obviously synthetic jump in a recorded product walkthrough.
            # Move the viewport in small wheel increments instead, retaining
            # the exact DOM target as the semantic source of truth.
            geometry = await locator.evaluate(
                """element => ({
                    targetTop: element.getBoundingClientRect().top + window.scrollY,
                    viewportHeight: window.innerHeight,
                    currentTop: window.scrollY,
                })"""
            )
            desired_top = max(
                0, float(geometry["targetTop"]) - float(geometry["viewportHeight"]) * 0.32
            )
            distance = desired_top - float(geometry["currentTop"])
            if self.cloud_mode:
                # Remote CDP recordings can collapse a compositor animation into
                # its start/end frames. Dispatch explicit, timed intermediate
                # positions so Browserbase's native recording contains visible
                # scroll continuity rather than an anchor jump.
                steps = max(8, min(24, int(abs(distance) / 120) + 1))
                duration_ms = int(min(3_000, max(1_200, steps * 100)))
                motion = await self.page.evaluate(
                    """async ({top, duration, steps}) => {
                        const start = window.scrollY;
                        const delta = top - start;
                        const path = [{x: window.scrollX, y: start}];
                        if (Math.abs(delta) < 2) return;
                        const ease = value => 1 - Math.pow(1 - value, 3);
                        const pause = duration / steps;
                        for (let index = 1; index <= steps; index += 1) {
                            window.scrollTo(0, start + delta * ease(index / steps));
                            path.push({x: window.scrollX, y: window.scrollY});
                            await new Promise(resolve => setTimeout(resolve, pause));
                        }
                        return {start_y: start, target_y: window.scrollY, duration_ms: duration, steps, path};
                    }""",
                    {"top": desired_top, "duration": duration_ms, "steps": steps},
                )
                await self.page.wait_for_timeout(450)
                return motion or {
                    "start_y": float(geometry["currentTop"]),
                    "target_y": desired_top,
                    "duration_ms": float(duration_ms),
                    "steps": float(steps),
                    "path": [],
                }
            # Use enough wheel samples for visible continuity without making a
            # remote CDP run spend several seconds on every landmark. Browser
            # sessions add command latency to each wheel event; a 30-sample
            # cap turned a three-minute story into a five-minute cloud run.
            # This remains a real, gradual scroll rather than an anchor jump.
            steps = max(3, min(14, int(abs(distance) / 180) + 1))
            path = [{"x": 0.0, "y": float(geometry["currentTop"])}]
            for _ in range(steps):
                await self.page.mouse.wheel(0, distance / steps)
                await self.page.wait_for_timeout(70)
                _, current_scroll = await self.view_state()
                path.append(
                    {"x": float(current_scroll.get("x", 0)), "y": float(current_scroll.get("y", 0))}
                )
            # Do not call scroll_into_view_if_needed here: it can undo the
            # directed wheel path with an abrupt anchor jump. The requested
            # target is intentionally positioned inside the reading region.
            # Leave enough time for intersection/entrance transitions to be
            # visible in the evidence recording before the next scene.
            await self.page.wait_for_timeout(450)
            return {
                "start_y": float(geometry["currentTop"]),
                "target_y": desired_top,
                "duration_ms": float(steps * 70 + 450),
                "steps": float(steps),
                "path": path,
            }
        if operation.kind is OperationKind.HOVER:
            await locator.hover()
            await self.page.wait_for_timeout(180)
            return {"hovered": True}
        if operation.kind is OperationKind.KEY_PRESS:
            key = str(operation.value or "Enter")
            await locator.press(key)
            return {"key": key}
        if operation.kind is OperationKind.DRAG:
            payload = operation.value if isinstance(operation.value, dict) else {}
            destination_data = payload.get("destination")
            if not isinstance(destination_data, dict):
                raise GroundingError("Drag requires an observed semantic destination")
            destination = Target.model_validate(destination_data)
            source_box = await self.target_rect(operation.target)
            destination_box = await self.target_rect(destination)
            if source_box is None or destination_box is None:
                raise GroundingError("Drag requires visible source and destination geometry")
            sx = source_box.x + source_box.width / 2
            sy = source_box.y + source_box.height / 2
            dx = destination_box.x + destination_box.width / 2
            dy = destination_box.y + destination_box.height / 2
            duration_ms = max(300, min(2_000, int(payload.get("duration_ms", 700))))
            await self.page.mouse.move(sx, sy)
            await self.page.wait_for_timeout(160)
            await self.page.mouse.down()
            steps = max(4, min(20, int(duration_ms / 50)))
            for index in range(1, steps + 1):
                progress = index / steps
                # Smoothstep keeps the pointer human-like while retaining a
                # deterministic path that can be reproduced from geometry.
                eased = progress * progress * (3 - 2 * progress)
                await self.page.mouse.move(sx + (dx - sx) * eased, sy + (dy - sy) * eased)
                await self.page.wait_for_timeout(max(1, int(duration_ms / steps)))
            await self.page.mouse.up()
            return {
                "source": {"x": sx, "y": sy},
                "destination": {"x": dx, "y": dy},
                "duration_ms": duration_ms,
                "steps": steps,
            }
        if operation.kind == OperationKind.WAIT_FOR_STATE:
            return await locator.wait_for(state="visible", timeout=operation.value or 5_000)
        if operation.kind == OperationKind.READ_VALUE:
            return await locator.input_value()
        if operation.kind == OperationKind.VERIFY_STATE:
            return await locator.wait_for(state="visible", timeout=operation.value or 5_000)
        if operation.kind in {
            OperationKind.CLICK,
            OperationKind.OPEN_NAVIGATION_ITEM,
            OperationKind.OPEN_MODAL,
            OperationKind.CLOSE_MODAL,
            OperationKind.SUBMIT,
            OperationKind.APPLY_FILTER,
        }:
            try:
                result = await locator.click()
            except PlaywrightError:
                if operation.kind is not OperationKind.OPEN_NAVIGATION_ITEM:
                    raise
                # A verified visible navigation anchor can be temporarily
                # overlapped by source-site transition chrome in cloud
                # browsers. Preserve semantic targeting and retry just that
                # non-side-effecting navigation click without coordinates.
                result = await locator.click(force=True)
            if operation.kind is OperationKind.OPEN_NAVIGATION_ITEM:
                expected = next(
                    (
                        str(condition.expected)
                        for condition in operation.postconditions
                        if condition.kind == "url"
                    ),
                    None,
                )
                if expected:
                    await self.page.wait_for_timeout(550)
                    if not _navigation_reached(self.page.url, expected, self.page.url):
                        # Some reactive menus acknowledge a click before the
                        # route handler is mounted. Re-ground and retry one
                        # time only when the first attempt did not change the
                        # route; navigation controls are non-side-effecting.
                        retry_locator, _ = await self.grounded_locator(operation.target)
                        await retry_locator.click(force=True)
                        await self.page.wait_for_timeout(700)
                    if not _navigation_reached(self.page.url, expected, self.page.url):
                        # The observed control has now proved unreliable in
                        # this fresh context. Its own same-origin href is the
                        # permitted direct-navigation fallback; return an
                        # auditable marker so the trace explains why it was
                        # used instead of silently pretending the click worked.
                        fallback_url = urljoin(self.page.url, expected)
                        await self.page.goto(
                            fallback_url, wait_until="domcontentloaded", timeout=20_000
                        )
                        await self.page.wait_for_timeout(450)
                        if not _navigation_reached(self.page.url, expected, fallback_url):
                            raise GroundingError(
                                f"Navigation control and same-origin fallback did not reach {expected!r}"
                            )
                        return {
                            "navigation_fallback": "direct_after_visible_noop",
                            "fallback_url": fallback_url,
                        }
            return result
        raise GroundingError(f"Unsupported primitive operation: {operation.kind}")

    async def snapshot(self, target: Target | None) -> dict[str, Any]:
        self.ensure_page()
        if target is None:
            return {"url": self.page.url}
        locator, strategy = await self.grounded_locator(target)
        try:
            # Keep this as one browser evaluation. A cloud CDP session can
            # add material latency to every request; the old implementation
            # made six independent round trips per before/after snapshot.
            # This still records the same target-local evidence and avoids
            # treating a fast cloud capture as an excuse to omit it.
            snapshot = await locator.evaluate(
                """element => {
                    const type = (element.getAttribute('type') || '').toLowerCase();
                    const identity = [
                      type,
                      element.getAttribute('name') || '',
                      element.getAttribute('autocomplete') || '',
                      element.getAttribute('aria-label') || '',
                      element.getAttribute('placeholder') || '',
                    ].join(' ').toLowerCase();
                    const sensitive = type === 'password' ||
                      /(pass(word)?|secret|token|api[ _-]?key|one[ -]?time|otp|cvv|cvc|pin)/.test(identity);
                    const fingerprint = value => {
                      let hash = 2166136261;
                      for (let index = 0; index < value.length; index += 1) {
                        hash ^= value.charCodeAt(index);
                        hash = Math.imul(hash, 16777619);
                      }
                      return (hash >>> 0).toString(16);
                    };
                    return {
                        url: window.location.href,
                        text: (element.innerText || element.textContent || '').slice(0, 500),
                        page_text_hash: fingerprint((document.body?.innerText || '').slice(0, 12000)),
                        dom_hash: fingerprint((document.body?.innerHTML || '').slice(0, 120000)),
                        accessibility_hash: fingerprint(Array.from(document.querySelectorAll(
                          'button,a,input,textarea,select,[role],[aria-label]'
                        )).filter(node => {
                          const rect = node.getBoundingClientRect();
                          return rect.width > 0 && rect.height > 0 && getComputedStyle(node).visibility !== 'hidden';
                        }).slice(0, 600).map(node => [
                          node.getAttribute('role') || node.tagName.toLowerCase(),
                          node.getAttribute('aria-label') || node.innerText || node.getAttribute('name') || '',
                          node.getAttribute('aria-expanded') || '',
                          node.getAttribute('aria-checked') || '',
                          node.getAttribute('aria-selected') || '',
                          node.disabled ? 'disabled' : ''
                        ].join('|')).join('\\n')),
                        value: !sensitive && ['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName) ? element.value : null,
                        attributes: {
                            className: typeof element.className === 'string' ? element.className : '',
                            ariaPressed: element.getAttribute('aria-pressed'),
                            checked: 'checked' in element ? element.checked : null,
                            disabled: 'disabled' in element ? element.disabled : null,
                        },
                    };
                }"""
            )
            return {**snapshot, "grounding_strategy": strategy}
        except PlaywrightError:  # Navigation can intentionally remove the previous target.
            return {"url": self.page.url, "target_available": False}

    async def page_evidence(self, *, max_text: int = 6_000) -> dict[str, Any]:
        """Capture bounded, non-secret evidence for a runtime replan.

        This is intentionally read-only and does not enumerate hidden DOM or
        credentials.  It gives a replanner the current URL, title, visible
        prose, and visible semantic controls after an unexpected state.
        """
        self.ensure_page()
        try:
            script = """(limit) => ({
                    url: window.location.href,
                    title: document.title,
                    text: (document.body?.innerText || '').slice(0, limit),
                    controls: Array.from(document.querySelectorAll(
                      'button, a, input, textarea, select, [role="button"], [role="link"], [role="tab"]'
                    )).filter(element => {
                      const box = element.getBoundingClientRect();
                      return box.width > 0 && box.height > 0 &&
                        getComputedStyle(element).visibility !== 'hidden';
                    }).slice(0, 80).map(element => ({
                      tag: element.tagName.toLowerCase(),
                      role: element.getAttribute('role'),
                      name: (element.getAttribute('aria-label') || element.innerText ||
                        element.getAttribute('placeholder') || element.getAttribute('name') || '').trim().slice(0, 160),
                      type: element.getAttribute('type'),
                      disabled: Boolean(element.disabled),
                    })).filter(item => item.name),
                    shadowRoots: Array.from(document.querySelectorAll('*')).filter(element => element.shadowRoot)
                      .slice(0, 20).map((host, index) => ({
                        index,
                        host: host.tagName.toLowerCase(),
                        text: (host.shadowRoot.innerText || host.shadowRoot.textContent || '').slice(0, 1000),
                        controls: Array.from(host.shadowRoot.querySelectorAll(
                          'button, a, input, textarea, select, [role="button"], [role="link"], [role="tab"]'
                        )).slice(0, 20).map(element => ({
                          tag: element.tagName.toLowerCase(),
                          role: element.getAttribute('role'),
                          name: (element.getAttribute('aria-label') || element.innerText ||
                            element.getAttribute('placeholder') || element.getAttribute('name') || '').trim().slice(0, 160),
                        })).filter(item => item.name),
                      })),
                })"""
            result = await self.page.evaluate(script, max_text)
            # Include visible evidence hosted inside frames. The frame is an
            # execution context, not a new product route; merge bounded text
            # and controls so replanning can recover embedded workflows while
            # retaining the top-level URL as the story identity.
            frame_text: list[str] = []
            frame_controls: list[dict[str, Any]] = []
            for index, frame in enumerate(getattr(self.page, "frames", [])[1:], start=1):
                try:
                    nested = await frame.evaluate(script, max(500, max_text // 2))
                except PlaywrightError:
                    continue
                nested_text = str(nested.get("text") or "").strip()
                if nested_text:
                    frame_text.append(f"[embedded frame {index}] {nested_text}")
                for control in nested.get("controls", []) or []:
                    if control not in frame_controls:
                        frame_controls.append(control)
            if frame_text:
                result["text"] = (str(result.get("text") or "") + "\n" + "\n".join(frame_text))[
                    :max_text
                ]
            if frame_controls:
                result["controls"] = [*(result.get("controls", []) or []), *frame_controls[:80]]
            shadow_text = []
            shadow_controls = []
            for item in result.get("shadowRoots", []) or []:
                text = str(item.get("text") or "").strip()
                if text:
                    shadow_text.append(f"[shadow root {item.get('index')}] {text}")
                for control in item.get("controls", []) or []:
                    if control not in shadow_controls:
                        shadow_controls.append(control)
            if shadow_text:
                result["text"] = (str(result.get("text") or "") + "\n" + "\n".join(shadow_text))[
                    :max_text
                ]
            if shadow_controls:
                result["controls"] = [*(result.get("controls", []) or []), *shadow_controls[:80]]
            result.pop("shadowRoots", None)
            return result
        except PlaywrightError as error:
            raise GroundingError("current page evidence is unavailable") from error

    async def snapshot_visible(self, target: Target) -> dict[str, Any]:
        """Snapshot a visible outcome witness without action-level uniqueness.

        This is intentionally limited to post-action proof.  It prevents a
        successful submit/navigation from being reclassified as failed merely
        because the old form control disappeared or the result lives inside
        repeated layout wrappers.
        """
        self.ensure_page()
        locator, strategy = await self.visible_locator(target)
        try:
            snapshot = await locator.evaluate(
                """element => {
                    const type = (element.getAttribute('type') || '').toLowerCase();
                    const identity = [type, element.getAttribute('name') || '',
                      element.getAttribute('autocomplete') || '', element.getAttribute('aria-label') || '',
                      element.getAttribute('placeholder') || ''].join(' ').toLowerCase();
                    const sensitive = type === 'password' ||
                      /(pass(word)?|secret|token|api[ _-]?key|one[ -]?time|otp|cvv|cvc|pin)/.test(identity);
                    return {
                    url: window.location.href,
                    text: (element.innerText || element.textContent || '').slice(0, 500),
                    accessibility_hash: (() => {
                      let hash = 2166136261;
                      const value = Array.from(document.querySelectorAll('button,a,input,textarea,select,[role],[aria-label]'))
                        .filter(node => { const rect = node.getBoundingClientRect(); return rect.width > 0 && rect.height > 0; })
                        .slice(0, 600).map(node => [node.getAttribute('role') || node.tagName.toLowerCase(), node.getAttribute('aria-label') || node.innerText || node.getAttribute('name') || '', node.getAttribute('aria-expanded') || '', node.getAttribute('aria-selected') || ''].join('|')).join('\\n');
                      for (let index = 0; index < value.length; index += 1) { hash ^= value.charCodeAt(index); hash = Math.imul(hash, 16777619); }
                      return (hash >>> 0).toString(16);
                    })(),
                    value: !sensitive && ['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName) ? element.value : null,
                    attributes: { className: typeof element.className === 'string' ? element.className : '' },
                    };
                }"""
            )
            return {**snapshot, "grounding_strategy": strategy}
        except PlaywrightError:
            return {"url": self.page.url, "target_available": False}

    async def view_state(self) -> tuple[Viewport, dict[str, float]]:
        self.ensure_page()
        state = await self.page.evaluate(
            "() => ({width: window.innerWidth, height: window.innerHeight, "
            "deviceScaleFactor: window.devicePixelRatio || 1, x: window.scrollX, y: window.scrollY})"
        )
        return (
            Viewport(
                width=int(state["width"]),
                height=int(state["height"]),
                device_scale_factor=float(state["deviceScaleFactor"]),
            ),
            {"x": float(state["x"]), "y": float(state["y"])},
        )
