"""Created either by POST /milestones/{id}/approve (milestone_id set) or
directly by staff via POST /invoices (milestone_id null, description set —
for retainers, one-off charges, anything not tied to a milestone reaching
100%). Stripe Checkout handles online card payments; PATCH
/invoices/{id}/mark-paid covers checks and wires made outside Stripe."""

from typing import Optional

import uuid
from datetime import datetime

from datetime import date

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Numeric, String, Text, event, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base
from app.models.numbering import next_document_number

INVOICE_STATUSES = ("PENDING", "PAID")
# How a PAID invoice was paid. CARD is set by the Stripe webhook; the rest
# are recorded by staff via PATCH /invoices/{id}/mark-paid. Shown on the
# client's receipt (GET /invoices/{id}/receipt).
PAYMENT_METHODS = ("CARD", "CHECK", "WIRE", "CASH", "OTHER")


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint(f"status IN {INVOICE_STATUSES}", name="ck_invoices_status_valid"),
        CheckConstraint(
            f"payment_method IS NULL OR payment_method IN {PAYMENT_METHODS}",
            name="ck_invoices_payment_method_valid",
        ),
    )

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
    # Who POST /invoices/{id}/send-payment-link emailed the Stripe Checkout
    # link to — audit trail, and lets a resend reuse it without re-asking.
    customer_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Null until paid; see PAYMENT_METHODS above.
    payment_method: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # Check number, wire reference, or Stripe PaymentIntent id.
    payment_reference: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    # ---- Professional invoice fields (Invoicing / Contracts tab) ----
    # INV-YYYY-NNNN, assigned on insert (see _assign_number below).
    invoice_number: Mapped[Optional[str]] = mapped_column(String(20), unique=True, nullable=True)
    # [{description, quantity, unit_price}] — null for older single-amount
    # invoices (milestone approvals, subscriptions), whose PDF shows one line.
    line_items: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    subtotal: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    tax_rate: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)  # percent
    tax_amount: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    issue_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bill_to_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # When staff last emailed it to the client (POST /invoices/{id}/send).
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


@event.listens_for(Invoice, "before_insert")
def _assign_number(mapper, connection, target: Invoice) -> None:
    if target.issue_date is None:
        target.issue_date = datetime.now().date()
    if not target.invoice_number:
        target.invoice_number = next_document_number(
            connection, Invoice.__table__, Invoice.__table__.c.invoice_number, "invoice_number_seq", "INV"
        )
