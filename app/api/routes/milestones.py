from typing import Optional

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.milestone import Milestone
from app.models.project import Project
from app.schemas.milestone import MilestoneCreate, MilestoneOut, MilestoneUpdate
from app.services.audit import record_audit_event
from app.services.auth import AuthenticatedUser, get_current_user_org_id, require_roles
from app.ws import manager

router = APIRouter(prefix="/api/v1/milestones", tags=["milestones"])

_STAFF_ROLES = ("SUPER_ADMIN", "PROJECT_MANAGER", "LEAD_ENGINEER")


@router.post("", response_model=MilestoneOut, status_code=201)
async def create_milestone(
    payload: MilestoneCreate,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> Milestone:
    milestone = Milestone(**payload.model_dump())
    db.add(milestone)
    db.flush()  # assigns milestone.id so the audit row below can reference it
    record_audit_event(
        db,
        user=user,
        action="milestone.create",
        resource_type="milestone",
        resource_id=milestone.id,
        metadata=payload.model_dump(mode="json"),
    )
    db.commit()
    db.refresh(milestone)
    await manager.broadcast(milestone.project_id, {"type": "milestones_updated"})
    return milestone


@router.get("", response_model=list[MilestoneOut])
def list_milestones(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    caller_org_id: Optional[uuid.UUID] = Depends(get_current_user_org_id),
) -> list[Milestone]:
    if caller_org_id is not None:
        project = db.get(Project, project_id)
        if not project or project.org_id != caller_org_id:
            raise HTTPException(404, "Project not found")

    return (
        db.query(Milestone)
        .filter(Milestone.project_id == project_id)
        .order_by(Milestone.created_at.asc())
        .all()
    )


@router.patch("/{milestone_id}", response_model=MilestoneOut)
async def update_milestone(
    milestone_id: uuid.UUID,
    payload: MilestoneUpdate,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> Milestone:
    milestone = db.get(Milestone, milestone_id)
    if not milestone:
        raise HTTPException(404, "Milestone not found")
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(milestone, field, value)
    record_audit_event(
        db,
        user=user,
        action="milestone.update",
        resource_type="milestone",
        resource_id=milestone.id,
        metadata=payload.model_dump(exclude_unset=True, mode="json"),
    )
    db.commit()
    db.refresh(milestone)
    await manager.broadcast(milestone.project_id, {"type": "milestones_updated"})
    return milestone
