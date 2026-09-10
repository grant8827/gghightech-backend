import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.milestone import Milestone
from app.schemas.milestone import MilestoneCreate, MilestoneOut, MilestoneUpdate
from app.services.auth import get_current_user, require_roles

router = APIRouter(prefix="/api/v1/milestones", tags=["milestones"])

_STAFF_ROLES = ("SUPER_ADMIN", "PROJECT_MANAGER", "LEAD_ENGINEER")


@router.post("", response_model=MilestoneOut, status_code=201, dependencies=[Depends(require_roles(*_STAFF_ROLES))])
def create_milestone(payload: MilestoneCreate, db: Session = Depends(get_db)) -> Milestone:
    milestone = Milestone(**payload.model_dump())
    db.add(milestone)
    db.commit()
    db.refresh(milestone)
    return milestone


@router.get("", response_model=list[MilestoneOut])
def list_milestones(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
) -> list[Milestone]:
    return (
        db.query(Milestone)
        .filter(Milestone.project_id == project_id)
        .order_by(Milestone.created_at.asc())
        .all()
    )


@router.patch(
    "/{milestone_id}", response_model=MilestoneOut, dependencies=[Depends(require_roles(*_STAFF_ROLES))]
)
def update_milestone(milestone_id: uuid.UUID, payload: MilestoneUpdate, db: Session = Depends(get_db)) -> Milestone:
    milestone = db.get(Milestone, milestone_id)
    if not milestone:
        raise HTTPException(404, "Milestone not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(milestone, field, value)
    db.commit()
    db.refresh(milestone)
    return milestone
