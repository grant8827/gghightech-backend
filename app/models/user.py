from typing import Optional

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base

# Kept in sync with the RBAC roles in the architecture spec. Enforced both
# here (CHECK constraint) and in the Pydantic schema, so a bad role can
# neither be written directly via SQL nor slip in through the API.
USER_ROLES = ("SUPER_ADMIN", "PROJECT_MANAGER", "LEAD_ENGINEER", "CLIENT_ADMIN", "CLIENT_VIEWER")

# The two kinds of account. Staff roles see every client's data and are not
# tenant-scoped (see app/services/auth.py's resolve_org_id), so who may
# hand one out is restricted — see invite_user in app/api/routes/users.py.
STAFF_USER_ROLES = ("SUPER_ADMIN", "PROJECT_MANAGER", "LEAD_ENGINEER")
CLIENT_USER_ROLES = ("CLIENT_ADMIN", "CLIENT_VIEWER")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(f"role IN {USER_ROLES}", name="ck_users_role_valid"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)

    # Nullable: null means "invited but hasn't been through
    # POST /auth/accept-invite yet" — see app/api/routes/users.py.
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Session revocation: every access token carries the value this had
    # when it was issued (see app/services/local_auth.py), and
    # app/services/auth.py rejects any token whose value no longer
    # matches. Incrementing it signs the user out everywhere at once —
    # see revoke_sessions() in app/services/local_auth.py.
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="CLIENT_VIEWER")
    avatar_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    organization: Mapped["Organization"] = relationship(back_populates="users")
