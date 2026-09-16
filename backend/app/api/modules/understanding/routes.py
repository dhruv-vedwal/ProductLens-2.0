from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api import deps_state as api
from app.api.modules.understanding.schemas import (
    KnowledgeInvalidationRequest,
    UnderstandingRequest,
)
from app.contracts.models import UnderstandingPreview
from app.providers.errors import ProviderError

router = APIRouter(tags=["understanding"])


@router.post("/understanding/preview", response_model=UnderstandingPreview)
async def understanding_preview(
    payload: UnderstandingRequest,
    _: dict = Depends(api.current_user),
) -> UnderstandingPreview:
    """Run a bounded, non-recording product scan before generation.

    This endpoint is intentionally separate from ``POST /runs``.  It gives a
    prompt assistant enough grounded context to refine a request, while the
    eventual generation run still performs its own fresh discovery and
    validation.
    """
    try:
        return await api.preflight_service.preview(
            url=str(payload.url),
            prompt=payload.prompt,
            audience=payload.audience,
            max_pages=payload.max_pages,
            use_stagehand=payload.use_stagehand,
            force_refresh=payload.force_refresh,
        )
    except (ProviderError, RuntimeError, OSError, ValueError) as error:
        raise HTTPException(
            status_code=502, detail=f"preflight understanding failed: {type(error).__name__}"
        ) from error


@router.post("/knowledge/invalidate")
def invalidate_product_knowledge(
    payload: KnowledgeInvalidationRequest, user: dict = Depends(api.current_user)
) -> dict:
    product_key = str(payload.url)
    equivalent_keys = tuple(dict.fromkeys((product_key, product_key.rstrip("/"))))
    if api.settings.auth_required and not any(
        api.repository.user_has_product_access(key, user["id"]) for key in equivalent_keys
    ):
        raise HTTPException(status_code=404, detail="product knowledge not found in your workspace")
    invalidated = any(api.repository.invalidate_knowledge(key) for key in equivalent_keys)
    return {"url": product_key, "invalidated": invalidated}
