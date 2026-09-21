"""Playwright adapter. Coordinates are captured as evidence, never used to execute."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from playwright.async_api import Error as PlaywrightError
from pydantic import ValidationError

from app.contracts.models import OperationKind, Rect, SemanticOperation, Target, Viewport
from app.execution.spatial_index import SpatialIndex


class GroundingError(RuntimeError):
    pass


def _route_key(value: str) -> tuple[str, str, str]:
    parsed = urlsplit(value)
    scheme = "https" if parsed.scheme in {"http", "https"} else parsed.scheme
    return scheme, parsed.netloc.casefold(), unquote(parsed.path).rstrip("/") or "/"


def _canonical_date(value: str) -> str | None:
    """Normalize a typed or widget date to ISO so locale display is not the proof."""

    cleaned = " ".join(str(value or "").strip().split())
    if not cleaned:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y%m%d", "%m%d%Y", "%d%m%Y"):
        try:
            parsed = datetime.strptime(cleaned, fmt).replace(tzinfo=UTC).date()
        except ValueError:
            continue
        return parsed.isoformat()
    tokens = [token for token in re.split(r"[-/.\s]+", cleaned) if token.isdigit()]
    if len(tokens) == 3 and all(len(token) <= 4 for token in tokens):
        year, month, day = tokens[0], tokens[1], tokens[2]
        if len(tokens[0]) != 4:
            day, month, year = tokens[0], tokens[1], tokens[2]
        try:
            return date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            return None
    return None


def _canonical_date_from_visible_label(value: str) -> str | None:
    """Parse a date embedded in a calendar's accessible/display label.

    Date widgets are not consistent across design systems: one exposes an ISO
    ``data-date``, another exposes ``aria-label="September 30, 2026"`` and a
    third uses ``30 Sep 2026``.  The resolver must ground against the observed
    label rather than assume a particular widget or selector.
    """
    text = " ".join(str(value or "").strip().split())
    if not text:
        return None
    direct = _canonical_date(text)
    if direct:
        return direct
    # Strip surrounding weekday/semantic words while retaining the date.  The
    # regex intentionally accepts only real month names and a four-digit year;
    # bare day numbers remain ambiguous and are handled by the calendar-state
    # fallback below.
    month = r"(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    patterns = (
        rf"\b{month}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,|\s)+\d{{4}}\b",
        rf"\b\d{{1,2}}\s+{month}(?:,|\s)+\d{{4}}\b",
    )
    formats = (
        ("%B %d %Y", "%b %d %Y"),
        ("%d %B %Y", "%d %b %Y"),
    )
    for pattern, date_formats in zip(patterns, formats):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = re.sub(r"(\d)(st|nd|rd|th)", r"\1", match.group(0), flags=re.IGNORECASE)
        candidate = candidate.replace(",", " ")
        for fmt in date_formats:
            try:
                return datetime.strptime(candidate, fmt).replace(tzinfo=UTC).date().isoformat()
            except ValueError:
                continue
    return None


def _canonical_time(value: str) -> str | None:
    """Normalize common 12/24-hour visible time values for verification."""
    text = re.sub(r"\s+", " ", str(value or "").strip()).upper()
    match = re.search(r"\b(\d{1,2})\s*[:.]\s*(\d{2})(?:\s*([AP]M))?\b", text)
    if match is None:
        digits = re.sub(r"\D", "", text)
        if len(digits) == 4:
            text = f"{digits[:2]}:{digits[2:]}"
            match = re.match(r"(\d{2}):(\d{2})", text)
    has_meridiem_group = match is not None and match.re.groups >= 3
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    meridiem = match.group(3) if has_meridiem_group else None
    if not 0 <= minute <= 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        if meridiem == "AM":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
    if not 0 <= hour <= 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def _visible_date_keystrokes(value: str) -> list[str]:
    iso = _canonical_date(value)
    if iso is None:
        return [value]
    year, month, day = iso.split("-")
    return list(
        dict.fromkeys(
            [
                iso,
                f"{month}{day}{year}",
                f"{day}{month}{year}",
                f"{month}/{day}/{year}",
                f"{day}-{month}-{year}",
                f"{year}{month}{day}",
            ]
        )
    )


def _navigation_reached(current: str, expected: str, base: str) -> bool:
    if expected.startswith("**/"):
        return current.endswith(expected[2:])
    return _route_key(current) == _route_key(urljoin(base, expected))


def _aria_role(value: str | None) -> str:
    """Normalize observed HTML tags to Playwright's ARIA role vocabulary."""
    raw = str(value or "").strip().casefold()
    return {
        "a": "link",
        "input": "textbox",
        "textarea": "textbox",
        "select": "combobox",
    }.get(raw, raw)


