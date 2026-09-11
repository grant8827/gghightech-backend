"""
Self-issued email/password login for staff.

This exists so a real admin login works today, without waiting on a Clerk
account. It's a genuine auth path (bcrypt hashing, a server-signed JWT with
an expiry) — not a placeholder — and it keeps working as a fallback even
after Clerk is configured, since app/services/auth.py checks this first.

The first (and so far only) account this issues tokens for is created via
`python -m app.cli create-superadmin` — see that file.
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

INTERNAL_ORG_NAME = "GG HighTech (Internal)"
INTERNAL_ORG_DOMAIN = "gghightech.internal"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed hash (e.g. a user row with no real bcrypt hash set) —
        # never let this raise into a 500, just treat as a failed login.
        return False


def create_access_token(user_id: str, email: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": user_id, "email": email, "role": role, "exp": expire}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Raises jwt.ExpiredSignatureError or jwt.PyJWTError on failure —
    callers (app/services/auth.py) decide what that means for the request."""
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])


def create_or_update_superadmin(db: Session, email: str, password: str, full_name: str) -> User:
    """Shared by `python -m app.cli create-superadmin` and the one-time
    BOOTSTRAP_TOKEN-gated endpoint (app/api/routes/auth.py) — both are ways
    to create the very first login before there's an admin session to
    invite anyone through the ordinary API."""
    org = db.query(Organization).filter(Organization.domain == INTERNAL_ORG_DOMAIN).first()
    if not org:
        org = Organization(name=INTERNAL_ORG_NAME, domain=INTERNAL_ORG_DOMAIN, plan_tier="INTERNAL")
        db.add(org)
        db.flush()

    user = db.query(User).filter(User.email == email).first()
    if user:
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
