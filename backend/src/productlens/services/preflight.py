"""Bounded, non-recording product understanding before a generation run.

The preflight service is intentionally descriptive.  It may inspect a public
site and ask the configured semantic observer for suggestions, but it never
records a video or dispatches a production workflow.  Generation must still
re-ground this evidence in its own discovery stage.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import async_playwright

from productlens.contracts.models import DiscoveryBudget, ObjectiveSpec, UnderstandingPreview
from productlens.discovery.live import LiveDiscovery, _objective_spec
from productlens.observability.logging import redact_prompt_text
from productlens.providers.errors import ProviderError
from productlens.urls import canonical_product_url


def canonical_url(value: str) -> str:
    return canonical_product_url(value)


def _suggested_prompt(objective: ObjectiveSpec, *, areas: list[str]) -> str:
    subject = objective.primary_entity or (areas[0] if areas else "the product")
    mode = objective.demo_type.replace("_", " ")
    audience = objective.audience or "a product prospect"
    if objective.raw.strip():
        base = objective.raw.strip().rstrip(".!?")
        context = ", ".join(areas[:3])
        detail = (
            f" Focus on the visible areas {context} and the resulting user value."
            if context
            else " Explain the purpose, key steps, and visible outcome."
        )
        return f"{base} for {audience}.{detail}"
    context = ", ".join(areas[:4])
    suffix = f" Prioritize the visible areas: {context}." if context else ""
    return f"Create a {mode} for {audience} showing {subject}.{suffix}".replace("  ", " ")


class PreflightService:
    def __init__(self, *, generator, artifact_root: Path, stagehand_provider=None, repository=None):
        self.generator = generator
        self.discovery = getattr(generator, "discovery", None) or LiveDiscovery()
        self.artifact_root = Path(artifact_root)
        self.stagehand_provider = stagehand_provider
        self.repository = repository

    def _cache_path(self, url: str, prompt: str) -> Path:
        key = hashlib.sha256(f"{canonical_url(url)}\n{prompt.strip()}".encode()).hexdigest()[:32]
        return self.artifact_root / "preflight" / key / "understanding.json"

    @staticmethod
    def _fresh(path: Path, max_age_seconds: int = 86_400) -> bool:
        try:
            return (datetime.now(UTC).timestamp() - path.stat().st_mtime) <= max_age_seconds
        except OSError:
            return False

    async def preview(
        self,
        *,
        url: str,
        prompt: str = "",
        audience: str | None = None,
        max_pages: int = 3,
        use_stagehand: bool = True,
        force_refresh: bool = False,
    ) -> UnderstandingPreview:
        target = canonical_url(url)
        cache_path = self._cache_path(target, prompt)
        if self.repository is not None and not force_refresh:
            cached_payload = self.repository.fresh_understanding_preview(target, prompt)
            if cached_payload:
                try:
                    return UnderstandingPreview.model_validate(cached_payload).model_copy(
                        update={"cached": True}
                    )
                except (ValueError, TypeError):
                    pass
        if cache_path.is_file() and self._fresh(cache_path) and not force_refresh:
            try:
                return UnderstandingPreview.model_validate(
                    json.loads(cache_path.read_text(encoding="utf-8"))
                ).model_copy(update={"cached": True})
            except (OSError, ValueError, TypeError):
                pass

        objective_text = (
            prompt.strip()
            or "Give a concise overview of the product and its most important visible areas."
        )
        if self.generator is not None:
            objective, _understanding = await self.generator._understand_objective(objective_text)
        else:
            objective = _objective_spec(objective_text)
        if audience and audience.strip():
            objective = objective.model_copy(update={"audience": audience.strip()[:160]})
        artifact_dir = cache_path.parent
        screenshot_dir = artifact_dir / "screenshots"
        context = None
        stagehand_note = None
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                context_page = await browser.new_page(viewport={"width": 1440, "height": 900})
                try:
                    await context_page.goto(target, wait_until="domcontentloaded", timeout=45_000)
                    await context_page.wait_for_timeout(700)
                    context = await self.discovery.discover(
                        context_page,
                        objective_text,
                        DiscoveryBudget(
                            max_time_seconds=45,
                            max_pages=max(1, min(max_pages, 4)),
                            max_actions=8,
                            max_model_calls=1,
                        ),
                        objective_spec=objective,
                        explore_visible_routes=False,
                        screenshot_directory=screenshot_dir,
                    )
                    if use_stagehand and self.stagehand_provider is not None:
                        try:
                            if self.generator is not None:
                                context, observation = await self.generator._stagehand_enrich(
                                    context_page, context, objective_text, environment="LOCAL"
                                )
                            else:
                                observation = await self.stagehand_provider.observe(
                                    url=context_page.url,
                                    instruction=f"Observe visible safe controls relevant to: {redact_prompt_text(objective_text)}",
                                    analysis_instruction="List only visible sections and safe read-only controls.",
                                    environment="LOCAL",
                                    cache_dir=artifact_dir / "stagehand-cache",
                                )
                                context = await self.discovery.enrich_with_stagehand(
                                    context_page, context, observation
                                )
                            stagehand_note = (
                                observation.get("status")
                                if isinstance(observation, dict)
                                else "OBSERVED"
                            )
                        except (ProviderError, RuntimeError, OSError) as error:
                            stagehand_note = f"UNAVAILABLE:{type(error).__name__}"
                finally:
                    await context_page.close()
                    await browser.close()
        except Exception as error:  # noqa: BLE001 - preflight must return a classified result
            status = "AUTH_REQUIRED" if "AUTH_REQUIRED" in str(error) else "FAILED"
            preview = UnderstandingPreview(
                status=status,
                url=target,
                prompt=prompt,
                suggested_prompt=prompt.strip()
                or "Describe the product's most important visible workflow.",
                objective=objective,
                assumptions=[
                    "The product could not be fully inspected during the bounded preflight."
                ],
                blockers=[type(error).__name__],
                evidence_refs=["preflight:error"],
            )
            artifact_dir.mkdir(parents=True, exist_ok=True)
            artifact_dir.joinpath("understanding.json").write_text(
                json.dumps(preview.model_dump(mode="json"), indent=2), encoding="utf-8"
            )
            if self.repository is not None:
                self.repository.save_understanding_preview(
                    target, prompt, preview.model_dump(mode="json")
                )
            return preview

        assert context is not None
        fingerprint_payload = {
            "url": target,
            "title": context.title,
            "pages": sorted((page.url, page.fingerprint) for page in context.page_knowledge),
            "routes": sorted(context.relevant_routes),
        }
        product_fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        areas = list(
            dict.fromkeys(
                [
                    *[
                        str(item)
                        for page in context.page_knowledge
                        for item in page.visible_sections
                    ],
                    *[str(item) for item in context.content_blocks],
                    *[str(item.name) for item in context.navigation if item.name],
                ]
            )
        )[:40]
        relationships = [
            relation.model_dump(mode="json") for relation in getattr(context, "relationships", [])
        ]
        # Preserve explicit intent even when a bounded scan cannot match one
        # endpoint to a page; the low-confidence edge is useful to the client
        # as a transparent assumption rather than an invented route.
        relationships.extend(
            {
                "source": relation.source,
                "target": relation.target,
                "relation": relation.relation,
                "required": True,
                "evidence_refs": ["objective_relationship"],
                "confidence": 0.2,
            }
            for relation in objective.supporting_relationships
            if not any(
                item.get("source") == relation.source and item.get("target") == relation.target
                for item in relationships
            )
        )
        assumptions = []
        if stagehand_note and stagehand_note.startswith("UNAVAILABLE"):
            assumptions.append(
                "Semantic observation was unavailable; visible DOM evidence remains authoritative."
            )
        if context.authentication_state == "unknown":
            assumptions.append(
                "Authentication state was not conclusively determined during the bounded scan."
            )
        status = (
            "AUTH_REQUIRED"
            if context.authentication_state == "login_required"
            else ("BLOCKED" if context.blockers else "READY")
        )
        preview = UnderstandingPreview(
            status=status,
            url=target,
            prompt=prompt,
            suggested_prompt=_suggested_prompt(objective, areas=areas),
            objective=objective,
            product_title=context.title,
            application_type=context.application_type,
            product_fingerprint=product_fingerprint,
            knowledge_version=product_fingerprint[:16],
            relevant_areas=areas,
            relationships=list(
                {json.dumps(item, sort_keys=True): item for item in relationships}.values()
            )[:40],
            assumptions=assumptions,
            blockers=list(context.blockers)[:20],
            evidence_refs=list(context.evidence)[:80] or ["preflight:visible-dom"],
            pages_inspected=[page.url for page in context.page_knowledge[:20]],
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(preview.model_dump(mode="json"), indent=2), encoding="utf-8"
        )
        if self.repository is not None:
            self.repository.save_understanding_preview(
                target, prompt, preview.model_dump(mode="json")
            )
        return preview
