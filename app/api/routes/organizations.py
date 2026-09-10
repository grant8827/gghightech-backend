from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.organization import Organization
from app.schemas.organization import OrganizationCreate, OrganizationOut
from app.services.auth import require_roles

router = APIRouter(prefix="/api/v1/organizations", tags=["organizations"])

# Only staff roles manage organizations/clients — not CLIENT_ADMIN/CLIENT_VIEWER.
_STAFF_ROLES = ("SUPER_ADMIN", "PROJECT_MANAGER")


@router.post("", response_model=OrganizationOut, status_code=201, dependencies=[Depends(require_roles(*_STAFF_ROLES))])
def create_organization(payload: OrganizationCreate, db: Session = Depends(get_db)) -> Organization:
    org = Organization(**payload.model_dump())
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


@router.get("", response_model=list[OrganizationOut], dependencies=[Depends(require_roles(*_STAFF_ROLES))])
def list_organizations(db: Session = Depends(get_db)) -> list[Organization]:
    return db.query(Organization).order_by(Organization.created_at.desc()).all()
