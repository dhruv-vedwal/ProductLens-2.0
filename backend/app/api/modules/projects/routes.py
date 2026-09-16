from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api import deps_state as api
from app.api.modules.projects.schemas import ProjectRequest

router = APIRouter(tags=["projects"])


@router.get("/projects")
def list_projects(user: dict = Depends(api.current_user)) -> list[dict]:
    """List studio workspaces and their request counts."""
    if api.settings.auth_required:
        api.repository.ensure_user_project(user["id"])
        return api.repository.list_projects(owner_id=user["id"])
    api.repository.ensure_local_project()
    return api.repository.list_projects()


@router.post("/projects")
def create_project(payload: ProjectRequest, user: dict = Depends(api.current_user)) -> dict:
    try:
        return api.repository.create_project(
            payload.name, owner_id=user["id"] if api.settings.auth_required else None
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.patch("/projects/{project_id}")
def rename_project(
    project_id: str, payload: ProjectRequest, user: dict = Depends(api.current_user)
) -> dict:
    try:
        if api.settings.auth_required:
            return api.repository.rename_project_for_user(project_id, user["id"], payload.name)
        return api.repository.rename_project(project_id, payload.name)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="project not found") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
