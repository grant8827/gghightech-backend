"""Created either by POST /milestones/{id}/approve (milestone_id set) or
directly by staff via POST /invoices (milestone_id null, description set —
for retainers, one-off charges, anything not tied to a milestone reaching
100%). Stripe stays stubbed (app/services/stripe_service.py) until a real
account exists; PATCH /invoices/{id}/mark-paid covers real-world payments
(check, wire) made outside Stripe in the meantime."""

from typing import Optional

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

INVOICE_STATUSES = ("PENDING", "PAID")


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (CheckConstraint(f"status IN {INVOICE_STATUSES}", name="ck_invoices_status_valid"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"))
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"))
    milestone_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("milestones.id", ondelete="CASCADE"), nullable=True
    )
    # What this invoice is for — required for ad-hoc invoices (no milestone
    # title to borrow); optional for milestone-driven ones, which already
    # have that context via milestone_id.
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
