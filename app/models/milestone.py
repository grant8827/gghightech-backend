from typing import Optional

import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base

MILESTONE_STATUSES = ("PLANNED", "IN_PROGRESS", "COMPLETED")


class Milestone(Base):
    __tablename__ = "milestones"
    __table_args__ = (
        CheckConstraint(f"status IN {MILESTONE_STATUSES}", name="ck_milestones_status_valid"),
        CheckConstraint("progress_percentage BETWEEN 0 AND 100", name="ck_milestones_progress_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"))

    # Denormalized from the parent project at creation — lets the Postgres
    # RLS policy (see the "tenant_isolation" migration) filter this table
    # directly on org_id instead of a subquery through projects, and matches
    # the same pattern audit_logs already uses.
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"))

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    progress_percentage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="PLANNED")
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    # Billing amount for this milestone — required before it can be
    # client-approved (see POST /milestones/{id}/approve). Null until staff
    # price it; not implied by budget_estimate, which is a whole-project
    # ballpark, not a per-milestone breakdown.
    amount: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project: Mapped["Project"] = relationship(back_populates="milestones")
