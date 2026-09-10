"""
Auth dependency backed by Clerk session tokens.

The frontend (via @clerk/nextjs) attaches a Clerk session JWT as a Bearer
token; this verifies it against Clerk's JWKS and returns the claims.

DEV-MODE FALLBACK: CLERK_JWKS_URL is blank until the Clerk account exists
and its keys are added to .env (see .env.example). Until then, requests may
instead send `X-Dev-User-Role` / `X-Dev-User-Email` headers to simulate a
signed-in user — but ONLY when ENVIRONMENT=development. This unblocks
building GGH-401 admin endpoints without waiting on Clerk sign-up, and is
hard-disabled the moment real Clerk keys (or a non-dev environment) are
configured, so it can never leak into staging/production.
"""

from typing import Optional

from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Depends, Header, HTTPException, status
from jwt import PyJWKClient

from app.core.config import settings


@dataclass
class AuthenticatedUser:
    clerk_user_id: str
    email: Optional[str]
    role: str


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
    if _clerk_configured():
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
        token = authorization.removeprefix("Bearer ").strip()
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
