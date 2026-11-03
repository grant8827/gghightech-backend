from typing import Optional

import logging
import math
import uuid

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import (
    AcceptInviteRequest,
    ForgotPasswordRequest,
    LoginRequest,
    LoginResponse,
    ResetPasswordRequest,
)
from app.services.audit import record_audit_event
from app.services.auth import AuthenticatedUser, get_current_user
from app.services.email import send_password_reset_email
from app.services.rate_limit import Rule, client_ip, limiter, rate_limit
from app.services.local_auth import (
    create_access_token_for,
    create_or_update_superadmin,
    create_password_reset_token,
    decode_invite_token,
    decode_password_reset_token,
    hash_password,
    password_reset_token_matches,
    revoke_sessions,
    verify_password,
)

logger = logging.getLogger("gghightech.auth")

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# Fixed windows for the two endpoints below that can't be brute-forced in
# practice (a signed invite JWT, a long random bootstrap token) but still
# shouldn't accept unlimited guesses.
_ACCEPT_INVITE_RULES: list[Rule] = [(10, 10 * 60)]
_BOOTSTRAP_RULES: list[Rule] = [(5, 60 * 60)]
# forgot-password sends email, so it's limited per IP and, separately, per
# address — the second stops one inbox being flooded from many IPs.
_FORGOT_PASSWORD_IP_RULES: list[Rule] = [(5, 15 * 60)]
_FORGOT_PASSWORD_EMAIL_RULES: list[Rule] = [(3, 60 * 60)]
_RESET_PASSWORD_RULES: list[Rule] = [(10, 10 * 60)]


def _login_ip_rules() -> list[Rule]:
    return [(settings.LOGIN_ATTEMPTS_PER_IP_PER_5_MIN, 5 * 60)]


def _too_many(retry_after: float, message: str) -> HTTPException:
    return HTTPException(429, message, headers={"Retry-After": str(math.ceil(retry_after))})


@router.post("/login", response_model=LoginResponse, dependencies=[Depends(rate_limit("login", _login_ip_rules))])
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> LoginResponse:
    """Brute-force protection, three layers (limits in app/core/config.py):

    1. Attempts per IP address — the rate_limit dependency above.
    2. Wrong passwords per (email, IP address): a short lockout aimed at
       one client guessing one account.
    3. Wrong passwords per email from any address: a higher threshold, for
       guessing spread across many addresses.

    While locked out, even the correct password is refused — otherwise the
    lock wouldn't stop a guess that happens to be right. Failures are
    counted the same way whether or not the email belongs to a real
    account, so a lockout never reveals which emails exist."""
    if not settings.SECRET_KEY:
        raise HTTPException(503, "Local login is not configured on this server")

    ip = client_ip(request)
    email_key = payload.email.strip().lower()
    window = settings.LOGIN_LOCKOUT_MINUTES * 60
    # (key, rules) for layers 2 and 3. A threshold of 0 disables that layer.
    lockouts: list[tuple[str, list[Rule]]] = [
        (f"login-fail:{email_key}|{ip}", [(settings.LOGIN_FAILURES_BEFORE_LOCKOUT, window)]),
        (f"login-fail:{email_key}", [(settings.LOGIN_FAILURES_BEFORE_ACCOUNT_LOCKOUT, window)]),
    ]
    lockouts = [(key, rules) for key, rules in lockouts if rules[0][0] > 0]

    def locked_for() -> float:
        return max((limiter.peek(key, rules) for key, rules in lockouts), default=0.0)

    retry_after = locked_for()
    if retry_after > 0:
        raise _too_many(retry_after, "Too many failed sign-in attempts. Please wait and try again.")

    user = db.query(User).filter(User.email == payload.email).first()
    # Deliberately identical error for "no such user" and "wrong password" —
    # doesn't tell an attacker which part was wrong.
    if not user or not user.password_hash or not verify_password(payload.password, user.password_hash):
        for key, _ in lockouts:
            limiter.record(key, window)
        now_locked = locked_for() > 0
        logger.warning("Failed login for %s from %s%s", email_key, ip, " (now locked out)" if now_locked else "")
        # Written for unknown emails too (resource_id stays null), so the
        # trail shows guessing against accounts that don't exist. Never
        # includes the password that was tried.
        record_audit_event(
            db,
            user=None,
            action="auth.login_locked" if now_locked else "auth.login_failed",
            resource_type="user",
            resource_id=user.id if user else None,
            org_id=user.org_id if user else None,
            metadata={"email": email_key, "ip": ip},
        )
        db.commit()
        raise HTTPException(401, "Incorrect email or password")

    # A real sign-in clears this client's own strikes. The any-address
    # count is left to age out, so one good login can't be used to reset
    # an attack coming from elsewhere.
    limiter.forget(f"login-fail:{email_key}|{ip}")

    token = create_access_token_for(user)
    return LoginResponse(access_token=token, role=user.role, email=user.email, full_name=user.full_name)


