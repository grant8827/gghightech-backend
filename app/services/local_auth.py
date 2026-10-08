"""
Self-issued email/password login — the site's only auth system.

Genuine auth path: bcrypt hashing, server-signed JWTs with an expiry. Three
token kinds share this signing key but are never interchangeable:
  - estimate PDF tokens (create_estimate_pdf_token/decode_estimate_pdf_token):
    a 60-minute, single-estimate download pass for the anonymous visitor
    who just submitted that estimate (see app/api/routes/estimates.py).
    Tagged with "purpose": "estimate_pdf".
  - access tokens (create_access_token/decode_access_token): a signed-in
    session, carries role/email for app/services/auth.py to trust.
  - invite tokens (create_invite_token/decode_invite_token): a one-time,
    longer-lived link emailed to a newly-invited user (see
    app/api/routes/users.py's invite_user) so they can set their own
    password at POST /auth/accept-invite. Tagged with "purpose": "invite"
    so one can never be used in place of the other.

The first (and so far only) staff account this issues tokens for is created
via `python -m app.cli create-superadmin` — see that file.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.organization import Organization
from app.models.user import User

ALGORITHM = "HS256"
INVITE_TOKEN_EXPIRE_DAYS = 7

ESTIMATE_PDF_PURPOSE = "estimate_pdf"
ESTIMATE_PDF_TOKEN_EXPIRE_MINUTES = 60

INTERNAL_ORG_NAME = "GG HighTech (Internal)"
INTERNAL_ORG_DOMAIN = "gghightech.internal"
INTERNAL_PLAN_TIER = "INTERNAL"


def is_internal_org(org: Organization) -> bool:
    """GG HighTech's own organization (created by create_or_update_superadmin
    below), as opposed to a client's. Staff accounts live here and client
    accounts never do."""
    return org.domain == INTERNAL_ORG_DOMAIN or org.plan_tier == INTERNAL_PLAN_TIER


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed hash (e.g. a user row with no real bcrypt hash set) —
        # never let this raise into a 500, just treat as a failed login.
        return False


def create_access_token(user_id: str, email: str, role: str, token_version: int = 0) -> str:
    """`email` and `role` are informational only — app/services/auth.py
    re-reads both from the users table on every request and never trusts
    these copies. `ver` is the user's token_version at issue time; the
    token dies the moment that number moves on."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": user_id, "email": email, "role": role, "ver": token_version, "exp": expire}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def create_access_token_for(user: User) -> str:
    return create_access_token(str(user.id), user.email, user.role, user.token_version)


def revoke_sessions(user: User) -> None:
    """Invalidates every access token issued to this user so far, on every
    device. Only stages the change — the caller commits, same convention
    as app/services/audit.record_audit_event."""
    user.token_version = (user.token_version or 0) + 1


def decode_access_token(token: str) -> dict[str, Any]:
    """Raises jwt.ExpiredSignatureError or jwt.PyJWTError on failure —
    callers (app/services/auth.py) decide what that means for the request."""
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])


def create_invite_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=INVITE_TOKEN_EXPIRE_DAYS)
    payload = {"sub": user_id, "purpose": "invite", "exp": expire}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_invite_token(token: str) -> dict[str, Any]:
    """Raises jwt.ExpiredSignatureError or jwt.PyJWTError (including our own
    InvalidTokenError below) on failure — see decode_access_token."""
    claims = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    if claims.get("purpose") != "invite":
        raise jwt.InvalidTokenError("Not an invite token")
    return claims


def create_estimate_pdf_token(estimate_id: str) -> str:
    """Handed to the anonymous visitor who just submitted an estimate
    (POST /estimates), so they — and only they — can download that one
    estimate's PDF for a short while. Tagged with its own "purpose" so it
    can never stand in for a session or an invite."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=ESTIMATE_PDF_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": estimate_id, "purpose": ESTIMATE_PDF_PURPOSE, "exp": expire}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_estimate_pdf_token(token: str) -> dict[str, Any]:
    """Raises jwt.PyJWTError on any failure, including a validly signed
    token of a different kind — see decode_invite_token."""
    claims = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM], options={"require": ["exp", "sub"]})
    if claims.get("purpose") != ESTIMATE_PDF_PURPOSE:
        raise jwt.InvalidTokenError("Not an estimate PDF token")
    return claims


def create_or_update_superadmin(db: Session, email: str, password: str, full_name: str) -> User:
    """Shared by `python -m app.cli create-superadmin` and the one-time
    BOOTSTRAP_TOKEN-gated endpoint (app/api/routes/auth.py) — both are ways
    to create the very first login before there's an admin session to
    invite anyone through the ordinary API."""
    org = db.query(Organization).filter(Organization.domain == INTERNAL_ORG_DOMAIN).first()
    if not org:
        org = Organization(name=INTERNAL_ORG_NAME, domain=INTERNAL_ORG_DOMAIN, plan_tier=INTERNAL_PLAN_TIER)
        db.add(org)
        db.flush()

    user = db.query(User).filter(User.email == email).first()
    if user:
        # A password reset: anyone signed in with the old one is signed out.
        revoke_sessions(user)
        user.password_hash = hash_password(password)
        user.role = "SUPER_ADMIN"
        user.full_name = full_name
    else:
        user = User(
            org_id=org.id,
            email=email,
            password_hash=hash_password(password),
            full_name=full_name,
            role="SUPER_ADMIN",
        )
        db.add(user)

    db.commit()
    db.refresh(user)
    return user
