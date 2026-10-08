"""Public client reviews (POST /reviews — app/api/routes/reviews.py).

Submitted by anonymous visitors with no email verification, by design, so
what keeps this usable is moderation after the fact: staff can hide,
re-publish, feature, or delete any review from the admin Reviews tab.
`email` is collected so staff can follow up; it is never returned by a
public endpoint.

Deliberately not RLS-protected, same reasoning as estimates: reviews
belong to the site, not to a tenant.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

# PENDING: waiting for staff approval (only used when REVIEWS_AUTO_PUBLISH
#   is off — see app/core/config.py).
# PUBLISHED: visible on the public site and counted in the average rating.
# HIDDEN: taken down by staff; kept, but not shown or counted.
REVIEW_STATUSES = ("PENDING", "PUBLISHED", "HIDDEN")


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint(f"status IN {REVIEW_STATUSES}", name="ck_reviews_status_valid"),
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_reviews_rating_range"),
        # Only something the public can see can be featured.
        CheckConstraint("NOT is_featured OR status = 'PUBLISHED'", name="ck_reviews_featured_is_published"),
        Index("ix_reviews_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PUBLISHED")
    # Staff-picked highlights for the home and portfolio pages.
    is_featured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Last staff action on this review, if any.
    moderated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    moderated_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
