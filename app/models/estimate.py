from typing import Optional

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base

# DRAFT: computed and shown to the visitor, not yet submitted as a lead.
# SUBMITTED: visitor supplied contact info / requested a PDF.
# CONVERTED: became a real Project (project_id gets set at that point).
ESTIMATE_STATUSES = ("DRAFT", "SUBMITTED", "CONVERTED")


class Estimate(Base):
    __tablename__ = "estimates"
    __table_args__ = (CheckConstraint(f"status IN {ESTIMATE_STATUSES}", name="ck_estimates_status_valid"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    client_phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Fixes the ERD/schema mismatch in the original spec: the diagram drew
    # Estimates as a child of Projects, but an estimate is captured *before*
    # a project (or org) exists. Both are nullable and only get set once a
    # lead converts — see PATCH /estimates/{id}/convert.
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )

    scope_configuration: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Free-text "tell us exactly what you want" field. The toggle-based
    # scope_configuration drives the calculated price; this is read by a
    # human on the GG HighTech side (surfaced in the admin Leads list, the
    # PDF, and the lead-notification email) to sanity-check that price
    # against what the client actually described before following up.
    project_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    calculated_min_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    calculated_max_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    estimated_weeks_min: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_weeks_max: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="DRAFT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    organization: Mapped[Optional["Organization"]] = relationship()
    project: Mapped[Optional["Project"]] = relationship()
