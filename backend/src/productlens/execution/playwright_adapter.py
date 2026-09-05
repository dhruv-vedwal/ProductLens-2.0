"""Playwright adapter. Coordinates are captured as evidence, never used to execute."""

from __future__ import annotations

import re
from typing import Any

from playwright.async_api import Error as PlaywrightError

from productlens.contracts.models import OperationKind, Rect, SemanticOperation, Target, Viewport


class GroundingError(RuntimeError):
    pass


class PlaywrightAdapter:
    def __init__(self, page: Any, *, cloud_mode: bool = False):
        self.page = page
        self.cloud_mode = cloud_mode

    def locator(self, target: Target) -> Any:
        """Return the preferred deterministic locator for compatibility callers."""
        return self.locator_candidates(target)[0][1]

    def locator_candidates(self, target: Target) -> list[tuple[str, Any]]:
        """Return evidence-backed candidates in the reliability order.

        Coordinates are deliberately absent. Each candidate remains a normal
        Playwright locator, so actionability is still enforced at execution.
        """
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
            candidates.append(("role", self.page.get_by_role(target.role, name=target.name, exact=True)))
            candidates.append(("role_casefold", self.page.get_by_role(target.role, name=flexible_name, exact=False)))
        if target.label:
            candidates.append(("label", self.page.get_by_label(target.label, exact=True)))
        if target.text:
            candidates.append(("text", self.page.get_by_text(target.text, exact=True)))
            flexible_text = re.compile("^" + r"\\s+".join(re.escape(part) for part in target.text.split()) + "$", re.IGNORECASE)
            candidates.append(("text_casefold", self.page.get_by_text(flexible_text)))
            candidates.append(("text_contains", self.page.get_by_text(target.text, exact=False)))
        if target.selector:
            candidates.append(("selector", self.page.locator(target.selector)))
        if not candidates:
            raise GroundingError(f"No deterministic grounding evidence for {target.name!r}")
        return candidates

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
                    # A portfolio can intentionally expose the same primary
                    # navigation in a top header and bottom dock. Both are
                    # semantically valid and visible; choose the highest one,
                    # which is what a human naturally uses while continuing a
                    # page story. Geometry resolves duplicate DOM evidence but
                    # is never used as the click mechanism itself.
                    positioned: list[tuple[float, int]] = []
                    for index in visible_indexes:
                        try:
                            box = await locator.nth(index).bounding_box()
                            positioned.append((float(box["y"]) if box else float("inf"), index))
                        except PlaywrightError:
                            positioned.append((float("inf"), index))
                    chosen = min(positioned)[1]
                    return locator.nth(chosen), f"{strategy}:visible-primary"
            attempts.append(f"{strategy}:{count}-matches")
        raise GroundingError(
            f"Unable to uniquely ground {target.name!r}; " + ", ".join(attempts)
        )

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
                return locator.first, strategy
            attempts.append(f"{strategy}:0-matches")
        raise GroundingError(
            f"Unable to find visible evidence for {target.name!r}; " + ", ".join(attempts)
        )

    async def target_rect(self, target: Target | None) -> Rect | None:
        if target is None:
            return None
        locator, _ = await self.grounded_locator(target)
        box = await locator.bounding_box()
        return Rect(**box) if box else None

    async def execute(self, operation: SemanticOperation) -> Any:
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
        if operation.target is None:
            raise GroundingError(f"{operation.kind} needs a semantic target")
        locator, _ = await self.grounded_locator(operation.target)
        if operation.kind in {
            OperationKind.FILL_TEXT,
            OperationKind.FILL_EMAIL,
            OperationKind.FILL_PHONE,
            OperationKind.SEARCH,
        }:
            # `fill()` is correct for machine setup but skips the visible typing a demo must show.
            if operation.target.test_id == "combobox-input":
                return await locator.fill(str(operation.value))
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
            return await locator.select_option(str(operation.value))
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
            desired_top = max(0, float(geometry["targetTop"]) - float(geometry["viewportHeight"]) * 0.32)
            distance = desired_top - float(geometry["currentTop"])
            if self.cloud_mode:
                # Remote CDP recordings can collapse a compositor animation into
                # its start/end frames. Dispatch explicit, timed intermediate
                # positions so Browserbase's native recording contains visible
                # scroll continuity rather than an anchor jump.
                steps = max(8, min(24, int(abs(distance) / 120) + 1))
                duration_ms = int(min(3_000, max(1_200, steps * 100)))
                await self.page.evaluate(
                    """async ({top, duration, steps}) => {
                        const start = window.scrollY;
                        const delta = top - start;
                        if (Math.abs(delta) < 2) return;
                        const ease = value => 1 - Math.pow(1 - value, 3);
                        const pause = duration / steps;
                        for (let index = 1; index <= steps; index += 1) {
                            window.scrollTo(0, start + delta * ease(index / steps));
                            await new Promise(resolve => setTimeout(resolve, pause));
                        }
                    }""",
                    {"top": desired_top, "duration": duration_ms, "steps": steps},
                )
                await self.page.wait_for_timeout(450)
                return {
                    "start_y": float(geometry["currentTop"]),
                    "target_y": desired_top,
                    "duration_ms": float(duration_ms),
                    "steps": float(steps),
                }
            # Use enough wheel samples for visible continuity without making a
            # remote CDP run spend several seconds on every landmark. Browser
            # sessions add command latency to each wheel event; a 30-sample
            # cap turned a three-minute story into a five-minute cloud run.
            # This remains a real, gradual scroll rather than an anchor jump.
            steps = max(3, min(14, int(abs(distance) / 180) + 1))
            for _ in range(steps):
                await self.page.mouse.wheel(0, distance / steps)
                await self.page.wait_for_timeout(70)
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
                return await locator.click()
            except PlaywrightError:
                if operation.kind is not OperationKind.OPEN_NAVIGATION_ITEM:
                    raise
                # A verified visible navigation anchor can be temporarily
                # overlapped by source-site transition chrome in cloud
                # browsers. Preserve semantic targeting and retry just that
                # non-side-effecting navigation click without coordinates.
                return await locator.click(force=True)
        raise GroundingError(f"Unsupported primitive operation: {operation.kind}")

    async def snapshot(self, target: Target | None) -> dict[str, Any]:
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
                    const log = document.querySelector('[data-testid="event-log"]');
                    return {
                        url: window.location.href,
                        text: (element.innerText || element.textContent || '').slice(0, 500),
                        value: ['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName) ? element.value : null,
                        attributes: {
                            className: typeof element.className === 'string' ? element.className : '',
                            ariaPressed: element.getAttribute('aria-pressed'),
                            checked: 'checked' in element ? element.checked : null,
                            disabled: 'disabled' in element ? element.disabled : null,
                        },
                        visible_event_log: (log?.innerText || '').slice(-500),
                    };
                }"""
            )
            return {**snapshot, "grounding_strategy": strategy}
        except PlaywrightError:  # Navigation can intentionally remove the previous target.
            return {"url": self.page.url, "target_available": False}

    async def view_state(self) -> tuple[Viewport, dict[str, float]]:
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