class PlaywrightAdapter:
    def __init__(
        self,
        page: Any,
        *,
        cloud_mode: bool = False,
        typing_delay_ms: int = 70,
        reconnect_page: Callable[[], Awaitable[Any | None]] | None = None,
    ):
        self.page = page
        self.cloud_mode = cloud_mode
        self.typing_delay_ms = max(0, int(typing_delay_ms))
        self._reconnect_page = reconnect_page
        # Canvas editors commonly remove focus from their transient text editor
        # when the browser takes a screenshot or the execution trace captures
        # a checkpoint between the placement gesture and the following typing
        # operation.  Keep the last *observed* placement for exactly that
        # hand-off; it is scoped to this adapter/page and is never an
        # application-specific coordinate or selector.
        self._pending_canvas_text_placement: dict[str, object] | None = None
        try:
            self._last_known_url = str(getattr(page, "url", "") or "")
        except (AttributeError, TypeError, RuntimeError, PlaywrightError):  # pragma: no cover
            self._last_known_url = ""

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

    async def ensure_page_async(self) -> Any:
        """Wait briefly for a remote CDP target hand-off after navigation."""
        if not self._page_is_closed(self.page):
            try:
                # A detached CDP target may report ``is_closed=False`` while
                # meaningful operations already raise TargetClosedError.
                if self.cloud_mode and hasattr(self.page, "title"):
                    await self.page.title()
                self._last_known_url = str(getattr(self.page, "url", "") or "")
            except (AttributeError, TypeError, RuntimeError, PlaywrightError):
                pass
            else:
                return self.page
        context = getattr(self.page, "context", None)
        for _ in range(40):
            pages = getattr(context, "pages", []) if context is not None else []
            for candidate in reversed(list(pages)):
                if not self._page_is_closed(candidate):
                    self.page = candidate
                    try:
                        self._last_known_url = str(getattr(candidate, "url", "") or "")
                    except (AttributeError, TypeError, RuntimeError, PlaywrightError):
                        return candidate
                    return candidate
            await asyncio.sleep(0.25)
        if self._reconnect_page is not None:
            try:
                replacement = await self._reconnect_page()
            except (PlaywrightError, RuntimeError, TypeError, TimeoutError):
                replacement = None
            if replacement is not None and not self._page_is_closed(replacement):
                self.page = replacement
                try:
                    self._last_known_url = str(getattr(replacement, "url", "") or "")
                except (AttributeError, TypeError, RuntimeError, PlaywrightError):
                    pass
                return replacement
        # Some CDP providers close the initial target when authentication
        # replaces the document and do not publish its replacement through
        # ``context.pages``. Re-open one context page and restore only the last
        # observed same-origin URL; cookies/storage remain owned by the
        # provider context. This is a bounded recovery for an explicit target
        # loss, never an extra opening navigation during a healthy run.
        if context is not None and self._last_known_url:
            try:
                replacement = await context.new_page()
                await replacement.goto(self._last_known_url, wait_until="domcontentloaded")
                self.page = replacement
                return replacement
            except (PlaywrightError, RuntimeError, TypeError):
                pass
        return self.ensure_page()

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
        page = await self.ensure_page_async()
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
        role = _aria_role(target.role)
        if role and target.name:
            candidates.append(
                ("role", self.page.get_by_role(role, name=target.name, exact=True))
            )
            candidates.append(
                (
                    "role_casefold",
                    self.page.get_by_role(role, name=flexible_name, exact=False),
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
                        role, name=target.name.replace("…", ""), exact=False
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
                            role,
                            name=re.compile(r"^" + re.escape(prefix) + r"\b", re.IGNORECASE),
                            exact=False,
                        ),
                    )
                )
        if target.label:
            candidates.append(("label", self.page.get_by_label(target.label, exact=True)))
            # Placeholder text is often the only stable semantic identity for
            # date/time and custom design-system inputs. Keep it separate from
            # CSS selectors so punctuation/Unicode in the placeholder cannot
            # make a captured selector invalid.
            try:
                candidates.append(
                    ("placeholder", self.page.get_by_placeholder(target.label, exact=True))
                )
            except (PlaywrightError, AttributeError):
                pass
        # A selector captured from the observed DOM is stronger evidence for
        # an action target than nearby descriptive text.  Forms commonly
        # render a label and its input as separate nodes (for example a
        # ``<span>Phone</span>`` beside ``<input name="phone">``).  Trying
        # text first can therefore click the label or wrapper and make a valid
        # plan fail in production.  Keep semantic role/label evidence first,
        # then use the observed selector, and only then fall back to text.
        if target.selector:
            # Canvas editors often render a full-size static backing canvas
            # beneath a second, pointer-active canvas.  A bare observed
            # ``canvas`` selector is intentionally preserved as the fallback,
            # but prefer the non-static layer when it is present so pointer
            # gestures reach the editor rather than its paint-only backdrop.
            # This is a rendering-semantic distinction, not an app adapter;
            # unknown editors still use the original selector if no such
            # layer exists.
            if target.selector.strip().casefold() == "canvas":
                candidates.append(("interactive_canvas", self.page.locator("canvas:not(.static)")))
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
                frame_role = _aria_role(target.role)
                if frame_role and target.name:
                    candidates.append(
                        (
                            f"{prefix}:role",
                            frame.get_by_role(frame_role, name=target.name, exact=True),
                        )
                    )
                    candidates.append(
                        (
                            f"{prefix}:role_casefold",
                            frame.get_by_role(frame_role, name=flexible_name, exact=False),
                        )
                    )
                if target.label:
                    candidates.append(
                        (f"{prefix}:label", frame.get_by_label(target.label, exact=True))
                    )
                    try:
                        candidates.append(
                            (
                                f"{prefix}:placeholder",
                                frame.get_by_placeholder(target.label, exact=True),
                            )
                        )
                    except (PlaywrightError, AttributeError):
                        pass
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
                            in_open_dialog = False
                            if hasattr(locator, "evaluate"):
                                try:
                                    in_open_dialog = bool(
                                        await locator.evaluate(
                                            """element => {
                                              const dialog = element.closest('dialog, [role="dialog"]');
                                              if (!dialog) return false;
                                              if (dialog.tagName === 'DIALOG') return Boolean(dialog.open);
                                              const style = getComputedStyle(dialog);
                                              return style.display !== 'none' && style.visibility !== 'hidden';
                                            }"""
                                        )
                                    )
                                except (AttributeError, PlaywrightError):
                                    in_open_dialog = False
                            if not in_open_dialog:
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
                    except (PlaywrightError, TypeError):
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
                    except (PlaywrightError, TypeError):
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
        await self.ensure_page_async()
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
        await self.ensure_page_async()
        dialogs = self.page.get_by_role("dialog")
        target_box: dict[str, float] | None = None
        if target is not None:
            try:
                target_locator, _ = await self.visible_locator(target)
                box = await target_locator.bounding_box()
                if box:
                    target_box = {
                        key: float(box.get(key, 0)) for key in ("x", "y", "width", "height")
                    }
            except (GroundingError, PlaywrightError):
                # An unavailable target is itself useful evidence below: an
                # open modal/popover may be intercepting the page.
                target_box = None
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
                        belongs = False
                        try:
                            if target.test_id:
                                belongs = bool(
                                    await dialog.get_by_test_id(target.test_id).count()
                                )
                            elif target.selector:
                                belongs = bool(await dialog.locator(target.selector).count())
                        except PlaywrightError:
                            belongs = False
                        if belongs:
                            continue
                        # The requested target is unavailable while the dialog
                        # is visible, which is exactly the blocked-state case.
                label = (await dialog.inner_text()).strip().replace("\n", " ")
                return label[:240] or "visible dialog"
        except PlaywrightError:
            pass

        # Many design systems do not use role=dialog.  Detect only visible,
        # elevated, pointer-intercepting surfaces that spatially overlap the
        # requested target.  This deliberately avoids class-name-only
        # heuristics: a candidate must have a fixed/absolute position, a
        # positive stacking context and an actual intersection with the target
        # (or cover the viewport when the target cannot be grounded).
        try:
            candidates = await self.page.evaluate(
                """(target) => {
                  const visible = node => {
                    const r = node.getBoundingClientRect();
                    const s = getComputedStyle(node);
                    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' &&
                      s.display !== 'none' && s.pointerEvents !== 'none';
                  };
                  const intersects = (a, b) => {
                    if (!b) return a.width >= innerWidth * .55 && a.height >= innerHeight * .35;
                    return a.left < b.x + b.width && a.right > b.x &&
                      a.top < b.y + b.height && a.bottom > b.y;
                  };
                  return Array.from(document.querySelectorAll(
                    '[aria-modal="true"],[data-state="open"],[role="listbox"],[role="menu"],
                    '[class*="modal" i],[class*="popover" i],[class*="dropdown" i],
                    '[class*="backdrop" i],[class*="overlay" i]'
                  )).filter(node => visible(node)).map(node => {
                    const r = node.getBoundingClientRect();
                    const s = getComputedStyle(node);
                    return { node, rect: {left:r.left, top:r.top, right:r.right, bottom:r.bottom,
                      width:r.width, height:r.height}, position:s.position,
                      zIndex: Number.parseInt(s.zIndex || '0', 10) || 0,
                      label:(node.getAttribute('aria-label') || node.innerText || '').trim().slice(0, 180) };
                  }).filter(item => ['fixed','absolute','sticky'].includes(item.position) &&
                    item.zIndex >= 1 && intersects(item.rect, target)).sort((a,b) => b.zIndex-a.zIndex)
                    .map(item => ({rect:item.rect, label:item.label, zIndex:item.zIndex}));
                }""",
                target_box,
            )
            if isinstance(candidates, list) and candidates:
                item = candidates[0]
                return str(item.get("label") or "visible overlay")[:240]
        except (PlaywrightError, TypeError):
            pass
        return None

    async def target_rect(self, target: Target | None) -> Rect | None:
        if target is None:
            return None
        await self.ensure_page_async()
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

    async def _select_date_human_visible(self, locator, value: str) -> dict[str, Any]:
        """Use the product's visible date control and fail if it cannot prove the value."""

        if not value.strip():
            raise GroundingError("SelectDate requires an observed date value")
        try:
            semantics = await locator.evaluate(
                """element => ({
                  tag: element.tagName.toLowerCase(),
                  type: (element.getAttribute('type') || '').toLowerCase(),
                  role: element.getAttribute('role') || '',
                  haspopup: element.getAttribute('aria-haspopup') || '',
                  readonly: Boolean(element.readOnly) || element.getAttribute('aria-readonly') === 'true',
                  placeholder: element.getAttribute('placeholder') || '',
                  name: element.getAttribute('name') || ''
                })"""
            )
        except (AttributeError, PlaywrightError):
            semantics = {}
        await locator.click()
        # A planner may encounter a time control in a form inventory before
        # the provider has classified its behavior. Keep the runtime generic:
        # resolve an editable time input by visible typing or an observed
        # visible slot, rather than treating ``10:30`` as a calendar date.
        if re.fullmatch(r"\d{1,2}:\d{2}(?:\s*[ap]m)?", value.strip(), re.IGNORECASE):
            if not semantics.get("readonly") and semantics.get("tag") in {"input", "textarea"}:
                try:
                    await locator.press("ControlOrMeta+A")
                    await locator.press_sequentially(value, delay=self.typing_delay_ms)
                    observed = await locator.evaluate("element => String(element.value || '')")
                except (AttributeError, PlaywrightError):
                    observed = ""
                if str(observed).strip() == value.strip():
                    return {
                        "selected": observed,
                        "interaction": "editable-time-visible-typing",
                        "focused": True,
                    }
            for role in ("option", "button", "gridcell"):
                try:
                    choices = self.page.get_by_role(
                        role, name=re.compile(rf"^{re.escape(value.strip())}$", re.IGNORECASE)
                    )
                    visible = [
                        choices.nth(index)
                        for index in range(await choices.count())
                        if await choices.nth(index).is_visible()
                    ]
                except (AttributeError, PlaywrightError):
                    visible = []
                if len(visible) == 1:
                    await visible[0].click()
                    return {"selected": value, "interaction": "observed-time-slot", "choice_role": role}
        is_native = semantics.get("tag") == "input" and semantics.get("type") == "date"
        if is_native and not semantics.get("readonly"):
            expected = _canonical_date(value) or value
            iso = _canonical_date(value)
            if iso:
                year, month, day = iso.split("-")
                try:
                    await locator.click()
                    for _ in range(3):
                        await locator.press("ArrowLeft")
                    for part in (month, day, year):
                        await locator.press_sequentially(part, delay=50)
                        await locator.press("ArrowRight")
                    observed = await locator.evaluate("element => String(element.value || '')")
                    if _canonical_date(str(observed)) == expected:
                        return {
                            "selected": observed,
                            "interaction": "native-date-visible-keyboard",
                            "focused": True,
                        }
                except (AttributeError, PlaywrightError):
                    pass
            for typed in _visible_date_keystrokes(value):
                try:
                    await locator.click()
                    await locator.press("Home")
                    await locator.press("ControlOrMeta+A")
                    await locator.press_sequentially(typed, delay=70)
                    observed = await locator.evaluate("element => String(element.value || '')")
                except (AttributeError, PlaywrightError):
                    observed = ""
                if _canonical_date(str(observed)) == expected or str(observed) == value:
                    return {
                        "selected": observed or expected,
                        "interaction": "native-date-visible-keyboard",
                        "focused": True,
                    }
            # Chromium's native date widget has no page-DOM calendar and does
            # not accept sequential keystrokes. After a visible click, commit
            # the ISO value so the demonstrated control retains the date.
            try:
                await locator.click()
                await locator.fill(expected if expected else value)
                observed = await locator.evaluate("element => String(element.value || '')")
            except (AttributeError, PlaywrightError):
                observed = ""
            if _canonical_date(str(observed)) == expected or str(observed) == value:
                return {
                    "selected": observed or expected,
                    "interaction": "native-date-visible-click-commit",
                    "focused": True,
                }
            try:
                await locator.press("Alt+ArrowDown")
            except (AttributeError, PlaywrightError):
                pass

        tokens = [token for token in re.split(r"[-/.\s]+", value) if token]
        # Some design systems render a calendar as an editable text input but
        # expose no queryable date cells (the popup may be a canvas or a
        # portal). In that case use visible sequential typing and verify the
        # normalized value before accepting it. Readonly controls still require
        # a grounded calendar choice; no product selector is assumed here.
        if not semantics.get("readonly") and semantics.get("tag") in {"input", "textarea"}:
            expected = _canonical_date(value) or value
            date_keystrokes = _visible_date_keystrokes(value)
            # Respect the observed display contract.  A text picker that
            # advertises ``dd-mm-yyyy`` may temporarily contain a canonical
            # ISO value while still being invalid; read back validity as well
            # as the normalized date before accepting the gesture.
            if re.search(r"dd[-/]mm[-/]yyyy", str(semantics.get("placeholder", "")), re.IGNORECASE):
                iso = _canonical_date(value)
                if iso:
                    year, month, day = iso.split("-")
                    preferred = f"{day}-{month}-{year}"
                    date_keystrokes = [preferred, *[item for item in date_keystrokes if item != preferred]]
            for typed in date_keystrokes:
                try:
                    await locator.click()
                    await locator.press("ControlOrMeta+A")
                    await locator.press_sequentially(typed, delay=self.typing_delay_ms)
                    observed_state = await locator.evaluate(
                        """element => ({
                          value: String(element.value || ''),
                          invalid: element.getAttribute('aria-invalid') === 'true' ||
                            Boolean(element.validity && element.validity.valid === false)
                        })"""
                    )
                    observed = str(observed_state.get("value", ""))
                except (AttributeError, PlaywrightError):
                    observed = ""
                    observed_state = {"invalid": True}
                if (
                    not observed_state.get("invalid")
                    and (_canonical_date(str(observed)) == expected or str(observed) == value)
                ):
                    return {
                        "selected": observed or value,
                        "interaction": "editable-date-visible-typing",
                        "focused": True,
                    }

        day = str(int(tokens[-1])) if len(tokens) >= 3 and tokens[-1].isdigit() else value
        scopes = [
            self.page.locator("[role='dialog']:visible").last,
            self.page.locator("[role='grid']:visible").last,
            self.page,
        ]
        expected_iso = _canonical_date(value)
        # Prefer an accessibility/data-date representation when available.
        # This handles custom calendars whose adjacent-month days share the
        # same visible number (for example two ``30`` buttons) without relying
        # on a product-specific class or selector.
        if expected_iso:
            for scope in scopes:
                try:
                    choices = scope.locator(
                        "[role='gridcell']:visible, [role='button']:visible, [role='option']:visible"
                    )
                    observed = await choices.evaluate_all(
                        """nodes => nodes.map((node, index) => ({
                          index,
                          disabled: node.hasAttribute('disabled') ||
                            node.getAttribute('aria-disabled') === 'true' ||
                            node.getAttribute('data-disabled') === 'true',
                          values: [
                            node.getAttribute('data-date'),
                            node.getAttribute('datetime'),
                            node.getAttribute('aria-label'),
                            node.getAttribute('title'),
                            node.textContent
                          ].filter(Boolean).map(String)
                        }))"""
                    )
                except (AttributeError, PlaywrightError):
                    observed = []
                matches = [
                    item
                    for item in observed
                    if not item.get("disabled")
                    and any(
                        _canonical_date_from_visible_label(label) == expected_iso
                        for label in item.get("values", [])
                    )
                ]
                if len(matches) == 1:
                    await choices.nth(int(matches[0]["index"])).click()
                    return {
                        "selected": value,
                        "interaction": "calendar-visible-date-label",
                        "choice_role": "calendar-control",
                    }
        for scope in scopes:
            for role in ("gridcell", "button", "option"):
                choices = scope.get_by_role(role, name=re.compile(rf"^(?:{re.escape(value)}|{day})$"))
                try:
                    visible = [
                        choices.nth(index)
                        for index in range(await choices.count())
                        if await choices.nth(index).is_visible()
                    ]
                except (AttributeError, PlaywrightError):
                    visible = []
                if len(visible) == 1:
                    await visible[0].click()
                    if is_native:
                        observed = ""
                        try:
                            observed = await locator.evaluate(
                                "element => String(element.value || '')"
                            )
                        except (AttributeError, PlaywrightError):
                            observed = value
                        expected = _canonical_date(value) or value
                        if _canonical_date(str(observed)) not in {expected, None} and str(
                            observed
                        ) not in {"", value}:
                            continue
                    return {
                        "selected": value,
                        "interaction": "calendar-visible-choice",
                        "choice_role": role,
                    }
        raise GroundingError(f"Date picker exposed no unique visible choice for {value!r}")

    async def _select_native_option_human_visible(
        self, locator, value: str
    ) -> dict[str, Any] | None:
        try:
            state = await locator.evaluate(
                """element => element.tagName === 'SELECT' ? {
                  native: true,
                  selectedIndex: element.selectedIndex,
                  options: Array.from(element.options).map((option, index) => ({
                    index, value: String(option.value || ''), label: String(option.label || option.textContent || '').trim(),
                    disabled: Boolean(option.disabled)
                  }))
                } : {native: false}"""
            )
        except (AttributeError, PlaywrightError):
            return None
        if not state.get("native"):
            return None
        enabled_options = [
            item
            for item in state.get("options", [])
            if not item.get("disabled")
        ]
        options = [
            item
            for item in enabled_options
            if value.casefold()
            in {str(item.get("value", "")).casefold(), str(item.get("label", "")).casefold()}
        ]
        if len(options) != 1:
            raise GroundingError(f"Native select has no unique observed option {value!r}")
        target_index = enabled_options.index(options[0])
        await locator.click()
        await locator.press("Home")
        for _ in range(target_index):
            await locator.press("ArrowDown")
            await self.page.wait_for_timeout(70)
        await locator.press("Enter")
        observed = await locator.evaluate(
            "element => ({value:String(element.value || ''), label:element.selectedOptions?.[0]?.textContent?.trim() || ''})"
        )
        if value.casefold() not in {
            str(observed.get("value", "")).casefold(),
            str(observed.get("label", "")).casefold(),
        }:
            raise GroundingError(f"Native select did not retain observed option {value!r}")
        return {
            "selected": value,
            "state": observed,
            "interaction": "native-select-visible-keyboard",
        }

    async def _fill_time_human_visible(self, locator, value: str) -> dict[str, Any]:
        """Enter a native time control through its visible segmented UI.

        Chromium exposes ``input[type=time]`` as multiple spinbutton segments.
        Sending the punctuation-bearing value to the currently focused segment
        is silently ignored.  Digits through the normal keyboard path advance
        those segments and retain the same human-visible typing semantics as
        every other form control; the resulting value is then verified.
        """
        await locator.click()
        await locator.press("ControlOrMeta+A")
        await locator.press("Backspace")
        expected = _canonical_time(value)
        if expected is None:
            raise GroundingError(f"Time input requires an observed time value: {value!r}")
        digits = expected.replace(":", "")
        await locator.press_sequentially(digits, delay=self.typing_delay_ms)
        observed = ""
        try:
            observed = str(await locator.evaluate("element => String(element.value || '')"))
        except (AttributeError, PlaywrightError):
            observed = ""
        if _canonical_time(observed) != expected:
            # A browser locale may accept punctuation after the segmented
            # attempt. This remains a visible keyboard retry, never a DOM
            # assignment or a product-specific adapter.
            await locator.press("ControlOrMeta+A")
            await locator.press("Backspace")
            await locator.press_sequentially(value, delay=self.typing_delay_ms)
            try:
                observed = str(await locator.evaluate("element => String(element.value || '')"))
            except (AttributeError, PlaywrightError):
                observed = ""
        if _canonical_time(observed) != expected:
            raise GroundingError(
                f"Time input did not retain observed value {value!r}; got {observed!r}"
            )
        return {
            "typed": True,
            "typing_started": True,
            "completed_value_checkpoint": observed,
            "interaction": "native-time-keyboard",
        }

    async def _visible_choice_value(self, locator, value: str) -> bool:
        """Check that a committed custom choice is visibly rendered nearby."""
        try:
            return bool(
                await locator.evaluate(
                    """(element, expected) => {
                      const normalize = input => String(input || '')
                        .replace(/\\s+/g, ' ').trim().toLocaleLowerCase();
                      const wanted = normalize(expected);
                      const matches = value => {
                        const text = normalize(value);
                        return Boolean(text && wanted && (text === wanted ||
                          text.startsWith(wanted + ' ') || wanted.startsWith(text + ' ')));
                      };
                      const direct = [
                        element.value,
                        element.textContent,
                        element.getAttribute?.('aria-label'),
                        element.getAttribute?.('aria-valuetext'),
                        element.getAttribute?.('data-value'),
                      ];
                      if (direct.some(matches)) return true;
                      const activeId = element.getAttribute?.('aria-activedescendant');
                      if (activeId) {
                        const active = document.getElementById(activeId);
                        if (active && matches(active.innerText || active.textContent ||
                          active.getAttribute?.('aria-label') || active.getAttribute?.('data-value'))) {
                          return true;
                        }
                      }
                      let node = element;
                      for (let depth = 0; node && depth < 10; depth += 1, node = node.parentElement) {
                        const role = String(node.getAttribute?.('role') || '').toLowerCase();
                        const className = String(node.className || '').toLowerCase();
                        const text = normalize(node.innerText || node.textContent || '');
                        const semantic = role === 'combobox' || role === 'listbox'
                          || /autocomplete|combobox|multiselect|select|chip|tag/.test(className)
                          || Boolean(node.querySelector?.('[role="option"], [data-tag], [data-value]'));
                        const ownsInput = node === element || Boolean(node.querySelector?.(
                          'input, textarea, [role="textbox"], [role="combobox"]'
                        ));
                        if ((semantic || ownsInput) && (matches(text) || text.includes(wanted)) &&
                          text.length <= (semantic ? 800 : 320)) return true;
                        if (semantic) {
                          const selected = node.querySelectorAll?.(
                            '[aria-selected="true"], [data-tag], [data-value], [role="option"]'
                          ) || [];
                          for (const item of selected) {
                            if (matches(item.innerText || item.textContent ||
                              item.getAttribute?.('aria-label') || item.getAttribute?.('data-value'))) {
                              return true;
                            }
                          }
                        }
                      }
                      return false;
                    }""",
                    str(value),
                )
            )
        except (AttributeError, PlaywrightError, TypeError):
            return False

    async def _choice_control_state(self, locator) -> str | None:
        """Return a compact pre/post state witness for custom choices."""
        try:
            state = await locator.evaluate(
                """element => {
                  const normalize = input => String(input || '')
                    .replace(/\\s+/g, ' ').trim().slice(0, 800);
                  let node = element;
                  for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
                    const controls = node.querySelectorAll?.(
                      'input, textarea, [role="textbox"], [role="combobox"]'
                    ) || [];
                    if (node === element || controls.length) {
                      return JSON.stringify({
                        value: normalize(element.value || element.textContent),
                        ariaValue: normalize(element.getAttribute?.('aria-valuetext')),
                        expanded: element.getAttribute?.('aria-expanded') || '',
                        active: element.getAttribute?.('aria-activedescendant') || '',
                        text: normalize(node.innerText || node.textContent),
                        selected: Array.from(node.querySelectorAll?.(
                          '[aria-selected="true"], [data-tag], [data-value]'
                        ) || []).map(item => normalize(
                          item.innerText || item.textContent || item.getAttribute?.('data-value')
                        )).filter(Boolean).slice(0, 24),
                      });
                    }
                  }
                  return JSON.stringify({
                    value: normalize(element.value || element.textContent),
                    ariaValue: normalize(element.getAttribute?.('aria-valuetext')),
                    expanded: element.getAttribute?.('aria-expanded') || '',
                    active: element.getAttribute?.('aria-activedescendant') || '',
                    text: normalize(element.innerText || element.textContent),
                    selected: [],
                  });
                }"""
            )
            return str(state)
        except (AttributeError, PlaywrightError, TypeError):
            return None

    async def execute(self, operation: SemanticOperation) -> Any:
        await self.ensure_page_async()
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
            pattern = str(payload.get("pattern", ""))
            if (
                pattern
                in {
                    "short_reversible_stroke",
                    "connector_segment",
                    "text_placement",
                    "shape_box",
                }
                and operation.target is not None
            ):
                surface, _ = await self.grounded_locator(operation.target)
            relative_points = payload.get("relative_points")
            if (
                not isinstance(points, list)
                and isinstance(relative_points, list)
                and operation.target is not None
            ):
                box = await surface.bounding_box()
                if not box or box.get("width", 0) < 16 or box.get("height", 0) < 16:
                    raise GroundingError("Observed drawing surface has no usable geometry")
                points = [
                    {
                        "x": float(box["x"]) + float(item["x"]) * float(box["width"]),
                        "y": float(box["y"]) + float(item["y"]) * float(box["height"]),
                    }
                    for item in relative_points
                    if isinstance(item, dict) and {"x", "y"} <= set(item)
                ]
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
            # Re-ground the semantic drawing tool immediately before a canvas
            # gesture. Editors may return to selection/text mode after focus
            # moves from the toolbar to the surface; a planned toolbar click
            # alone is therefore not a sufficient interaction guarantee.
            if pattern in {
                "connector_segment",
                "short_reversible_stroke",
                "text_placement",
                "shape_box",
            }:
                if pattern == "connector_segment":
                    tool_terms = ("arrow", "connector", "line")
                elif pattern == "text_placement":
                    tool_terms = ("text", "label")
                elif pattern == "shape_box":
                    tool_terms = ("rectangle", "box", "shape", "component")
                else:
                    tool_terms = ("draw", "pen", "freehand", "pencil")
                try:
                    controls = await self.page.locator("button,[role='button']").all()
                    for index, control in enumerate(controls):
                        if not await control.is_visible():
                            continue
                        metadata = await control.evaluate(
                            """node => ({
                              text: (node.innerText || '').toLowerCase(),
                              aria: (node.getAttribute('aria-label') || '').toLowerCase(),
                              title: (node.getAttribute('title') || '').toLowerCase(),
                              testid: (node.getAttribute('data-testid') || '').toLowerCase()
                            })"""
                        )
                        haystack = " ".join(
                            str(metadata.get(key, ""))
                            for key in ("text", "aria", "title", "testid")
                        )
                        if any(term in haystack for term in tool_terms):
                            await self.page.locator("button,[role='button']").nth(index).click()
                            await self.page.wait_for_timeout(180)
                            break
                except (PlaywrightError, TypeError):
                    # The planned click remains the evidence-backed fallback
                    # when an editor exposes no semantic toolbar metadata.
                    pass
            surface_fingerprint = None
            surface_structure = None
            surface_box = None
            proof_box = None
            if (
                pattern
                in {
                    "short_reversible_stroke",
                    "connector_segment",
                    "text_placement",
                    "shape_box",
                }
                and operation.target is not None
            ):
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
                    # A selection marquee or cursor highlight can change
                    # pixels without creating an editor element.  Where the
                    # page exposes an SVG/DOM scene graph, retain a compact
                    # structural fingerprint as stronger evidence of a
                    # committed edit; canvas-only editors continue to use
                    # the pixel witness above.
                    try:
                        surface_structure = await self.page.evaluate(
                            """() => ({
                              svg: Array.from(document.querySelectorAll('svg')).map(node => ({
                                count: node.querySelectorAll('*').length,
                                html: node.innerHTML.length
                              })),
                              canvas: document.querySelectorAll('canvas').length,
                              semantic_nodes: document.querySelectorAll(
                                '[data-testid],[data-shape],[data-element-id],[aria-label]'
                              ).length
                            })"""
                        )
                    except PlaywrightError:
                        surface_structure = None
                except PlaywrightError:
                    surface_fingerprint = None
                payload = {**payload, "points": points}
            if not isinstance(points, list) or len(points) < 2:
                raise GroundingError("PointerSequence requires at least two observed points")
            # Tool palettes, inspector drawers, and onboarding overlays often
            # sit above a full-viewport canvas.  The canvas bounding box alone
            # therefore does not prove that a point is dispatchable: in the
            # a planned gesture can begin beneath an open style panel and be
            # silently ignored. Rebase the observed
            # path to the nearest unobstructed region while preserving its
            # shape and relative geometry.  This is DOM/geometry based and
            # works for any canvas editor that exposes a canvas surface.
            if surface is not None and pattern in {
                "short_reversible_stroke",
                "connector_segment",
                "text_placement",
                "shape_box",
            }:
                try:
                    rebased = await surface.evaluate(
                        """
                        (surface, original) => {
                          const pts = Array.isArray(original) ? original : [];
                          const rect = surface.getBoundingClientRect();
                          if (!pts.length || rect.width < 16 || rect.height < 16) return null;
                          const allowed = (x, y) => {
                            const hit = document.elementFromPoint(x, y);
                            if (!hit) return false;
                            if (hit === surface || surface.contains(hit)) return true;
                            // Layered editors commonly put an interactive
                            // canvas over a paint canvas; accept that sibling
                            // only when it is itself an interactive canvas.
                            return hit.tagName === 'CANVAS' &&
                              hit.classList.contains('interactive');
                          };
                          const minX = Math.min(...pts.map(p => Number(p.x)));
                          const maxX = Math.max(...pts.map(p => Number(p.x)));
                          const minY = Math.min(...pts.map(p => Number(p.y)));
                          const maxY = Math.max(...pts.map(p => Number(p.y)));
                          const width = maxX - minX;
                          const height = maxY - minY;
                          const centerX = (minX + maxX) / 2;
                          const centerY = (minY + maxY) / 2;
                          const step = 24;
                          if (pts.every(p => allowed(Number(p.x), Number(p.y)))) {
                            return { points: pts, translated: false };
                          }
                          for (let dy = rect.top + 24 - (centerY - height / 2);
                               dy <= rect.bottom - 24 - (centerY + height / 2);
                               dy += step) {
                            for (let dx = rect.left + 24 - (centerX - width / 2);
                                 dx <= rect.right - 24 - (centerX + width / 2);
                                 dx += step) {
                              const candidate = pts.map(p => ({
                                x: Number(p.x) + dx,
                                y: Number(p.y) + dy,
                              }));
                              if (candidate.every(p =>
                                p.x >= rect.left && p.x <= rect.right &&
                                p.y >= rect.top && p.y <= rect.bottom &&
                                allowed(p.x, p.y))) {
                                return { points: candidate, translated: dx !== 0 || dy !== 0 };
                              }
                            }
                          }
                          return null;
                        }
                        """,
                        points,
                    )
                    if isinstance(rebased, dict) and isinstance(rebased.get("points"), list):
                        points = rebased["points"]
                        if rebased.get("translated"):
                            payload = {**payload, "points": points, "geometry_rebased": True}
                except (PlaywrightError, TypeError, ValueError):
                    # Keep the evidence-backed coordinates when a browser
                    # does not expose elementFromPoint reliably.
                    pass
            duration_ms = max(0, int(payload.get("duration_ms", 450)))
            shortcut = payload.get("tool_shortcut")
            if isinstance(shortcut, str) and len(shortcut.strip()) == 1 and shortcut.isalnum():
                # Some canvas editors revert to selection after a toolbar
                # click when focus moves to the drawing surface.  Reassert
                # only the shortcut that discovery observed on the chosen
                # tool; this keeps the gesture generic and avoids guessing
                # application-specific keys.
                await self.page.keyboard.press(shortcut.strip())
                await self.page.wait_for_timeout(180)
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
                payload.get(
                    "press",
                    payload.get("pattern")
                    in {"short_reversible_stroke", "connector_segment", "shape_box"},
                )
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
            if pattern == "text_placement":
                # Canvas editors often treat toolbar-selected text placement
                # as a click gesture rather than a zero-length drag. Keep the
                # observed path for the trace, then issue the equivalent native
                # click at that same grounded point to open the editor.
                await self.page.mouse.click(float(first["x"]), float(first["y"]), button=button)
                await self.page.wait_for_timeout(180)
                self._pending_canvas_text_placement = {
                    "url": str(getattr(self.page, "url", "") or ""),
                    "selector": str(operation.target.selector if operation.target else ""),
                    "point": {"x": float(first["x"]), "y": float(first["y"])},
                }
            elif pattern != "text_placement":
                # A new drawing gesture cannot consume an older text hand-off.
                self._pending_canvas_text_placement = None
            elif pattern == "connector_segment":
                # Selection handles are transient pixels, not proof of a
                # committed connector. Clear them before taking the outcome
                # witness so a failed drag cannot pass verification.
                await self.page.keyboard.press("Escape")
                await self.page.wait_for_timeout(180)
            changed = None
            after_structure = None
            if (
                pattern
                in {
                    "short_reversible_stroke",
                    "connector_segment",
                    "text_placement",
                    "shape_box",
                }
                and operation.target is not None
            ):
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
                    after_structure = await self.page.evaluate(
                        """() => ({
                          svg: Array.from(document.querySelectorAll('svg')).map(node => ({
                            count: node.querySelectorAll('*').length,
                            html: node.innerHTML.length
                          })),
                          canvas: document.querySelectorAll('canvas').length,
                          semantic_nodes: document.querySelectorAll(
                            '[data-testid],[data-shape],[data-element-id],[aria-label]'
                          ).length
                        })"""
                    )
                    structural_witness = bool(
                        isinstance(surface_structure, dict)
                        and surface_structure.get("svg")
                        and isinstance(after_structure, dict)
                        and after_structure != surface_structure
                    )
                    pixel_witness = bool(
                        surface_fingerprint is not None and after_fingerprint != surface_fingerprint
                    )
                    editor_witness = False
                    if pattern == "text_placement":
                        try:
                            editor_witness = bool(
                                await self.page.locator(
                                    "textarea:visible, [contenteditable='true']:visible, [role='textbox']:visible"
                                ).count()
                            )
                        except (AttributeError, PlaywrightError, TypeError):
                            editor_witness = False
                    # Prefer structural proof when an SVG scene graph is
                    # available; transient selection pixels are not a valid
                    # committed drawing.  For canvas-only products, the
                    # clipped pixel witness remains the portable fallback.
                    changed = (
                        (structural_witness or editor_witness)
                        if (isinstance(surface_structure, dict) and surface_structure.get("svg"))
                        else (pixel_witness or editor_witness)
                    )
                except PlaywrightError:
                    changed = False
            return {
                "points": points,
                "duration_ms": duration_ms,
                "button": button,
                **({"surface_changed": changed} if changed is not None else {}),
                **(
                    {
                        "semantic_evidence": {
                            "before": surface_structure,
                            "after": after_structure,
                            "committed": bool(changed),
                        }
                    }
                    if pattern
                    in {
                        "short_reversible_stroke",
                        "connector_segment",
                        "text_placement",
                        "shape_box",
                    }
                    else {}
                ),
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
        canvas_text_payload: dict[str, object] | None = None
        if operation.kind is OperationKind.KEY_PRESS and operation.target is not None:
            target_selector = str(operation.target.selector or "").casefold()
            if target_selector in {"canvas", "svg"} and isinstance(operation.value, str):
                # Older/remote planners may ground the canvas target on the
                # KeyPress itself and emit the label as a plain string. Treat
                # that as the same semantic text-placement operation as the
                # richer payload form; never send the label to locator.press.
                canvas_text_payload = {
                    "text": operation.value,
                    "surface_target": operation.target.model_dump(mode="json"),
                    "placement_mode": "text",
                }
        if operation.kind is OperationKind.KEY_PRESS and (
            operation.target is None or canvas_text_payload is not None
        ):
            # A focused canvas text editor needs human-paced text input rather
            # than a single keyboard shortcut.  The planner uses this form
            # only after grounding a visible text tool and canvas target.
            payload = canvas_text_payload or (
                operation.value if isinstance(operation.value, dict) else None
            )
            if isinstance(payload, dict) and isinstance(payload.get("text"), str):
                text = str(payload["text"])
                surface_fingerprint = None
                surface_target = payload.get("surface_target")
                if isinstance(surface_target, dict):
                    try:
                        surface, _ = await self.grounded_locator(Target.model_validate(surface_target))
                        box = await surface.bounding_box()
                        if box and box.get("width", 0) >= 16 and box.get("height", 0) >= 16:
                            surface_fingerprint = await self.page.screenshot(clip=box)
                    except (PlaywrightError, GroundingError, ValidationError):
                        surface_fingerprint = None
                focus_before = await self.page.evaluate(
                    """() => {
                      const e = document.activeElement;
                      if (!e || e === document.body || e === document.documentElement) return null;
                      const tag = e.tagName.toLowerCase();
                      const editable = e.isContentEditable || tag === 'input' || tag === 'textarea' ||
                        e.getAttribute('role') === 'textbox';
                      return editable ? {
                        tag,
                        name: (e.getAttribute('aria-label') || e.getAttribute('name') ||
                          e.getAttribute('placeholder') || '').slice(0,160),
                        contenteditable: e.isContentEditable,
                      } : null;
                    }"""
                )
                if not focus_before and isinstance(surface_target, dict):
                    # Some editors only create their contenteditable after a
                    # second native click on the visible drawing surface.
                    # Re-ground that surface and retry the same semantic
                    # placement before declaring the interaction unsupported.
                    try:
                        surface, _ = await self.grounded_locator(
                            Target.model_validate(surface_target)
                        )
                        box = await surface.bounding_box()
                        if box:
                            placement = payload.get("placement")
                            px = float(placement.get("x", 0.5)) if isinstance(placement, dict) else 0.5
                            py = float(placement.get("y", 0.5)) if isinstance(placement, dict) else 0.5
                            pending = self._pending_canvas_text_placement
                            pending_point = pending.get("point") if isinstance(pending, dict) else None
                            pending_matches = bool(
                                isinstance(pending, dict)
                                and pending.get("url") == str(getattr(self.page, "url", "") or "")
                                and pending.get("selector") == str(surface_target.get("selector", ""))
                                and isinstance(pending_point, dict)
                                and {"x", "y"} <= set(pending_point)
                            )
                            if pending_matches:
                                # Restore the exact observed click point when
                                # focus was lost between pointer placement and
                                # typing.  Falling back to a declared relative
                                # placement remains valid for planners that
                                # supply one explicitly.
                                click_x = float(pending_point["x"])
                                click_y = float(pending_point["y"])
                            else:
                                click_x = float(box["x"]) + float(box["width"]) * px
                                click_y = float(box["y"]) + float(box["height"]) * py
                            await self.page.mouse.click(click_x, click_y)
                            await self.page.wait_for_timeout(180)
                            focus_before = await self.page.evaluate(
                                """() => { const e=document.activeElement; return e &&
                                (e.isContentEditable || ['input','textarea'].includes(e.tagName.toLowerCase()) ||
                                e.getAttribute('role') === 'textbox'); }"""
                            )
                            if (
                                not focus_before
                                and payload.get("placement_mode") == "text"
                            ):
                                # Text-mode editors commonly expose a single
                                # keyboard affordance when the toolbar click
                                # is swallowed by a canvas overlay. This is a
                                # bounded semantic fallback, never used for
                                # arbitrary keypresses or ordinary pages.
                                await self.page.keyboard.press("t")
                                await self.page.wait_for_timeout(120)
                                await self.page.mouse.click(
                                    float(box["x"]) + float(box["width"]) * px,
                                    float(box["y"]) + float(box["height"]) * py,
                                )
                                await self.page.wait_for_timeout(180)
                                focus_before = await self.page.evaluate(
                                    """() => { const e=document.activeElement; return e &&
                                    (e.isContentEditable || ['input','textarea'].includes(e.tagName.toLowerCase()) ||
                                    e.getAttribute('role') === 'textbox'); }"""
                                )
                            if not focus_before and payload.get("placement_mode") == "text":
                                # Canvas editors frequently keep a transient
                                # textarea/contenteditable outside the drawing
                                # surface. Discover it by semantics rather
                                # than a product selector and focus the first
                                # visible editor created by the placement.
                                editors = self.page.locator(
                                    "textarea:visible, [contenteditable='true']:visible, [role='textbox']:visible"
                                )
                                for index in range(await editors.count()):
                                    editor = editors.nth(index)
                                    try:
                                        await editor.focus()
                                        focus_before = await self.page.evaluate(
                                            """() => { const e=document.activeElement; return e &&
                                            (e.isContentEditable || ['input','textarea'].includes(e.tagName.toLowerCase()) ||
                                            e.getAttribute('role') === 'textbox'); }"""
                                        )
                                        if focus_before:
                                            break
                                    except PlaywrightError:
                                        continue
                    except (PlaywrightError, GroundingError, ValidationError):
                        focus_before = None
                if not focus_before:
                    raise GroundingError("Keyboard text input requires a focused editable surface")
                await self.page.keyboard.type(text, delay=70)
                if payload.get("placement_mode") == "text":
                    # Drawing editors commonly keep the label in a transient
                    # textarea/contenteditable until their standard commit
                    # shortcut is dispatched.  Commit only this explicitly
                    # grounded text-placement operation; ordinary keyboard
                    # actions must retain their caller-supplied key semantics.
                    await self.page.keyboard.press("ControlOrMeta+Enter")
                    await self.page.wait_for_timeout(220)
                await self.page.wait_for_timeout(120)
                focus = await self.page.evaluate(
                    """() => { const e=document.activeElement; return e && e !== document.body ? {
                      tag:e.tagName.toLowerCase(), name:(e.getAttribute('aria-label') || e.getAttribute('name') ||
                      e.getAttribute('placeholder') || '').slice(0,160), contenteditable:e.isContentEditable
                    } : null; }"""
                )
                surface_changed = None
                if surface_fingerprint is not None and isinstance(surface_target, dict):
                    try:
                        surface, _ = await self.grounded_locator(Target.model_validate(surface_target))
                        box = await surface.bounding_box()
                        after = await self.page.screenshot(clip=box) if box else None
                        surface_changed = after != surface_fingerprint
                    except (PlaywrightError, GroundingError, ValidationError):
                        surface_changed = None
                result = {
                    "text_length": len(text),
                    "scope": "focused-editable",
                    "focused": focus,
                    **({"surface_changed": surface_changed} if surface_changed is not None else {}),
                }
                if payload.get("placement_mode") == "text":
                    # Do not let a later unrelated canvas operation consume a
                    # stale text placement.  The pointer->text hand-off is
                    # one-shot by design.
                    self._pending_canvas_text_placement = None
                return result
            key = str(operation.value or "Escape")
            await self.page.keyboard.press(key)
            await self.page.wait_for_timeout(120)
            focus = await self.page.evaluate(
                """() => { const e=document.activeElement; return e && e !== document.body ? {
                  tag:e.tagName.toLowerCase(), name:(e.getAttribute('aria-label') || e.getAttribute('name') ||
                  e.getAttribute('placeholder') || '').slice(0,160), contenteditable:e.isContentEditable
                } : null; }"""
            )
            return {"key": key, "scope": "page", "focused": focus}
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
        # The planner may call a native segmented control either a text field
        # or an option selector depending on what the accessibility snapshot
        # exposed. Resolve the browser-native input type at dispatch time so
        # both plans use the same visible, verified interaction path.
        observed_input_type = ""
        if operation.kind in {
            OperationKind.FILL_TEXT,
            OperationKind.FILL_EMAIL,
            OperationKind.FILL_PHONE,
            OperationKind.SEARCH,
            OperationKind.SELECT_OPTION,
        }:
            try:
                observed_input_type = str(
                    await locator.evaluate("element => String(element.type || '').toLowerCase()")
                )
            except (AttributeError, PlaywrightError):
                observed_input_type = ""
            if observed_input_type == "time":
                return await self._fill_time_human_visible(locator, str(operation.value))
            if observed_input_type == "date" and operation.kind is not OperationKind.SELECT_OPTION:
                return await self._select_date_human_visible(locator, str(operation.value or ""))
        if operation.kind in {
            OperationKind.FILL_TEXT,
            OperationKind.FILL_EMAIL,
            OperationKind.FILL_PHONE,
            OperationKind.SEARCH,
        }:
            # Never use fill() here — it commits the whole string at once and
            # reads as paste on camera. Visible demos must keystroke.
            if operation.kind is OperationKind.FILL_TEXT:
                # Canvas editors create a transient textarea/contenteditable
                # after a text-placement gesture. The semantic target remains
                # the observed canvas, so type through the focused editor.
                try:
                    target_tag = await locator.evaluate(
                        "element => String(element.tagName || '').toLowerCase()"
                    )
                    focused_editor = await self.page.evaluate(
                        """() => {
                          const element = document.activeElement;
                          if (!element || element === document.body) return false;
                          const tag = String(element.tagName || '').toLowerCase();
                          return Boolean(element.isContentEditable || tag === 'textarea' ||
                            tag === 'input' || element.getAttribute('role') === 'textbox');
                        }"""
                    )
                except (AttributeError, PlaywrightError, TypeError):
                    target_tag, focused_editor = "", False
                if target_tag in {"canvas", "svg"} and focused_editor:
                    visible_value = str(operation.value)
                    await self.page.keyboard.type(visible_value, delay=self.typing_delay_ms)
                    # Canvas editors commonly keep text in a transient editor
                    # until their documented commit shortcut is dispatched.
                    # Use the standard modifier+Enter contract first; Escape
                    # is intentionally not used because it can discard the
                    # just-entered value in otherwise equivalent editors.
                    await self.page.keyboard.press("ControlOrMeta+Enter")
                    await self.page.wait_for_timeout(220)
                    return {
                        "typed": True,
                        "typing_started": True,
                        "completed_value_checkpoint": visible_value,
                        "scope": "focused-editable",
                    }
            await locator.click()
            await locator.press("ControlOrMeta+A")
            await locator.press("Backspace")
            visible_value = str(operation.value)
            split_at = max(1, len(visible_value) // 2)
            await locator.press_sequentially(
                visible_value[:split_at], delay=self.typing_delay_ms
            )
            try:
                partial_value = await locator.evaluate(
                    "element => String(element.value || element.textContent || '')"
                )
            except (AttributeError, PlaywrightError):
                partial_value = visible_value[:split_at]
            result = await locator.press_sequentially(
                visible_value[split_at:], delay=self.typing_delay_ms
            )
            # Lightweight adapter fakes used by unit tests (and a few remote
            # wrappers) expose the action methods but not Playwright's
            # ``evaluate`` helper.  Keep the browser witness when available;
            # the execution engine will require it for a real production
            # interaction, while compatibility adapters can still exercise
            # the visible typing sequence itself.
            try:
                focus = await locator.evaluate(
                    "element => ({focused: element === document.activeElement || element.contains(document.activeElement), valuePresent: Boolean(element.value || element.textContent), valueLength: String(element.value || element.textContent || '').length})"
                )
            except (AttributeError, PlaywrightError):
                focus = None
            return {
                "typed": True,
                "typing_started": True,
                "partial_value_checkpoint": partial_value,
                "completed_value_checkpoint": visible_value,
                "focus": focus,
                "result": result,
            }
        if operation.kind == OperationKind.SELECT_DATE:
            return await self._select_date_human_visible(locator, str(operation.value or ""))
        if operation.kind == OperationKind.SELECT_DATE_RANGE:
            if not isinstance(operation.value, dict) or "start" not in operation.value:
                raise GroundingError(
                    "SelectDateRange requires a start value and a product-specific compiled target"
                )
            start = await self._select_date_human_visible(
                locator, str(operation.value["start"])
            )
            if operation.value.get("end"):
                end = await self._select_date_human_visible(
                    locator, str(operation.value["end"])
                )
                return {"start": start, "end": end}
            return {"start": start}
        if operation.kind == OperationKind.SELECT_OPTION:
            # ``get_by_label`` can resolve both the input and the transient
            # listbox that a custom combobox opens.  Keep the evidence-backed
            # target, but narrow the action surface to the actual control
            # before keyboard fallback.  This is important for any design
            # system that renders an aria-labelled listbox beside its input;
            # pressing Control+A on the broad label locator is a strict-mode
            # failure and makes an otherwise valid option look unavailable.
            selection_locator = locator
            target = operation.target
            if target is not None:
                control_candidates: list[Any] = []
                if target.selector:
                    control_candidates.append(self.page.locator(target.selector))
                control_name = target.label or target.name
                if control_name:
                    try:
                        control_candidates.append(
                            self.page.get_by_role(
                                "combobox",
                                name=re.compile(re.escape(control_name.rstrip(" *")), re.IGNORECASE),
                                exact=False,
                            )
                        )
                    except (PlaywrightError, AttributeError, AssertionError):
                        pass
                for candidate in control_candidates:
                    try:
                        count = await candidate.count()
                        visible = [
                            i
                            for i in range(count)
                            if await candidate.nth(i).is_visible()
                        ]
                        if len(visible) == 1:
                            selection_locator = candidate.nth(visible[0])
                            break
                    except (PlaywrightError, TypeError, AttributeError):
                        continue
            native = await self._select_native_option_human_visible(
                selection_locator, str(operation.value)
            )
            if native is not None:
                return native
            try:
                # Design-system comboboxes expose their choices through the
                # accessibility tree instead of a native <select>. The value
                # came from discovery's visible option probe, so this remains
                # a semantic, evidence-backed choice rather than typed guess.
                # MUI Autocomplete inputs are often covered by adornment /
                # label chrome that makes actionability wait forever; open
                # with force + keyboard fallbacks before treating as failed.
                opened = False
                for open_attempt in (
                    lambda: selection_locator.click(timeout=8_000),
                    lambda: selection_locator.click(timeout=5_000, force=True),
                    lambda: selection_locator.click(),
                    lambda: selection_locator.focus(),
                ):
                    try:
                        await open_attempt()
                        opened = True
                        break
                    except (PlaywrightError, TypeError):
                        continue
                if not opened:
                    raise GroundingError(
                        f"Could not open observed combobox for option {operation.value!r}"
                    )
                try:
                    press = getattr(selection_locator, "press", None)
                    if callable(press):
                        await press("ArrowDown")
                except (PlaywrightError, AttributeError, TypeError):
                    pass
                try:
                    wait_for_selector = getattr(self.page, "wait_for_selector", None)
                    if callable(wait_for_selector):
                        await wait_for_selector(
                            "[role='listbox'], [role='option']",
                            state="attached",
                            timeout=2_500,
                        )
                except (PlaywrightError, AttributeError, TypeError):
                    pass
                # Keep the opened option surface on screen long enough for the
                # production recording to prove the choice.  Without this
                # bounded reveal dwell, a fast CDP action can capture only the
                # post-selection value and make a valid interaction look like
                # a random assignment.
                wait_for_timeout = getattr(self.page, "wait_for_timeout", None)
                if callable(wait_for_timeout):
                    await wait_for_timeout(550)
                # Re-ground against the options that are actually visible now.
                # Discovery labels can be clipped by an accessibility wrapper;
                # accept a prefix only when it identifies exactly one current
                # option. Never type a stale label or choose the first nearby
                # option when the evidence is ambiguous.
                try:
                    option_surface = self.page.get_by_role("option")
                    option_count = await option_surface.count()
                except (AttributeError, PlaywrightError, AssertionError):
                    option_surface = self.page.get_by_role(
                        "option", name=str(operation.value), exact=True
                    )
                    option_count = await option_surface.count()
                visible_labels: list[str] = []
                visible_options: list[tuple[int, str, Any]] = []
                for index in range(option_count):
                    candidate = option_surface.nth(index)
                    try:
                        if await candidate.is_visible():
                            label = ""
                            try:
                                label = (await candidate.inner_text()).strip()
                            except (AttributeError, PlaywrightError):
                                try:
                                    label = str(
                                        await candidate.get_attribute("aria-label") or ""
                                    ).strip()
                                except (AttributeError, PlaywrightError):
                                    label = ""
                            if label:
                                visible_labels.append(label)
                                visible_options.append((index, label, candidate))
                    except (PlaywrightError, TypeError):
                        continue
                expected_fold = " ".join(str(operation.value).split()).casefold()

                def _option_matches(label: str) -> bool:
                    observed_fold = " ".join(label.split()).casefold()
                    return bool(
                        observed_fold
                        and (
                            observed_fold == expected_fold
                            or observed_fold.startswith(expected_fold + " ")
                            or expected_fold.startswith(observed_fold + " ")
                        )
                    )

                matched_options = [
                    item for item in visible_options if _option_matches(item[1])
                ]
                if len(matched_options) == 1:
                    _index, observed_label, candidate = matched_options[0]
                    state_before = await self._choice_control_state(selection_locator)
                    try:
                        await candidate.click(timeout=5_000)
                    except TypeError:
                        await candidate.click()
                    except PlaywrightError:
                        try:
                            await candidate.click(timeout=3_000, force=True)
                        except TypeError:
                            await candidate.click()
                    if callable(wait_for_timeout):
                        await wait_for_timeout(220)
                    selection_witness = await self._visible_choice_value(
                        selection_locator, observed_label
                    )
                    state_after = await self._choice_control_state(selection_locator)
                    state_transition = bool(
                        state_before is not None
                        and state_after is not None
                        and state_before != state_after
                    )
                    if not selection_witness and state_transition:
                        # Some custom multi-selects keep the chosen chip in a
                        # sibling layer with no accessible label. A unique
                        # observed option click plus a bounded control-owned
                        # state transition is still a valid semantic witness;
                        # a no-op or ambiguous click cannot satisfy it.
                        selection_witness = True
                    if not selection_witness:
                        raise GroundingError(
                            f"Observed choice did not render the selected label "
                            f"{observed_label!r}; re-observe dependent options"
                        )
                    return {
                        "selected": observed_label,
                        "requested": str(operation.value),
                        "options_visible": True,
                        "interaction": "custom-listbox-visible-choice",
                        "selection_witness": selection_witness,
                        "selection_state_changed": state_transition,
                    }
                # A dependent combobox can legitimately replace its choices
                # after an earlier field changes. Never type an unavailable
                # planned label and let a fuzzy autocomplete select the first
                # neighbouring option; that creates a false-looking demo. A
                # visible option surface with no exact match is a reversible
                # pre-dispatch grounding failure, so the execution kernel can
                # re-observe and replan from the current state.
                if visible_labels:
                    observed_folds = {
                        " ".join(label.split()).casefold() for label in visible_labels
                    }
                    if expected_fold not in observed_folds:
                        raise GroundingError(
                            f"Observed option changed before dispatch: expected {operation.value!r}; "
                            f"available labels={visible_labels[:12]!r}"
                        )
                # Fallback: type the observed label into the open combobox and
                # commit with Enter when the listbox option node is transient.
                try:
                    await selection_locator.press("ControlOrMeta+A")
                    await selection_locator.press("Backspace")
                    await selection_locator.press_sequentially(
                        str(operation.value), delay=self.typing_delay_ms
                    )
                    await selection_locator.press("Enter")
                    if callable(wait_for_timeout):
                        await wait_for_timeout(220)
                    witness = await self._visible_choice_value(
                        selection_locator, str(operation.value)
                    )
                    if not witness:
                        raise GroundingError(
                            f"Observed autocomplete did not commit exact option {operation.value!r}"
                        )
                    return {
                        "selected": str(operation.value),
                        "options_visible": False,
                        "interaction": "custom-combobox-typeahead",
                        "selection_witness": witness,
                    }
                except PlaywrightError as error:
                    raise GroundingError(
                        f"Observed option is no longer visible: {operation.value!r}"
                    ) from error
            except AttributeError as error:
                raise GroundingError("Custom select lacks a visible option surface") from error
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
            focus = await locator.evaluate(
                "element => element === document.activeElement || element.contains(document.activeElement)"
            )
            return {"key": key, "focused": bool(focus)}
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
                click_position = None
                if isinstance(operation.value, dict) and isinstance(
                    operation.value.get("relative"), dict
                ):
                    relative = operation.value["relative"]
                    box = await locator.bounding_box()
                    if (
                        box
                        and 0 <= float(relative.get("x", -1)) <= 1
                        and 0 <= float(relative.get("y", -1)) <= 1
                    ):
                        click_position = {
                            "x": float(box["width"]) * float(relative["x"]),
                            "y": float(box["height"]) * float(relative["y"]),
                        }
                result = (
                    await locator.click(position=click_position)
                    if click_position
                    else await locator.click()
                )
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
        await self.ensure_page_async()
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
                    const surfaceSignature = element => {
                      try {
                        if (element instanceof HTMLCanvasElement) {
                          const width = Math.min(element.width || 0, 320);
                          const height = Math.min(element.height || 0, 240);
                          if (!width || !height) return '';
                          const source = document.createElement('canvas');
                          source.width = width; source.height = height;
                          const ctx = source.getContext('2d', {willReadFrequently: true});
                          if (!ctx) return '';
                          ctx.drawImage(element, 0, 0, width, height);
                          const data = ctx.getImageData(0, 0, width, height).data;
                          let sampled = '';
                          for (let i = 0; i < data.length; i += 16) sampled += String.fromCharCode(data[i]);
                          return fingerprint(sampled);
                        }
                        if (element instanceof SVGElement) return fingerprint(element.outerHTML.slice(0, 200000));
                      } catch (_) { return ''; }
                      return '';
                    };
                    return {
                        url: window.location.href,
                        text: (element.innerText || element.textContent || '').slice(0, 500),
                        page_text_hash: fingerprint((document.body?.innerText || '').slice(0, 12000)),
                        dom_hash: fingerprint((document.body?.innerHTML || '').slice(0, 120000)),
                        surface_signature: surfaceSignature(element),
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
                        focused: element === document.activeElement || element.contains(document.activeElement),
                        active_element: document.activeElement ? {
                          tag: document.activeElement.tagName.toLowerCase(),
                          name: (document.activeElement.getAttribute('aria-label') ||
                            document.activeElement.getAttribute('name') ||
                            document.activeElement.getAttribute('placeholder') || '').slice(0,160),
                        } : null,
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
        await self.ensure_page_async()
        try:
            script = """(limit) => ({
                    url: window.location.href,
                    title: document.title,
                    text: (document.body?.innerText || '').slice(0, limit),
                    domSnapshot: (() => {
                      const body = document.body;
                      if (!body) return '';
                      const clone = body.cloneNode(true);
                      clone.querySelectorAll('input, textarea, select').forEach(element => {
                        element.removeAttribute('value');
                        if (element.tagName === 'TEXTAREA') element.textContent = '';
                        if (element.tagName === 'SELECT') element.querySelectorAll('option').forEach(option => {
                          option.removeAttribute('selected');
                        });
                      });
                      return clone.outerHTML.slice(0, 50000);
                    })(),
                    controls: Array.from(document.querySelectorAll(
                      'button, a, input, textarea, select, canvas, [contenteditable="true"], [role="button"], [role="link"], [role="tab"], [role="combobox"], [role="checkbox"], [role="radio"], [role="option"], [role="gridcell"], [role="application"]'
                    )).filter(element => {
                      const box = element.getBoundingClientRect();
                      return box.width > 0 && box.height > 0 &&
                        getComputedStyle(element).visibility !== 'hidden';
                    }).sort((left, right) => {
                      const score = element => {
                        const box = element.getBoundingClientRect();
                        const inViewport = box.bottom > 0 && box.right > 0 &&
                          box.top < window.innerHeight && box.left < window.innerWidth;
                        const inActiveSurface = Boolean(element.closest(
                          '[aria-modal="true"],[role="dialog"],[role="listbox"],[role="menu"],'
                          + '[data-state="open"],[class*="modal" i],[class*="popover" i],'
                          + '[class*="dropdown" i],[class*="overlay" i]'
                        ));
                        return (inActiveSurface ? 1000 : 0) +
                          (element === document.activeElement ? 500 : 0) +
                          (inViewport ? 100 : 0);
                      };
                      return score(right) - score(left);
                    }).slice(0, 160).map(element => ({
                      tag: element.tagName.toLowerCase(),
                      role: element.getAttribute('role'),
                      selector: (() => {
                        const esc = value => (globalThis.CSS && CSS.escape)
                          ? CSS.escape(String(value))
                          : String(value).replace(/[^a-zA-Z0-9_-]/g, '_');
                        const testId = element.getAttribute('data-testid');
                        if (testId) return `[data-testid="${String(testId).replace(/\\"/g, '\\\\"')}"]`;
                        const id = element.getAttribute('id');
                        if (id) return `#${esc(id)}`;
                        const name = element.getAttribute('name');
                        if (name) return `${element.tagName.toLowerCase()}[name="${String(name).replace(/\\"/g, '\\\\"')}"]`;
                        const label = element.getAttribute('aria-label');
                        if (label) return `${element.tagName.toLowerCase()}[aria-label="${String(label).replace(/\\"/g, '\\\\"')}"]`;
                        return null;
                      })(),
                      name: (element.getAttribute('aria-label') || element.innerText ||
                        element.getAttribute('placeholder') || element.getAttribute('name') || '').trim().slice(0, 160),
                      type: element.getAttribute('type'),
                      value: ['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName)
                        ? String(element.value || '').slice(0, 240)
                        : (element.getAttribute('aria-valuetext') || '').slice(0, 240),
                      options: element.tagName === 'SELECT'
                        ? Array.from(element.options || []).filter(option => !option.disabled)
                            .map(option => (option.label || option.textContent || option.value || '').trim())
                            .filter(Boolean).slice(0, 100)
                        : Array.from(document.querySelectorAll(
                            element.getAttribute('aria-controls')
                              ? `#${globalThis.CSS && CSS.escape ? CSS.escape(element.getAttribute('aria-controls')) : element.getAttribute('aria-controls')} [role="option"]`
                              : ':scope > option'
                          )).map(option => (option.innerText || option.textContent || '').trim())
                            .filter(Boolean).slice(0, 100),
                      expanded: element.getAttribute('aria-expanded') === null
                        ? null : element.getAttribute('aria-expanded') === 'true',
                      haspopup: element.getAttribute('aria-haspopup'),
                      autocomplete: element.getAttribute('aria-autocomplete'),
                      contenteditable: element.getAttribute('contenteditable') === 'true',
                      readonly: Boolean(element.readOnly) || element.getAttribute('aria-readonly') === 'true',
                      draggable: element.draggable || element.getAttribute('aria-grabbed') !== null,
                      submits_form: element.getAttribute('type') === 'submit',
                      canvas_tool: Boolean(
                        element.closest('[role="toolbar"],[class*="toolbar" i]') &&
                        (element.getAttribute('aria-label') || element.getAttribute('title'))
                      ),
                      form: element.form?.getAttribute('aria-label') || element.form?.getAttribute('name') ||
                        element.closest('form')?.getAttribute('aria-label') || '',
                      section: element.closest('fieldset,[role="group"],section')?.getAttribute('aria-label') ||
                        element.closest('fieldset')?.querySelector('legend')?.textContent || '',
                      ancestry: (() => {
                        const parts = [];
                        let node = element.parentElement;
                        while (node && parts.length < 4) {
                          parts.push(`${node.tagName.toLowerCase()}:${node.getAttribute('role') || ''}:${node.getAttribute('aria-label') || ''}`);
                          node = node.parentElement;
                        }
                        return parts.join('>');
                      })(),
                      stable_attributes: {
                        testid: element.getAttribute('data-testid'),
                        name: element.getAttribute('name'),
                        ariaLabel: element.getAttribute('aria-label'),
                        ariaControls: element.getAttribute('aria-controls'),
                      },
                      disabled: Boolean(element.disabled),
                      geometry: (() => { const box = element.getBoundingClientRect(); return {
                        x: box.x, y: box.y, width: box.width, height: box.height,
                      }; })(),
                    })).filter(item => item.name),
                    visualSurface: {
                      canvas: document.querySelectorAll('canvas').length > 0,
                      svg: document.querySelectorAll('svg').length > 0,
                      contenteditable: document.querySelectorAll('[contenteditable="true"]').length > 0,
                    },
                    focusedControl: (() => {
                      const element = document.activeElement;
                      if (!element || element === document.body || element === document.documentElement) return null;
                      const box = element.getBoundingClientRect();
                      return {
                        tag: element.tagName.toLowerCase(),
                        role: element.getAttribute('role'),
                        name: (element.getAttribute('aria-label') || element.getAttribute('placeholder') ||
                          element.getAttribute('name') || element.innerText || '').trim().slice(0, 160),
                        geometry: {x: box.x, y: box.y, width: box.width, height: box.height},
                        contenteditable: element.getAttribute('contenteditable') === 'true',
                      };
                    })(),
                    overlays: Array.from(document.querySelectorAll(
                      '[aria-modal="true"],[role="dialog"],[role="listbox"],[role="menu"],[data-state="open"],[class*="modal" i],[class*="popover" i],[class*="dropdown" i],[class*="backdrop" i],[class*="overlay" i]'
                    )).filter(element => {
                      const box = element.getBoundingClientRect();
                      const style = getComputedStyle(element);
                      return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' &&
                        style.display !== 'none' && style.pointerEvents !== 'none';
                    }).slice(0, 24).map(element => {
                      const box = element.getBoundingClientRect();
                      return {
                        role: element.getAttribute('role'),
                        label: (element.getAttribute('aria-label') || element.innerText || '').trim().slice(0, 180),
                        geometry: {x: box.x, y: box.y, width: box.width, height: box.height},
                      };
                    }),
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
                          geometry: (() => { const box = element.getBoundingClientRect(); return {
                            x: box.x, y: box.y, width: box.width, height: box.height,
                          }; })(),
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
            try:
                result["accessibilitySnapshot"] = await self.page.locator("body").aria_snapshot(
                    timeout=2_000
                )
            except (AttributeError, PlaywrightError):
                result["accessibilitySnapshot"] = ""
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
        await self.ensure_page_async()
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
        await self.ensure_page_async()
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
