"""Contact messages: a public submit side and a staff inbox.

Anyone can send a message from the Contact page or the floating message
button. It lands in the admin Messages tab as NEW, and the team is emailed
a copy at LEAD_NOTIFICATION_EMAIL (best effort: if email isn't configured,
the message is still saved). Protected by per-IP and per-email limits and
a hidden spam-trap field, like reviews.
"""

import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.contact_message import MESSAGE_STATUSES, ContactMessage
from app.schemas.contact_message import (
    ContactMessageAdmin,
    ContactMessageCreate,
    ContactMessageSubmitted,
    ContactMessageUpdate,
    UnreadCount,
)
from app.services.audit import record_audit_event
from app.services.auth import AuthenticatedUser, require_roles
from app.services.email import send_contact_notification
from app.services.rate_limit import Rule, client_ip, limiter, rate_limit

logger = logging.getLogger("gghightech.messages")

router = APIRouter(prefix="/api/v1/messages", tags=["messages"])

_STAFF = ("SUPER_ADMIN", "PROJECT_MANAGER")

# Stops one sender flooding the inbox by rotating IP addresses.
_PER_EMAIL_RULES: list[Rule] = [(5, 24 * 60 * 60)]


def _submit_rules() -> list[Rule]:
    return [
        (settings.CONTACT_SUBMIT_LIMIT_PER_HOUR, 60 * 60),
        (settings.CONTACT_SUBMIT_LIMIT_PER_DAY, 24 * 60 * 60),
    ]


@router.post(
    "",
    response_model=ContactMessageSubmitted,
    status_code=201,
    dependencies=[Depends(rate_limit("contact-submit", _submit_rules))],
)
def send_message(
    payload: ContactMessageCreate, request: Request, db: Session = Depends(get_db)
) -> ContactMessageSubmitted:
    if payload.website:
        # A bot filled in the hidden field. Answer exactly as if it had
        # worked, so it has no reason to try a different approach.
        logger.warning("Contact spam trap triggered from %s", client_ip(request))
        return ContactMessageSubmitted()

    retry_after = limiter.hit(f"contact-email:{payload.email.lower()}", _PER_EMAIL_RULES)
    if retry_after > 0:
        raise HTTPException(
            429,
            "We've received several messages from you today. We'll be in touch soon!",
            headers={"Retry-After": str(math.ceil(retry_after))},
        )

    msg = ContactMessage(
        name=payload.name,
        email=payload.email.lower(),
        phone=payload.phone,
        subject=payload.subject,
        message=payload.message,
        source=payload.source,
        status="NEW",
    )
    db.add(msg)
    db.flush()  # assigns msg.id for the audit row
    record_audit_event(
        db,
        user=None,  # public endpoint, no signed-in caller
        action="message.create",
        resource_type="contact_message",
        resource_id=msg.id,
        metadata={"source": payload.source},
    )
    db.commit()

    send_contact_notification(
        message_id=str(msg.id),
        name=msg.name,
        email=msg.email,
        phone=msg.phone,
        subject=msg.subject,
        message=msg.message,
    )
    return ContactMessageSubmitted()


# ---- Staff inbox ----


@router.get("/admin", response_model=list[ContactMessageAdmin])
def list_messages(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF)),
) -> list[ContactMessage]:
    if status is not None and status not in MESSAGE_STATUSES:
        raise HTTPException(422, f"status must be one of {MESSAGE_STATUSES}")
    query = db.query(ContactMessage)
    if status:
        query = query.filter(ContactMessage.status == status)
    return query.order_by(ContactMessage.created_at.desc()).limit(500).all()


@router.get("/admin/unread-count", response_model=UnreadCount)
def unread_count(
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF)),
) -> UnreadCount:
    return UnreadCount(unread=db.query(ContactMessage).filter(ContactMessage.status == "NEW").count())


@router.patch("/{message_id}", response_model=ContactMessageAdmin)
def update_message(
    message_id: uuid.UUID,
    payload: ContactMessageUpdate,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF)),
) -> ContactMessage:
    msg = db.get(ContactMessage, message_id)
    if not msg:
        raise HTTPException(404, "Message not found")
    previous = msg.status
    msg.status = payload.status
    msg.handled_at = datetime.now(timezone.utc)
    msg.handled_by = user.email
    record_audit_event(
        db,
        user=user,
        action="message.update",
        resource_type="contact_message",
        resource_id=msg.id,
        metadata={"from": previous, "to": payload.status},
    )
    db.commit()
    db.refresh(msg)
    return msg


@router.delete("/{message_id}", status_code=204)
def delete_message(
    message_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("SUPER_ADMIN")),
) -> None:
    """Permanent. Archiving (PATCH status=ARCHIVED) is the reversible option."""
    msg = db.get(ContactMessage, message_id)
    if not msg:
        raise HTTPException(404, "Message not found")
    record_audit_event(
        db,
        user=user,
        action="message.delete",
        resource_type="contact_message",
        resource_id=msg.id,
        metadata={"status": msg.status, "source": msg.source},
    )
    db.delete(msg)
    db.commit()
