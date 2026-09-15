"""
Auth dependency — checks two sources, in order, for who's calling:

1. Our own self-issued JWT (app/services/local_auth.py) — the site's only
   real login, e.g. the SUPER_ADMIN created via
   `python -m app.cli create-superadmin`, or any user who's been through
   POST /auth/accept-invite.
2. DEV-MODE FALLBACK: `X-Dev-User-Role` / `X-Dev-User-Email` headers, but
   ONLY when ENVIRONMENT=development — never available once a real
   environment is set, so it can't leak into staging/production.
"""

from typing import Optional

import uuid
from dataclasses import dataclass

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.user import User
from app.services.local_auth import decode_access_token


@dataclass
class AuthenticatedUser:
    role: str
    email: Optional[str] = None
    user_id: Optional[str] = None  # set when authenticated via local_auth


async def resolve_authenticated_user(
    token: Optional[str],
    x_dev_user_role: Optional[str] = None,
    x_dev_user_email: Optional[str] = None,
) -> AuthenticatedUser:
    """Core token-resolution logic, factored out of get_current_user so it
    can also be called directly from the WebSocket route (app/api/routes/ws.py),
    which authenticates via a `token` query param instead of an
    Authorization header — browsers can't set custom headers on a
    `new WebSocket(...)` connection."""
    if token and settings.SECRET_KEY:
        try:
            claims = decode_access_token(token)
        except jwt.ExpiredSignatureError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired, please log in again") from exc
        except jwt.PyJWTError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session token") from exc

        # An invite token (POST /auth/accept-invite) is signed with the same
        # key but is not a session — reject it explicitly rather than
        # relying on the KeyError a missing "role" claim would otherwise
        # raise below.
        if claims.get("purpose") == "invite":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session token")

        return AuthenticatedUser(user_id=claims["sub"], email=claims.get("email"), role=claims["role"])

    if settings.ENVIRONMENT != "development":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Auth is not configured")

    if not x_dev_user_role:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Dev auth: send X-Dev-User-Role (and optionally X-Dev-User-Email) — "
            "SECRET_KEY is not configured yet (see .env.example).",
        )
    return AuthenticatedUser(email=x_dev_user_email, role=x_dev_user_role)


async def get_current_user(
    authorization: Optional[str] = Header(default=None),
    x_dev_user_role: Optional[str] = Header(default=None),
    x_dev_user_email: Optional[str] = Header(default=None),
) -> AuthenticatedUser:
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
    return await resolve_authenticated_user(token, x_dev_user_role, x_dev_user_email)


def resolve_org_id(db: Session, user: AuthenticatedUser) -> Optional[uuid.UUID]:
    """Resolves the caller's own org_id, for routes/sockets that must scope
    CLIENT_ADMIN/CLIENT_VIEWER callers to their own tenant (GGH-301/302).
    Returns None for staff roles (SUPER_ADMIN/PROJECT_MANAGER/LEAD_ENGINEER),
    who aren't tenant-scoped and may filter by an explicit org_id instead.
    """
    if user.role not in ("CLIENT_ADMIN", "CLIENT_VIEWER"):
        return None

    db_user: Optional[User] = None
    if user.user_id:
        db_user = db.get(User, uuid.UUID(user.user_id))
    elif user.email:
        db_user = db.query(User).filter(User.email == user.email).first()

    if not db_user:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No organization membership found for this account")

    return db_user.org_id


def get_current_user_org_id(
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> Optional[uuid.UUID]:
    """Depends()-compatible wrapper around resolve_org_id for HTTP routes."""
    return resolve_org_id(db, user)


def require_roles(*allowed_roles: str):
    """Dependency factory for RBAC-gated routes, e.g.
    `Depends(require_roles("SUPER_ADMIN", "PROJECT_MANAGER"))`."""

    async def checker(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
        if user.role not in allowed_roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires one of roles: {allowed_roles}")
        return user

    return checker
