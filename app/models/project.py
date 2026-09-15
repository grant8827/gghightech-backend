from typing import Optional

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base

PROJECT_STATUSES = ("DISCOVERY", "IN_PROGRESS", "QA", "DELIVERED")

# GGH-302 — no CI webhook exists yet, so this is set manually via
# PATCH /projects/{id}/deployment (same "stub until real CI exists" pattern
# as _send_clerk_invite_stub in users.py) until a GitHub Actions job calls it.
DEPLOYMENT_STATUSES = ("PENDING", "SUCCESS", "FAILED")


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(f"status IN {PROJECT_STATUSES}", name="ck_projects_status_valid"),
        CheckConstraint(
            f"last_deploy_status IS NULL OR last_deploy_status IN {DEPLOYMENT_STATUSES}",
            name="ck_projects_last_deploy_status_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="DISCOVERY")
    budget_estimate: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    health_score: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    staging_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    repository_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_deploy_commit_sha: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    last_deploy_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    last_deployed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    organization: Mapped["Organization"] = relationship(back_populates="projects")
    milestones: Mapped[list["Milestone"]] = relationship(back_populates="project", cascade="all, delete-orphan")

    @property
    def overall_progress(self) -> int:
        """GGH-301 — average of this project's milestones' progress_percentage.
        A proxy for "dynamically calculated progress": there's no Jira (or
        other task-tracker) integration to derive real task-level completion
        from, so milestone progress is the finest-grained real signal we have.
        """
        if not self.milestones:
            return 0
        return round(sum(m.progress_percentage for m in self.milestones) / len(self.milestones))
