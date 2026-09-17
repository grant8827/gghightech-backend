from typing import Optional

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.project import Project
from app.models.project_update import ProjectUpdate
from app.models.user import User
from app.schemas.project_update import ProjectUpdateCreate, ProjectUpdateOut
from app.services.auth import AuthenticatedUser, get_current_user_org_id, require_roles
from app.ws import manager

router = APIRouter(prefix="/api/v1/projects", tags=["updates"])

_STAFF_ROLES = ("SUPER_ADMIN", "PROJECT_MANAGER", "LEAD_ENGINEER")


@router.post("/{project_id}/updates", response_model=ProjectUpdateOut, status_code=201)
async def post_project_update(
    project_id: uuid.UUID,
    payload: ProjectUpdateCreate,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> ProjectUpdate:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    author = db.get(User, uuid.UUID(user.user_id)) if user.user_id else None
    update = ProjectUpdate(
        project_id=project.id,
        org_id=project.org_id,
        author_id=user.user_id or "",
        author_name=author.full_name if author else (user.email or "Staff"),
        author_role=user.role,
        message=payload.message,
    )
    db.add(update)
    db.commit()
    db.refresh(update)
    await manager.broadcast(project.id, {"type": "status_update"})
    return update


@router.get("/{project_id}/updates", response_model=list[ProjectUpdateOut])
def list_project_updates(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    caller_org_id: Optional[uuid.UUID] = Depends(get_current_user_org_id),
) -> list[ProjectUpdate]:
    if caller_org_id is not None:
        project = db.get(Project, project_id)
        if not project or project.org_id != caller_org_id:
            raise HTTPException(404, "Project not found")

    return (
        db.query(ProjectUpdate)
        .filter(ProjectUpdate.project_id == project_id)
        .order_by(ProjectUpdate.created_at.desc())
        .all()
    )
