"""Recurring billing plans (Payments & Subscriptions admin tab). This is
deliberately not real recurring billing — there's no scheduler and no
Stripe account with recurring Prices behind it. It's a record of "what
this client owes monthly and on what day," plus a manual
POST /subscriptions/{id}/generate-invoice that creates one real Invoice
on demand — the same honest stopping point as the existing Stripe-stubbed
Pay button (app/services/stripe_service.py).

Deliberately not RLS-protected, same reasoning as JiraTicket: staff-only,
never read by the client portal in this scope."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

SUBSCRIPTION_STATUSES = ("ACTIVE", "PAUSED", "CANCELED")


class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"
    __table_args__ = (
        CheckConstraint(f"status IN {SUBSCRIPTION_STATUSES}", name="ck_subscription_plans_status_valid"),
        CheckConstraint("billing_day BETWEEN 1 AND 28", name="ck_subscription_plans_billing_day_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"))
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    billing_day: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_invoiced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
