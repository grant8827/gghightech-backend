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
from sqlalchemy import text
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


def _apply_rls_context(db: Session, org_id: Optional[uuid.UUID]) -> None:
    """Sets the two Postgres session vars the row-level-security policies
    (see the "tenant_isolation" migration) key off. This is defense-in-depth
    *under* the org_id filtering routes already do themselves
    (get_current_user_org_id et al.) — it doesn't replace that filtering, it
    backstops it so a missed WHERE clause or a future raw query still can't
    cross tenants.

    org_id=None means "staff" (bypasses RLS entirely, matching their existing
    unrestricted app-level access); a UUID scopes strictly to that org.

    SET LOCAL, not plain SET: this used to be a session-level SET on the
    theory that it needed to survive a route's later db.commit() (e.g.
    create-then-refresh) — SET LOCAL's effect ends at commit, so the
    refresh's SELECT would otherwise run with no context at all. That
    theory was wrong in a way that only showed up under real concurrent
    load: SQLAlchemy's Session doesn't keep the same physical connection
    across a commit — db.commit() ends the transaction and *may* hand the
    Session a different pooled connection for the next one. A session-level
    SET lives on whichever connection it was run on; if commit swaps
    connections, the "survives commits" SET is sitting on a connection
    nobody's using anymore, and the new one never got set at all. This
    reliably reproduced through the browser (several concurrent requests
    contending for the pool) and never through sequential curl (no
    contention, so the same connection kept getting reused by luck).

    SET LOCAL doesn't have that failure mode, because it's not trying to
    outlive its transaction — it's tied to the current transaction on
    whichever connection is *actually* live right now. Any route that
    commits mid-request and then runs another RLS-relevant query must
    re-apply this (see commit_with_rls_refresh below) rather than assume
    the first call's effect carries forward.

    Deliberately never SET app.current_org_id to '' or leave it untouched
    on the bypass path — the policy (see migration e90e0a659dc5) has to
    tolerate that GUC coming back as an empty string, not just NULL: once
    a pooled connection has had it SET at least once, RESET reverts it to
    '' (not NULL), and Postgres doesn't guarantee OR short-circuits, so
    bypass_rls=true alone can't be trusted to protect the ::uuid cast from
    ever seeing it."""
    if org_id is not None:
        db.execute(text("SET LOCAL app.bypass_rls = 'false'"))
        # org_id is always a uuid.UUID here (never raw user input), so
        # stringifying it into the SQL is safe — SET doesn't support bind
        # parameters the way ordinary queries do.
        db.execute(text(f"SET LOCAL app.current_org_id = '{org_id}'"))
    else:
        db.execute(text("SET LOCAL app.bypass_rls = 'true'"))


def commit_with_rls_refresh(db: Session, obj, org_id: Optional[uuid.UUID]) -> None:
    """db.commit() followed by db.refresh(obj), safe under RLS.

    Every route that commits and then refreshes an RLS-protected row
    (projects, milestones, invoices, project_updates) must use this instead
    of calling db.commit()/db.refresh() directly — see the SET LOCAL note
    on _apply_rls_context above for why the naive version silently returns
    zero rows under concurrent load. commit() ends the current transaction,
    the refresh's SELECT starts a new one, and _apply_rls_context has to be
    re-run for *that* transaction, on whatever connection actually ends up
    serving it, rather than trusting the pre-commit SET to still apply.

    `org_id` should be the same value the route's own require_roles/
    resolve_org_id call already resolved (None for staff)."""
    db.commit()
    _apply_rls_context(db, org_id)
    db.refresh(obj)


def resolve_org_id(db: Session, user: AuthenticatedUser) -> Optional[uuid.UUID]:
    """Resolves the caller's own org_id, for routes/sockets that must scope
    CLIENT_ADMIN/CLIENT_VIEWER callers to their own tenant (GGH-301/302).
    Returns None for staff roles (SUPER_ADMIN/PROJECT_MANAGER/LEAD_ENGINEER),
    who aren't tenant-scoped and may filter by an explicit org_id instead.
    Also applies the RLS session context (see _apply_rls_context) as a side
    effect, since every caller of this function has a live `db` session for
    the rest of the request.
    """
    if user.role not in ("CLIENT_ADMIN", "CLIENT_VIEWER"):
        _apply_rls_context(db, None)
        return None

    db_user: Optional[User] = None
    if user.user_id:
        db_user = db.get(User, uuid.UUID(user.user_id))
    elif user.email:
        db_user = db.query(User).filter(User.email == user.email).first()

    if not db_user:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No organization membership found for this account")

    _apply_rls_context(db, db_user.org_id)
    return db_user.org_id


def get_current_user_org_id(
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> Optional[uuid.UUID]:
    """Depends()-compatible wrapper around resolve_org_id for HTTP routes."""
    return resolve_org_id(db, user)


def require_roles(*allowed_roles: str):
    """Dependency factory for RBAC-gated routes, e.g.
    `Depends(require_roles("SUPER_ADMIN", "PROJECT_MANAGER"))`. Every caller
    of this is staff-only by construction, so it always sets the RLS bypass
    context (see _apply_rls_context) — staff routes that touch RLS-protected
    tables (projects, milestones, invoices, project_updates) would otherwise
    see zero rows regardless of their app-level role check."""

    async def checker(
        user: AuthenticatedUser = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> AuthenticatedUser:
        if user.role not in allowed_roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires one of roles: {allowed_roles}")
        _apply_rls_context(db, None)
        return user

    return checker
