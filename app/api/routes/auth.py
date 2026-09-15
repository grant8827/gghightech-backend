from typing import Optional

import uuid

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import AcceptInviteRequest, LoginRequest, LoginResponse
from app.services.audit import record_audit_event
from app.services.auth import AuthenticatedUser, get_current_user
from app.services.local_auth import (
    create_access_token,
    create_or_update_superadmin,
    decode_invite_token,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    if not settings.SECRET_KEY:
        raise HTTPException(503, "Local login is not configured on this server")

    user = db.query(User).filter(User.email == payload.email).first()
    # Deliberately identical error for "no such user" and "wrong password" —
    # doesn't tell an attacker which part was wrong.
    if not user or not user.password_hash or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Incorrect email or password")

    token = create_access_token(str(user.id), user.email, user.role)
    return LoginResponse(access_token=token, role=user.role, email=user.email, full_name=user.full_name)


@router.post("/accept-invite", response_model=LoginResponse)
def accept_invite(payload: AcceptInviteRequest, db: Session = Depends(get_db)) -> LoginResponse:
    """Second half of app/api/routes/users.py's invite_user: the emailed
    link lands here so the invited user can set their own password. Signs
    them in immediately on success (same response shape as /login) rather
    than making them turn around and log in again."""
    if not settings.SECRET_KEY:
        raise HTTPException(503, "Local login is not configured on this server")

    try:
        claims = decode_invite_token(payload.token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "This invite link has expired")
    except jwt.PyJWTError:
        raise HTTPException(401, "This invite link is invalid")

    user = db.get(User, uuid.UUID(claims["sub"]))
    if not user:
        raise HTTPException(404, "User not found")
    if user.password_hash:
        raise HTTPException(409, "This invite has already been used — sign in normally instead")
    if len(payload.password) < 12:
        raise HTTPException(422, "Password must be at least 12 characters")

    user.password_hash = hash_password(payload.password)
    record_audit_event(
        db,
        user=AuthenticatedUser(user_id=str(user.id), email=user.email, role=user.role),
        action="user.accept_invite",
        resource_type="user",
        resource_id=user.id,
        org_id=user.org_id,
        metadata={},
    )
    db.commit()

    token = create_access_token(str(user.id), user.email, user.role)
    return LoginResponse(access_token=token, role=user.role, email=user.email, full_name=user.full_name)


@router.get("/me")
def me(user: AuthenticatedUser = Depends(get_current_user)) -> dict:
    """Lets the frontend verify a stored token is still valid and see who
    it belongs to, without needing to decode the JWT client-side."""
    return {"email": user.email, "role": user.role}


@router.post("/bootstrap-superadmin", status_code=201)
def bootstrap_superadmin(
    payload: LoginRequest,
    full_name: Optional[str] = None,
    x_bootstrap_token: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict:
    """One-time way to create the first SUPER_ADMIN in an environment with
    no shell/DB access (e.g. a managed platform where `railway ssh`-style
    access isn't available). Inert unless BOOTSTRAP_TOKEN is set — set it,
    call this once, then unset it. See app/core/config.py.

    Every failure mode (missing token, wrong token, no full_name) returns
    the same plain 404 — nothing distinguishes "this endpoint doesn't
    exist" from "you got a parameter wrong", by design."""
    if not settings.BOOTSTRAP_TOKEN or x_bootstrap_token != settings.BOOTSTRAP_TOKEN or not full_name:
        raise HTTPException(404)
    if len(payload.password) < 12:
        raise HTTPException(422, "Password must be at least 12 characters")

    user = create_or_update_superadmin(db, payload.email, payload.password, full_name)
    return {"email": user.email, "role": user.role, "id": str(user.id)}
