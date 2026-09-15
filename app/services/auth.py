"""
Auth dependency — checks three sources, in order, for who's calling:

1. Our own self-issued JWT (app/services/local_auth.py) — real staff login,
   e.g. the SUPER_ADMIN created via `python -m app.cli create-superadmin`.
   Checked first since it's a local signature check, no network call.
2. A Clerk session JWT, once CLERK_JWKS_URL/CLERK_ISSUER are configured.
3. DEV-MODE FALLBACK: `X-Dev-User-Role` / `X-Dev-User-Email` headers, but
   ONLY when ENVIRONMENT=development — never available once a real
   environment is set, so it can't leak into staging/production.
"""

from typing import Optional

import uuid
from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Depends, Header, HTTPException, status
from jwt import PyJWKClient
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
    clerk_user_id: Optional[str] = None  # set when authenticated via Clerk


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    return PyJWKClient(settings.CLERK_JWKS_URL)


def _clerk_configured() -> bool:
    return bool(settings.CLERK_JWKS_URL and settings.CLERK_ISSUER)


async def resolve_authenticated_user(
    token: Optional[str],
    x_dev_user_role: Optional[str] = None,
    x_dev_user_email: Optional[str] = None,
) -> AuthenticatedUser:
    """Core token-resolution logic, factored out of get_current_user so it
    can also be called directly from the WebSocket route (app/ws_routes.py),
    which authenticates via a `token` query param instead of an
    Authorization header — browsers can't set custom headers on a
    `new WebSocket(...)` connection."""
    if token and settings.SECRET_KEY:
        try:
            claims = decode_access_token(token)
            return AuthenticatedUser(user_id=claims["sub"], email=claims.get("email"), role=claims["role"])
        except jwt.ExpiredSignatureError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired, please log in again") from exc
        except jwt.PyJWTError:
            pass  # not one of ours (wrong signature) — might be a Clerk token, keep trying

    if _clerk_configured():
        if not token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
        try:
            signing_key = _jwks_client().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=settings.CLERK_ISSUER,
                options={"verify_aud": False},
            )
        except jwt.PyJWTError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token: {exc}") from exc

        return AuthenticatedUser(
            clerk_user_id=claims["sub"],
            email=claims.get("email"),
            role=claims.get("public_metadata", {}).get("role", "CLIENT_VIEWER"),
        )

    if settings.ENVIRONMENT != "development":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Auth is not configured")

    if not x_dev_user_role:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Dev auth: send X-Dev-User-Role (and optionally X-Dev-User-Email) — "
            "Clerk is not configured yet (see .env.example).",
        )
    # No clerk_user_id here (deliberately not a "dev-user" placeholder): a
    # non-null clerk_user_id would take priority over email in resolve_org_id
    # below and never match any real row, breaking org-scoped dev testing
    # via X-Dev-User-Email for CLIENT_ADMIN/CLIENT_VIEWER.
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
    elif user.clerk_user_id:
        db_user = db.query(User).filter(User.clerk_user_id == user.clerk_user_id).first()
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
