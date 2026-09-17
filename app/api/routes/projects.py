from typing import Optional

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.project import DEPLOYMENT_STATUSES, PROJECT_STATUSES, Project
from app.schemas.project import ProjectCreate, ProjectDeploymentUpdate, ProjectOut, ProjectUpdate
from app.services.audit import record_audit_event
from app.services.auth import AuthenticatedUser, commit_with_rls_refresh, get_current_user_org_id, require_roles
from app.services.github_service import GithubSyncError, fetch_latest_commit
from app.services.jira_service import JiraNotConfiguredError, JiraSyncError, fetch_issue_progress
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
    commit_with_rls_refresh(db, project, None)
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


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> Project:
    """Staff-only. Everything on ProjectUpdate is optional — only fields
    actually sent get changed. Closes the "no way to set staging_url" gap
    and gives Jira sync somewhere to point at (jira_project_key)."""
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    changes = payload.model_dump(exclude_unset=True)
    if "status" in changes and changes["status"] not in PROJECT_STATUSES:
        raise HTTPException(422, f"status must be one of {PROJECT_STATUSES}")
    for field, value in changes.items():
        setattr(project, field, value)

    record_audit_event(
        db,
        user=user,
        action="project.update",
        resource_type="project",
        resource_id=project.id,
        org_id=project.org_id,
        metadata=changes,
    )
    commit_with_rls_refresh(db, project, None)
    await manager.broadcast(project.id, {"type": "project_updated"})
    return project


@router.post("/{project_id}/sync-github", response_model=ProjectOut)
async def sync_project_github(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if not project.repository_url:
        raise HTTPException(422, "Set a repository_url for this project first")

    try:
        commit = fetch_latest_commit(project.repository_url)
    except GithubSyncError as exc:
        raise HTTPException(422, str(exc)) from exc

    project.latest_commit_sha = commit["sha"]
    project.latest_commit_message = commit["message"]
    project.latest_commit_synced_at = datetime.now(timezone.utc)

    record_audit_event(
        db,
        user=user,
        action="project.sync_github",
        resource_type="project",
        resource_id=project.id,
        org_id=project.org_id,
        metadata={"sha": commit["sha"]},
    )
    commit_with_rls_refresh(db, project, None)
    await manager.broadcast(project.id, {"type": "github_synced"})
    return project


@router.post("/{project_id}/sync-jira", response_model=ProjectOut)
async def sync_project_jira(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if not project.jira_project_key:
        raise HTTPException(422, "Set a jira_project_key for this project first")

    try:
        progress = fetch_issue_progress(project.jira_project_key)
    except JiraNotConfiguredError as exc:
        raise HTTPException(503, str(exc)) from exc
    except JiraSyncError as exc:
        raise HTTPException(422, str(exc)) from exc

    project.jira_issue_count = progress["issue_count"]
    project.jira_done_count = progress["done_count"]
    project.jira_synced_at = datetime.now(timezone.utc)

    record_audit_event(
        db,
        user=user,
        action="project.sync_jira",
        resource_type="project",
        resource_id=project.id,
        org_id=project.org_id,
        metadata=progress,
    )
    commit_with_rls_refresh(db, project, None)
    await manager.broadcast(project.id, {"type": "jira_synced"})
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
    commit_with_rls_refresh(db, project, None)
    await manager.broadcast(project.id, {"type": "deployment_updated"})
    return project
