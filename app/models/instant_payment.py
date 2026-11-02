"""Instant payments (app/api/routes/instant_payments.py) — staff type a
customer email and an amount, and Stripe Checkout's link is emailed to
them. No organization, project or milestone: for quick charges to anyone,
including people who aren't clients in the portal.

Deliberately not RLS-protected: there's no tenant to scope it to, and only
staff can read or create these.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

INSTANT_PAYMENT_STATUSES = ("PENDING", "PAID")


class InstantPayment(Base):
    __tablename__ = "instant_payments"
    __table_args__ = (
        CheckConstraint(f"status IN {INSTANT_PAYMENT_STATUSES}", name="ck_instant_payments_status_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_email: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    # The most recent Checkout link sent — kept so staff can copy it again.
    checkout_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
