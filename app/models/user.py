from typing import Optional

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base

# Kept in sync with the RBAC roles in the architecture spec. Enforced both
# here (CHECK constraint) and in the Pydantic schema, so a bad role can
# neither be written directly via SQL nor slip in through the API.
USER_ROLES = ("SUPER_ADMIN", "PROJECT_MANAGER", "LEAD_ENGINEER", "CLIENT_ADMIN", "CLIENT_VIEWER")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(f"role IN {USER_ROLES}", name="ck_users_role_valid"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)

    # Nullable: SSO-only users (via Clerk) never get a local password hash.
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Set once a user has signed in through Clerk — links our row to theirs.
    clerk_user_id: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)

    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="CLIENT_VIEWER")
    avatar_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    organization: Mapped["Organization"] = relationship(back_populates="users")
