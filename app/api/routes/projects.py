from typing import Optional

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.project import Project
from app.schemas.project import ProjectCreate, ProjectOut
from app.services.auth import get_current_user, require_roles

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])

_STAFF_ROLES = ("SUPER_ADMIN", "PROJECT_MANAGER", "LEAD_ENGINEER")

# NOTE: list/get are only auth-gated (any signed-in role), not org-scoped yet —
# a client can currently pass any org_id and see another org's projects. Real
# tenant isolation (scope to the caller's own org_id, or add row-level
# security) is Phase 2 work, tracked alongside the client portal (Epic 3).


@router.post("", response_model=ProjectOut, status_code=201, dependencies=[Depends(require_roles(*_STAFF_ROLES))])
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)) -> Project:
    if db.query(Project).filter(Project.slug == payload.slug).first():
        raise HTTPException(409, f"Slug {payload.slug!r} is already in use")
    project = Project(**payload.model_dump())
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.get("", response_model=list[ProjectOut])
def list_projects(
    org_id: Optional[uuid.UUID] = None,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
) -> list[Project]:
    query = db.query(Project)
    if org_id:
        query = query.filter(Project.org_id == org_id)
    return query.order_by(Project.created_at.desc()).all()


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: uuid.UUID, db: Session = Depends(get_db), _user=Depends(get_current_user)) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project
