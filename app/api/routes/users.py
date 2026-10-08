import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.organization import Organization
from app.models.user import STAFF_USER_ROLES, User
from app.schemas.user import UserInvite, UserOut
from app.services.audit import record_audit_event
from app.services.auth import AuthenticatedUser, require_roles
from app.services.email import send_invite_email
from app.services.local_auth import create_invite_token, is_internal_org, revoke_sessions

router = APIRouter(prefix="/api/v1/users", tags=["users"])

_STAFF_ROLES = ("SUPER_ADMIN", "PROJECT_MANAGER")


@router.post("/invite", response_model=UserOut, status_code=201)
def invite_user(
    payload: UserInvite,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> User:
    """Who may create which kind of account:

    - Staff roles (STAFF_USER_ROLES) can only be granted by a SUPER_ADMIN.
      A PROJECT_MANAGER may invite client users only — otherwise they
      could invite themselves a SUPER_ADMIN account.
    - Staff accounts belong to the internal organization, client accounts
      to a client organization, never the other way round.
    """
    inviting_staff = payload.role in STAFF_USER_ROLES

    if inviting_staff and current_user.role != "SUPER_ADMIN":
        # Recorded: an attempt to hand out staff access without the right
        # to is worth being able to find later.
        record_audit_event(
            db,
            user=current_user,
            action="user.invite_denied",
            resource_type="user",
            org_id=payload.org_id,
            metadata={"email": payload.email, "role": payload.role},
        )
        db.commit()
        raise HTTPException(403, "Only a SUPER_ADMIN can invite staff accounts")

    org = db.get(Organization, payload.org_id)
    if not org:
        raise HTTPException(404, "Organization not found")
    if inviting_staff and not is_internal_org(org):
        raise HTTPException(422, "Staff accounts must belong to the internal organization")
    if not inviting_staff and is_internal_org(org):
        raise HTTPException(422, "Client accounts cannot be added to the internal organization")

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

    invite_token = create_invite_token(str(user.id))
    invite_link = f"{settings.FRONTEND_URL}/accept-invite?token={invite_token}"
    send_invite_email(user.email, user.full_name, invite_link)
    return user


@router.get("", response_model=list[UserOut], dependencies=[Depends(require_roles(*_STAFF_ROLES))])
def list_users(db: Session = Depends(get_db)) -> list[User]:
    return db.query(User).order_by(User.created_at.desc()).all()


@router.post("/{user_id}/revoke-sessions", status_code=204)
def revoke_user_sessions(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles("SUPER_ADMIN")),
) -> None:
    """Signs a user out everywhere without deleting them — for a lost
    device or a suspected stolen token. They can sign in again with their
    password straight away."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    revoke_sessions(user)
    record_audit_event(
        db,
        user=current_user,
        action="user.revoke_sessions",
        resource_type="user",
        resource_id=user.id,
        org_id=user.org_id,
        metadata={"email": user.email},
    )
    db.commit()


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
