"""GGH-602 — immutable audit trail for mutating actions. Written via
app/services/audit.record_audit_event; there is deliberately no update/delete
route for this table."""

from typing import Optional

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # No FK constraint: staff/cross-org actions may have no org_id, and we
    # never want a later org deletion to cascade-delete its audit trail.
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    # The authenticated caller's user id. Null for genuinely unauthenticated
    # mutations (e.g. the public lead-capture estimate endpoint), where
    # actor_role is "ANONYMOUS".
    actor_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    actor_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    actor_role: Mapped[str] = mapped_column(String(50), nullable=False)

    action: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g. "project.create"
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g. "project"
    resource_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Named event_metadata (not `metadata`) since that's a reserved
    # attribute name on SQLAlchemy's declarative Base.
    event_metadata: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