@router.post(
    "/accept-invite",
    response_model=LoginResponse,
    dependencies=[Depends(rate_limit("accept-invite", lambda: _ACCEPT_INVITE_RULES))],
)
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

    token = create_access_token_for(user)
    return LoginResponse(access_token=token, role=user.role, email=user.email, full_name=user.full_name)


@router.post(
    "/forgot-password",
    status_code=202,
    dependencies=[Depends(rate_limit("forgot-password", lambda: _FORGOT_PASSWORD_IP_RULES))],
)
def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)) -> dict:
    """Emails a reset link if the address belongs to an account. The
    response is the same either way, so this can't be used to find out
    which emails have accounts. Also rescues an invited user whose invite
    expired before they set a password."""
    message = {"detail": "If that email has an account, a reset link is on its way."}
    if not settings.SECRET_KEY:
        raise HTTPException(503, "Local login is not configured on this server")

    email_key = payload.email.strip().lower()
    if limiter.hit(f"forgot-password:{email_key}", _FORGOT_PASSWORD_EMAIL_RULES) > 0:
        return message

    user = db.query(User).filter(User.email == payload.email).first()
    if not user:
        return message

    reset_link = f"{settings.FRONTEND_URL.rstrip('/')}/reset-password?token={create_password_reset_token(user)}"
    send_password_reset_email(user.email, user.full_name, reset_link)
    record_audit_event(
        db,
        user=None,
        action="auth.password_reset_requested",
        resource_type="user",
        resource_id=user.id,
        org_id=user.org_id,
        metadata={},
    )
    db.commit()
    return message


@router.post(
    "/reset-password",
    response_model=LoginResponse,
    dependencies=[Depends(rate_limit("reset-password", lambda: _RESET_PASSWORD_RULES))],
)
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)) -> LoginResponse:
    """Second half of forgot-password. Sets the new password, signs the
    user out everywhere else, and signs them in here (same response as
    /login)."""
    if not settings.SECRET_KEY:
        raise HTTPException(503, "Local login is not configured on this server")

    invalid = HTTPException(401, "This reset link is invalid or has expired — request a new one")
    try:
        claims = decode_password_reset_token(payload.token)
        user_id = uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError):
        raise invalid
    user = db.get(User, user_id)
    if not user or not password_reset_token_matches(claims, user):
        raise invalid
    if len(payload.password) < 12:
        raise HTTPException(422, "Password must be at least 12 characters")

    user.password_hash = hash_password(payload.password)
    revoke_sessions(user)
    # A successful reset proves control of the inbox; don't leave the
    # account locked out by failures that came before it.
    email_key = user.email.strip().lower()
    limiter.forget(f"login-fail:{email_key}")
    record_audit_event(
        db,
        user=AuthenticatedUser(user_id=str(user.id), email=user.email, role=user.role),
        action="auth.password_reset",
        resource_type="user",
        resource_id=user.id,
        org_id=user.org_id,
        metadata={},
    )
    db.commit()

    token = create_access_token_for(user)
    return LoginResponse(access_token=token, role=user.role, email=user.email, full_name=user.full_name)


@router.get("/me")
def me(user: AuthenticatedUser = Depends(get_current_user)) -> dict:
    """Lets the frontend verify a stored token is still valid and see who
    it belongs to, without needing to decode the JWT client-side."""
    return {"email": user.email, "role": user.role}


@router.post("/logout", status_code=204)
def logout(user: AuthenticatedUser = Depends(get_current_user), db: Session = Depends(get_db)) -> None:
    """Server-side sign-out. There is no per-device session record, so
    this ends every session the user has, on every device — the token
    used to call it included."""
    if not user.user_id:
        return  # dev-header caller (development only): nothing to revoke
    db_user = db.get(User, uuid.UUID(user.user_id))
    if not db_user:
        return
    revoke_sessions(db_user)
    record_audit_event(
        db,
        user=user,
        action="auth.logout",
        resource_type="user",
        resource_id=db_user.id,
        org_id=db_user.org_id,
        metadata={},
    )
    db.commit()


@router.post("/bootstrap-superadmin", status_code=201)
def bootstrap_superadmin(
    payload: LoginRequest,
    request: Request,
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
    if not settings.BOOTSTRAP_TOKEN:
        raise HTTPException(404)
    # Only once the endpoint is actually switched on: limits guesses at the
    # token. Checked after the line above so a disabled endpoint stays
    # indistinguishable from one that doesn't exist.
    retry_after = limiter.hit(f"bootstrap:{client_ip(request)}", _BOOTSTRAP_RULES)
    if retry_after > 0:
        raise _too_many(retry_after, "Too many requests. Please wait and try again.")
    if x_bootstrap_token != settings.BOOTSTRAP_TOKEN or not full_name:
        raise HTTPException(404)
    if len(payload.password) < 12:
        raise HTTPException(422, "Password must be at least 12 characters")

    user = create_or_update_superadmin(db, payload.email, payload.password, full_name)
    return {"email": user.email, "role": user.role, "id": str(user.id)}
