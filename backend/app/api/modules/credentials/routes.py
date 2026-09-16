from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api import deps_state as api
from app.api.modules.credentials.schemas import CredentialCreateRequest
from app.credentials.crypto import encrypt_secret

router = APIRouter(tags=["credentials"])


@router.get("/credentials")
def list_credentials(user: dict = Depends(api.current_user)) -> list[dict]:
    """Opaque credential references from env vault and the user's durable store."""
    items = api.credential_service.list_available_references()
    if api.settings.auth_required:
        for row in api.repository.list_product_credentials_for_user(user["id"]):
            items.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "reference": row["reference"],
                    "available": True,
                    "source": "vault",
                    "project_id": row.get("project_id"),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                }
            )
    return items


@router.post("/credentials", status_code=201)
def create_credential(
    payload: CredentialCreateRequest, user: dict = Depends(api.current_user)
) -> dict:
    if not api.settings.auth_required:
        raise HTTPException(
            status_code=409, detail="durable credentials require authenticated mode"
        )
    reference = f"secret://productlens/{payload.name}"
    try:
        row = api.repository.create_product_credential(
            owner_id=user["id"],
            name=payload.name,
            reference=reference,
            username_ciphertext=encrypt_secret(payload.username, api.settings.auth_secret),
            password_ciphertext=encrypt_secret(payload.password, api.settings.auth_secret),
            project_id=payload.project_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "id": row["id"],
        "name": row["name"],
        "reference": row["reference"],
        "available": True,
        "source": "vault",
        "project_id": row.get("project_id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


@router.delete("/credentials/{credential_id}", status_code=204)
def delete_credential(credential_id: str, user: dict = Depends(api.current_user)) -> None:
    if not api.settings.auth_required:
        raise HTTPException(status_code=409, detail="durable credentials require authenticated mode")
    try:
        api.repository.delete_product_credential_for_user(credential_id, user["id"])
    except KeyError as error:
        raise HTTPException(status_code=404, detail="credential not found") from error
