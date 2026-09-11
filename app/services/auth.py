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

from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Depends, Header, HTTPException, status
from jwt import PyJWKClient

from app.core.config import settings
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


async def get_current_user(
    authorization: Optional[str] = Header(default=None),
    x_dev_user_role: Optional[str] = Header(default=None),
    x_dev_user_email: Optional[str] = Header(default=None),
) -> AuthenticatedUser:
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()

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
    return AuthenticatedUser(clerk_user_id="dev-user", email=x_dev_user_email, role=x_dev_user_role)


def require_roles(*allowed_roles: str):
    """Dependency factory for RBAC-gated routes, e.g.
    `Depends(require_roles("SUPER_ADMIN", "PROJECT_MANAGER"))`."""

    async def checker(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
        if user.role not in allowed_roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires one of roles: {allowed_roles}")
        return user

    return checker
