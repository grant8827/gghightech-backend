import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import UserInvite, UserOut
from app.services.auth import require_roles

router = APIRouter(prefix="/api/v1/users", tags=["users"])
logger = logging.getLogger("gghightech.users")

_STAFF_ROLES = ("SUPER_ADMIN", "PROJECT_MANAGER")


def _send_clerk_invite_stub(email: str, org_id: str, role: str) -> None:
    """Stand-in for the real Clerk invitation API call (creates a magic-link
    sign-up for the user). Wired up once a Clerk account/secret key exists —
    same pattern as app/services/email.py."""
    if settings.CLERK_SECRET_KEY:
        raise NotImplementedError("CLERK_SECRET_KEY is set but the Clerk invitations API call isn't wired up yet")
    logger.info("STUB CLERK INVITE — would invite %s to org %s as %s", email, org_id, role)


@router.post("/invite", response_model=UserOut, status_code=201, dependencies=[Depends(require_roles(*_STAFF_ROLES))])
def invite_user(payload: UserInvite, db: Session = Depends(get_db)) -> User:
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(409, f"{payload.email} is already a user")

    user = User(org_id=payload.org_id, email=payload.email, full_name=payload.full_name, role=payload.role)
    db.add(user)
    db.commit()
    db.refresh(user)

    _send_clerk_invite_stub(payload.email, str(payload.org_id), payload.role)
    return user


@router.get("", response_model=list[UserOut], dependencies=[Depends(require_roles(*_STAFF_ROLES))])
def list_users(db: Session = Depends(get_db)) -> list[User]:
    return db.query(User).order_by(User.created_at.desc()).all()
