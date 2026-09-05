"""Evidence-backed browser viewport selection.

This is deliberately separate from camera decisions. It may choose a capture
viewport, but it never changes the browser's CSS zoom from 100%.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from productlens.contracts.models import ObservedElement, PageKnowledge, Viewport, ViewportDecision

if TYPE_CHECKING:
    from playwright.async_api import Page

_CANDIDATES = (Viewport(width=1440, height=900), Viewport(width=1600, height=1000), Viewport(width=1920, height=1080))


def evaluate_viewport_candidates(
    elements: list[ObservedElement], objective: str, pages: list[PageKnowledge] | None = None,
) -> list[ViewportDecision]:
    """Score every supported capture configuration before locking production.

    This remains deterministic and browser-provider independent. Live discovery
    supplies the evidence (controls, forms, and objective terms); a future
    browser probe can add geometry evidence without changing this contract.
    """
    objective_tokens = set(re.findall(r"[a-z0-9]{3,}", objective.lower()))
    actionable = [item for item in elements if item.actionable]
    matched = sum(
        bool(objective_tokens & set(re.findall(r"[a-z0-9]{3,}", f"{item.name} {item.text or ''}".lower())))
        for item in actionable
    )
    forms = sum(item.tag in {"input", "select", "textarea"} for item in actionable)
    page_knowledge = pages or []
    # The production context uses one locked native viewport, so it must be
    # selected against every discovered page that could appear in the story,
    # not merely the initial dashboard.  PageKnowledge is deliberately text
    # and evidence based, letting this assessment remain provider-independent.
    page_densities = [
        len(page.visible_sections) + len(page.actionable_controls) + len(page.form_schemas) * 2
        for page in page_knowledge
    ]
    densest_page = max(page_densities, default=0)
    density = max(len(actionable) + forms * 2 + matched * 3, densest_page)
    full_walkthrough = bool(re.search(r"\b(?:full|complete|entire|every|each)\b", objective.lower()))
    decisions: list[ViewportDecision] = []
    for candidate in _CANDIDATES:
        # Wider frames preserve navigation and dense content; smaller frames
        # retain readability for sparse pages. All candidates remain native
        # browser scale and are evaluated rather than selected by URL/site.
        width_bonus = ((candidate.width - 1440) / 480 * min(0.22, density / 250)) if density >= 20 else 0.0
        # A full walkthrough needs enough room for page context, but it does
        # not automatically benefit from the widest viewport. Many products
        # have a fixed-width reading column which becomes materially smaller
        # in a 1920 CSS-pixel browser. A live probe below can promote a wider
        # frame only when the rendered layout proves that it helps.
        full_bonus = 0.04 if full_walkthrough and candidate.width == 1600 else 0.0
        compact_penalty = 0.08 if density < 20 and candidate.width == 1920 else 0.0
        score = round(max(0.05, 0.58 + width_bonus + full_bonus - compact_penalty), 3)
        decisions.append(ViewportDecision(
            viewport=candidate,
            browser_zoom_percent=100,
            score=score,
            evidence=[
                f"observed actionable controls: {len(actionable)}",
                f"observed form controls: {forms}",
                f"objective-relevant controls: {matched}",
                f"discovered pages evaluated: {len(page_knowledge)}",
                f"densest discovered page score: {densest_page}",
                "full-walkthrough context preference" if full_walkthrough else "content-density preference",
                "browser CSS zoom retained at 100%",
            ],
        ))
    return decisions


def choose_viewport(
    elements: list[ObservedElement], objective: str, pages: list[PageKnowledge] | None = None,
) -> ViewportDecision:
    """Pick the smallest native-scale viewport that preserves product context.

    DOM evidence cannot infer arbitrary camera coordinates, so scores use only
    observed, actionable controls and objective-term coverage. Dense apps get a
    wider capture to avoid cropping labels and sidebars; sparse pages retain a
    closer native view. The decision is retained as a run artifact.
    """
    return max(evaluate_viewport_candidates(elements, objective, pages), key=lambda item: item.score)


async def probe_viewport_candidates(
    page: Page, elements: list[ObservedElement], objective: str,
    pages: list[PageKnowledge] | None = None,
) -> tuple[ViewportDecision, list[dict[str, object]]]:
    """Select a native browser viewport from rendered layout evidence.

    Static DOM inventory can tell us whether an application is dense, but not
    whether a particular responsive breakpoint leaves its meaningful reading
    column tiny inside a wide capture. This bounded, side-effect-free probe
    evaluates every supported viewport on the opening route before production
    locks one. It intentionally changes *only viewport size*, never browser
    CSS zoom or product state.
    """
    baseline = {item.viewport.width: item for item in evaluate_viewport_candidates(elements, objective, pages)}
    probes: list[dict[str, object]] = []
    decisions: list[ViewportDecision] = []
    for candidate in _CANDIDATES:
        await page.set_viewport_size({"width": candidate.width, "height": candidate.height})
        # Let responsive CSS, fonts, and entrance transitions settle. The
        # production readiness gate still performs its own fuller wait.
        await page.wait_for_timeout(280)
        metrics = await page.evaluate(
            """() => {
                const visible = (el) => {
                  const r = el.getBoundingClientRect();
                  const style = getComputedStyle(el);
                  return r.width > 2 && r.height > 2 && r.bottom > 0 && r.top < innerHeight
                    && style.display !== 'none' && style.visibility !== 'hidden'
                    && Number(style.opacity || 1) > .02;
                };
                const contentNodes = [...document.querySelectorAll(
                  'main h1, main h2, main h3, main p, main article, main section, [role="main"] h1, [role="main"] h2, [role="main"] p, [role="main"] article'
                )].filter(visible);
                const rects = contentNodes.map((node) => node.getBoundingClientRect());
                const minX = rects.length ? Math.min(...rects.map((r) => r.left)) : 0;
                const maxX = rects.length ? Math.max(...rects.map((r) => r.right)) : innerWidth;
                const controls = [...document.querySelectorAll('a,button,input,select,textarea,[role="button"]')].filter(visible);
                const fontSizes = contentNodes.map((node) => parseFloat(getComputedStyle(node).fontSize || '0')).filter(Boolean);
                return {
                  width: innerWidth,
                  height: innerHeight,
                  contentWidth: Math.max(0, maxX - minX),
                  visibleContentNodes: contentNodes.length,
                  visibleControls: controls.length,
                  minFontSize: fontSizes.length ? Math.min(...fontSizes) : 0,
                  horizontalOverflow: Math.max(0, document.documentElement.scrollWidth - innerWidth),
                };
            }"""
        )
        content_ratio = min(1.0, float(metrics["contentWidth"]) / candidate.width)
        # A readable composition normally gives meaningful page content about
        # 55–92% of the capture width. Penalize both a tiny fixed column and a
        # cramped edge-to-edge responsive layout. This is a layout score, not
        # an arbitrary preference for a product or URL.
        composition = max(0.0, 1.0 - abs(content_ratio - 0.74) / 0.42)
        readability = min(1.0, float(metrics["minFontSize"]) / 14.0) if metrics["minFontSize"] else 0.55
        controls = min(1.0, float(metrics["visibleControls"]) / 8.0)
        overflow_penalty = min(0.45, float(metrics["horizontalOverflow"]) / candidate.width)
        static_score = baseline[candidate.width].score
        score = round(max(0.05, min(1.0, static_score * 0.35 + composition * 0.45 + readability * 0.12 + controls * 0.08 - overflow_penalty)), 3)
        probe = {
            "viewport": candidate.model_dump(),
            "content_width_ratio": round(content_ratio, 3),
            "visible_content_nodes": int(metrics["visibleContentNodes"]),
            "visible_controls": int(metrics["visibleControls"]),
            "minimum_visible_font_px": round(float(metrics["minFontSize"]), 2),
            "horizontal_overflow_px": round(float(metrics["horizontalOverflow"]), 2),
            "composition_score": round(composition, 3),
            "score": score,
        }
        probes.append(probe)
        decisions.append(ViewportDecision(
            viewport=candidate,
            browser_zoom_percent=100,
            score=score,
            evidence=[
                *baseline[candidate.width].evidence,
                "live responsive-layout probe",
                f"visible content width ratio: {content_ratio:.3f}",
                f"visible controls at viewport: {int(metrics['visibleControls'])}",
                f"horizontal overflow: {float(metrics['horizontalOverflow']):.0f}px",
                "browser CSS zoom retained at 100%",
            ],
        ))
    selected = max(decisions, key=lambda item: item.score)
    # Leave the inspection context in exactly the selected native state. A
    # later fresh production context receives this persisted decision.
    await page.set_viewport_size({"width": selected.viewport.width, "height": selected.viewport.height})
    return selected, probes
