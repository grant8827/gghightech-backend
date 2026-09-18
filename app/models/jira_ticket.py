"""A local record of Jira issues created *through this tool*
(POST /projects/{id}/jira-tickets — app/services/jira_service.py's
create_issue) — not a live mirror of the Jira project's full backlog, same
"first 100 issues, no pagination" honesty about not being a full Jira
client that fetch_issue_progress already carries. Only written on a
successful create; a failed attempt (Jira unreachable, not configured)
leaves no row.

Deliberately not RLS-protected: staff-only, never read by the client
portal. Same category as estimates/users/organizations today — a
deliberate scope decision, not an oversight, since RLS is otherwise a
load-bearing security control in this app (see the "tenant_isolation"
migration)."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class JiraTicket(Base):
    __tablename__ = "jira_tickets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"))
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"))

    jira_issue_key: Mapped[str] = mapped_column(String(50), nullable=False)
    jira_url: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    issue_type: Mapped[str] = mapped_column(String(50), nullable=False, default="Task")
    created_by_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
