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

from app.core.config import settings

ALGORITHM = "HS256"


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
