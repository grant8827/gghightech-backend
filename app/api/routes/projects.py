from typing import Optional

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.project import DEPLOYMENT_STATUSES, Project
from app.schemas.project import ProjectCreate, ProjectDeploymentUpdate, ProjectOut
from app.services.audit import record_audit_event
from app.services.auth import AuthenticatedUser, get_current_user_org_id, require_roles
from app.ws import manager

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])

_STAFF_ROLES = ("SUPER_ADMIN", "PROJECT_MANAGER", "LEAD_ENGINEER")

# list/get are tenant-scoped via get_current_user_org_id: a CLIENT_ADMIN/
# CLIENT_VIEWER caller is always restricted to their own org_id (any org_id
# they pass is ignored), while staff roles are unrestricted and may filter
# by an explicit org_id as before.


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(
    payload: ProjectCreate,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> Project:
    if db.query(Project).filter(Project.slug == payload.slug).first():
        raise HTTPException(409, f"Slug {payload.slug!r} is already in use")
    project = Project(**payload.model_dump())
    db.add(project)
    db.flush()  # assigns project.id so the audit row below can reference it
    record_audit_event(
        db,
        user=user,
        action="project.create",
        resource_type="project",
        resource_id=project.id,
        org_id=project.org_id,
        metadata=payload.model_dump(mode="json"),
    )
    db.commit()
    db.refresh(project)
    return project


@router.get("", response_model=list[ProjectOut])
def list_projects(
    org_id: Optional[uuid.UUID] = None,
    db: Session = Depends(get_db),
    caller_org_id: Optional[uuid.UUID] = Depends(get_current_user_org_id),
) -> list[Project]:
    if caller_org_id is not None:
        org_id = caller_org_id

    query = db.query(Project)
    if org_id:
        query = query.filter(Project.org_id == org_id)
    return query.order_by(Project.created_at.desc()).all()


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    caller_org_id: Optional[uuid.UUID] = Depends(get_current_user_org_id),
) -> Project:
    project = db.get(Project, project_id)
    if not project or (caller_org_id is not None and project.org_id != caller_org_id):
        raise HTTPException(404, "Project not found")
    return project


@router.patch("/{project_id}/deployment", response_model=ProjectOut)
async def update_project_deployment(
    project_id: uuid.UUID,
    payload: ProjectDeploymentUpdate,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> Project:
    """GGH-302 — records a deployment manually until a real CI webhook calls
    this instead. Staff-only: this isn't exposed to CLIENT_* callers, who
    only ever read the resulting fields via GET /projects/{id}."""
    if payload.status not in DEPLOYMENT_STATUSES:
        raise HTTPException(422, f"status must be one of {DEPLOYMENT_STATUSES}")

    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    project.last_deploy_commit_sha = payload.commit_sha
    project.last_deploy_status = payload.status
    project.last_deployed_at = datetime.now(timezone.utc)

    record_audit_event(
        db,
        user=user,
        action="project.deployment_update",
        resource_type="project",
        resource_id=project.id,
        org_id=project.org_id,
        metadata=payload.model_dump(mode="json"),
    )
    db.commit()
    db.refresh(project)
    await manager.broadcast(project.id, {"type": "deployment_updated"})
    return project
