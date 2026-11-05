"""Email Server admin API: a SUPER_ADMIN-only pass-through to the Java
management service (backend-java), which manages the Apache James mail
server for every sending domain.

Why a pass-through instead of the browser calling the Java service: it stays
on the private network, never sees user JWTs, and this app's one login,
revocation and audit trail cover it. The Java service trusts the
X-Service-Token shared with this app and records X-Actor-Email.

Every path is listed explicitly (ids typed as UUIDs), so nothing else on the
Java service is reachable from here. Off entirely while EMAIL_SERVICE_URL is
blank: /status says so and the admin tab stays hidden.

Responses can contain a newly created SMTP password (shown once). Nothing
here logs response bodies.
"""

import logging
import uuid
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.services.audit import record_audit_event
from app.services.auth import AuthenticatedUser, require_roles

logger = logging.getLogger("gghightech.email_server")

router = APIRouter(prefix="/api/v1/email-server", tags=["email-server"])

_admin = require_roles("SUPER_ADMIN")
# DNS verification does live lookups; give it room.
_TIMEOUT_SECONDS = 30.0


def _enabled() -> bool:
    return bool(settings.EMAIL_SERVICE_URL and settings.EMAIL_SERVICE_TOKEN)


def _forward(method: str, path: str, user: AuthenticatedUser, body: Any = None, params: Optional[dict] = None):
    """Returns (status, parsed JSON or None). Raises HTTPException with the
    Java service's own message for 4xx/5xx, and 502/503 when it can't be
    used at all."""
    if not _enabled():
        raise HTTPException(503, "The Email Server isn't set up")
    try:
        response = httpx.request(
            method,
            f"{settings.EMAIL_SERVICE_URL.rstrip('/')}/api/v1/email{path}",
            headers={"X-Service-Token": settings.EMAIL_SERVICE_TOKEN, "X-Actor-Email": user.email or ""},
            json=body,
            params={k: v for k, v in (params or {}).items() if v is not None},
            timeout=_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.error("Email Server unreachable: %s", type(exc).__name__)
        raise HTTPException(502, "Could not reach the Email Server") from exc

    payload = None
    if response.content:
        try:
            payload = response.json()
        except ValueError:
            payload = None
    if response.status_code >= 400:
        # The Java service sends RFC 9457 problem details: the reason is "detail".
        message = payload.get("detail") if isinstance(payload, dict) else None
        if response.status_code == 401:
            # Our token was refused: a configuration problem, not the admin's.
            logger.error("Email Server rejected EMAIL_SERVICE_TOKEN")
            raise HTTPException(502, "The Email Server rejected this app's credentials")
        raise HTTPException(response.status_code, message or "The Email Server refused the request")
    return response.status_code, payload


def _respond(status: int, payload: Any) -> Response:
    if payload is None:
        return Response(status_code=status, headers={"Cache-Control": "no-store"})
    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})


def _audit(db: Session, user: AuthenticatedUser, action: str, resource_type: str, resource_id, metadata: dict) -> None:
    record_audit_event(
        db, user=user, action=action, resource_type=resource_type, resource_id=str(resource_id), metadata=metadata
    )
    db.commit()


@router.get("/status")
def status(user: AuthenticatedUser = Depends(_admin)) -> dict:
    return {"enabled": _enabled()}


@router.get("/overview")
def overview(user: AuthenticatedUser = Depends(_admin)) -> Response:
    return _respond(*_forward("GET", "/overview", user))


@router.get("/domains")
def list_domains(user: AuthenticatedUser = Depends(_admin)) -> Response:
    return _respond(*_forward("GET", "/domains", user))


@router.post("/domains")
def create_domain(
    name: str = Body(..., embed=True, max_length=253),
    user: AuthenticatedUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> Response:
    status_code, domain = _forward("POST", "/domains", user, body={"name": name})
    _audit(db, user, "email_server.domain_add", "email_domain", domain["id"], {"name": domain["name"]})
    return _respond(status_code, domain)


@router.get("/domains/{domain_id}")
def get_domain(domain_id: uuid.UUID, user: AuthenticatedUser = Depends(_admin)) -> Response:
    return _respond(*_forward("GET", f"/domains/{domain_id}", user))


@router.post("/domains/{domain_id}/verify")
def verify_domain(domain_id: uuid.UUID, user: AuthenticatedUser = Depends(_admin)) -> Response:
    return _respond(*_forward("POST", f"/domains/{domain_id}/verify", user))


@router.delete("/domains/{domain_id}")
def delete_domain(
    domain_id: uuid.UUID, user: AuthenticatedUser = Depends(_admin), db: Session = Depends(get_db)
) -> Response:
    status_code, _ = _forward("DELETE", f"/domains/{domain_id}", user)
    _audit(db, user, "email_server.domain_delete", "email_domain", domain_id, {})
    return _respond(status_code, None)


@router.get("/domains/{domain_id}/accounts")
def list_accounts(domain_id: uuid.UUID, user: AuthenticatedUser = Depends(_admin)) -> Response:
    return _respond(*_forward("GET", f"/domains/{domain_id}/accounts", user))


@router.post("/domains/{domain_id}/accounts")
def create_account(
    domain_id: uuid.UUID,
    local_part: str = Body(..., embed=True, max_length=64),
    description: Optional[str] = Body(default=None, embed=True, max_length=255),
    user: AuthenticatedUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> Response:
    status_code, credential = _forward(
        "POST", f"/domains/{domain_id}/accounts", user, body={"localPart": local_part, "description": description}
    )
    # The address only, never the password.
    _audit(
        db, user, "email_server.smtp_account_create", "smtp_account", credential["account"]["id"],
        {"address": credential["account"]["address"]},
    )
    return _respond(status_code, credential)


@router.post("/accounts/{account_id}/revoke")
def revoke_account(
    account_id: uuid.UUID, user: AuthenticatedUser = Depends(_admin), db: Session = Depends(get_db)
) -> Response:
    status_code, account = _forward("POST", f"/accounts/{account_id}/revoke", user)
    _audit(db, user, "email_server.smtp_account_revoke", "smtp_account", account_id, {"address": account["address"]})
    return _respond(status_code, account)


@router.get("/messages")
def messages(
    domain: Optional[str] = None, limit: int = 50, user: AuthenticatedUser = Depends(_admin)
) -> Response:
    return _respond(*_forward("GET", "/messages", user, params={"domain": domain, "limit": max(1, min(limit, 200))}))
