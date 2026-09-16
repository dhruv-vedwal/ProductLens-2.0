from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException

from app.api import deps_state as api
from app.api.modules.auth.schemas import LoginRequest, PreferenceRequest, SignupRequest
from app.auth.security import decode_access_token, hash_password, verify_password

router = APIRouter(tags=["auth"])


@router.post("/auth/signup", status_code=201)
def signup(payload: SignupRequest) -> dict[str, object]:
    try:
        user = api.repository.create_user(
            email=payload.email,
            password_hash=hash_password(payload.password),
            display_name=payload.display_name,
        )
        api.repository.ensure_user_project(user["id"])
        return api.issue_session(user)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/auth/login")
def login(payload: LoginRequest) -> dict[str, object]:
    user = api.repository.get_user_by_email(payload.email)
    if not user or not verify_password(payload.password, user.get("password_hash")):
        raise HTTPException(status_code=401, detail="email or password is incorrect")
    return api.issue_session(user)


@router.get("/auth/me")
def me(user: dict = Depends(api.current_user)) -> dict[str, str | None]:
    return api.public_user(user)


@router.post("/auth/logout", status_code=204)
def logout(
    authorization: str | None = Header(default=None), user: dict = Depends(api.current_user)
) -> None:
    if authorization:
        claims = decode_access_token(
            authorization.removeprefix("Bearer ").strip(), api.settings.auth_secret
        )
        if claims:
            api.repository.revoke_session(claims["sid"], user["id"])


@router.patch("/auth/preferences")
def preferences(
    payload: PreferenceRequest, user: dict = Depends(api.current_user)
) -> dict[str, str | None]:
    return api.public_user(
        api.repository.update_user_preferences(
            user["id"], theme_preference=payload.theme_preference
        )
    )
