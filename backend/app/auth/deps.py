"""FastAPI auth dependencies shared by JSON and media/SSE routes."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Header, HTTPException, Query

from app.auth.security import decode_access_token
from app.config.settings import Settings


def resolve_current_user(
    *,
    repository,
    settings: Settings,
    authorization: str | None = None,
    token: str | None = None,
) -> dict:
    """Resolve the signed session user from Bearer and/or ``?token=``.

    Media and SSE endpoints often cannot set ``Authorization`` (``<video>``,
    ``EventSource``). Mutating JSON APIs should still pass Bearer-only callers.
    """
    if not settings.auth_required:
        return {
            "id": "local-studio",
            "email": "local@productlens.invalid",
            "display_name": "Local Studio",
            "theme_preference": "system",
        }
    raw: str | None = None
    if authorization and authorization.startswith("Bearer "):
        raw = authorization.removeprefix("Bearer ").strip() or None
    if not raw and token:
        raw = token.strip() or None
    if not raw:
        raise HTTPException(status_code=401, detail="sign in to access your ProductLens workspace")
    claims = decode_access_token(raw, settings.auth_secret)
    if not claims or not repository.active_session(claims["sid"], claims["sub"]):
        raise HTTPException(status_code=401, detail="your session has expired; sign in again")
    try:
        return repository.get_user(claims["sub"])
    except KeyError as error:
        raise HTTPException(status_code=401, detail="account is unavailable") from error


def bearer_only_factory(repository, get_settings: Callable[[], Settings]):
    def current_user(authorization: str | None = Header(default=None)) -> dict:
        return resolve_current_user(
            repository=repository, settings=get_settings(), authorization=authorization
        )

    return current_user


def bearer_or_query_factory(repository, get_settings: Callable[[], Settings]):
    def current_user_bearer_or_query(
        authorization: str | None = Header(default=None),
        token: str | None = Query(default=None),
    ) -> dict:
        return resolve_current_user(
            repository=repository,
            settings=get_settings(),
            authorization=authorization,
            token=token,
        )

    return current_user_bearer_or_query
