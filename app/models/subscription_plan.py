"""Recurring billing plans used to create Stripe-hosted Checkout links, or
billed manually invoice-by-invoice. Three real distinctions matter here,
not variations of one thing:

- One-time charges (ad-hoc project work) never create a SubscriptionPlan
  at all — see POST /invoices/{id}/send-payment-link instead.
- Annual plans (domain renewal, Apple Developer Program fee, etc.) are
  always a real recurring Stripe subscription (is_subscription=True) —
  annual costs here are inherently recurring, so there's nothing to ask.
- Monthly plans are the one case that's genuinely ambiguous: the client
  may want to auto-renew (is_subscription=True, a real Stripe
  subscription) or just be billed month-to-month with no standing
  commitment (is_subscription=False — the original manual
  generate-invoice flow, unchanged).

Deliberately not RLS-protected, same reasoning as JiraTicket: staff-only."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

SUBSCRIPTION_STATUSES = ("ACTIVE", "PAUSED", "CANCELED")
BILLING_FREQUENCIES = ("MONTHLY", "ANNUAL")


class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"
    __table_args__ = (
        CheckConstraint(f"status IN {SUBSCRIPTION_STATUSES}", name="ck_subscription_plans_status_valid"),
        CheckConstraint(f"billing_frequency IN {BILLING_FREQUENCIES}", name="ck_subscription_plans_frequency_valid"),
        # NULL passes a Postgres CHECK (comparisons against NULL are
        # UNKNOWN, not FALSE) — billing_day is only meaningful for a
        # manual monthly plan's bookkeeping, so it's nullable for
        # subscriptions/annual plans without weakening this range check.
        CheckConstraint("billing_day BETWEEN 1 AND 28", name="ck_subscription_plans_billing_day_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"))
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    billing_frequency: Mapped[str] = mapped_column(String(20), nullable=False, default="MONTHLY")
    # Real auto-renewing Stripe subscription vs. billed manually as needed —
    # see the class docstring. Always True when billing_frequency is ANNUAL.
    is_subscription: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    billing_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")

    # Who Stripe Checkout links for this plan get emailed to — stored once
    # so later actions (checkout link, generate-invoice) don't need it
    # re-entered. See app/services/email.py's send_payment_link_email.
    customer_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_invoiced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
