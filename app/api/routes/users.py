import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import UserInvite, UserOut
from app.services.audit import record_audit_event
from app.services.auth import AuthenticatedUser, get_current_user, require_roles

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


@router.post("/invite", response_model=UserOut, status_code=201)
def invite_user(
    payload: UserInvite,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> User:
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(409, f"{payload.email} is already a user")

    user = User(org_id=payload.org_id, email=payload.email, full_name=payload.full_name, role=payload.role)
    db.add(user)
    db.flush()  # assigns user.id so the audit row below can reference it
    record_audit_event(
        db,
        user=current_user,
        action="user.invite",
        resource_type="user",
        resource_id=user.id,
        org_id=user.org_id,
        metadata={"email": payload.email, "role": payload.role},
    )
    db.commit()
    db.refresh(user)

    _send_clerk_invite_stub(payload.email, str(payload.org_id), payload.role)
    return user


@router.get("", response_model=list[UserOut], dependencies=[Depends(require_roles(*_STAFF_ROLES))])
def list_users(db: Session = Depends(get_db)) -> list[User]:
    return db.query(User).order_by(User.created_at.desc()).all()


@router.delete("/{user_id}", status_code=204)
def delete_user(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles("SUPER_ADMIN")),
) -> None:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if current_user.user_id and str(user.id) == current_user.user_id:
        raise HTTPException(400, "Cannot delete your own account")
    record_audit_event(
        db,
        user=current_user,
        action="user.delete",
        resource_type="user",
        resource_id=user.id,
        org_id=user.org_id,
        metadata={"email": user.email, "role": user.role},
    )
    db.delete(user)
    db.commit()
