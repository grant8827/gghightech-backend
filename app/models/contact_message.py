"""Messages from the public contact form (POST /messages —
app/api/routes/messages.py).

Sent by anonymous visitors from the Contact page or the floating message
button, and read by staff in the admin Messages tab. Never shown publicly.

Deliberately not RLS-protected, same reasoning as reviews and estimates:
a message belongs to the site, not to a tenant.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

# NEW: not opened by staff yet (counted in the sidebar badge).
# READ: opened, still in the inbox.
# ARCHIVED: dealt with; kept for reference.
MESSAGE_STATUSES = ("NEW", "READ", "ARCHIVED")

# Where on the site the message was sent from, for staff context only.
MESSAGE_SOURCES = ("CONTACT_PAGE", "WIDGET")


class ContactMessage(Base):
    __tablename__ = "contact_messages"
    __table_args__ = (
        CheckConstraint(f"status IN {MESSAGE_STATUSES}", name="ck_contact_messages_status_valid"),
        CheckConstraint(f"source IN {MESSAGE_SOURCES}", name="ck_contact_messages_source_valid"),
        Index("ix_contact_messages_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    subject: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="CONTACT_PAGE")

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="NEW", server_default="NEW")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Last staff status change, if any.
    handled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    handled_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
