"""Client reviews: a public submit/read side and a staff moderation side.

Reviews are accepted from anyone with no email verification (a product
decision), and by default go live immediately. What protects the site is
(a) the submit limits and spam checks below, and (b) staff moderation —
every review can be hidden, re-published, featured, or deleted from the
admin Reviews tab. Set REVIEWS_AUTO_PUBLISH=false to hold new reviews for
approval instead.
"""

import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.review import REVIEW_STATUSES, Review
from app.schemas.review import (
    ReviewAdmin,
    ReviewCreate,
    ReviewModerate,
    ReviewPublic,
    ReviewSubmitted,
    ReviewSummary,
)
from app.services.audit import record_audit_event
from app.services.auth import AuthenticatedUser, require_roles
from app.services.rate_limit import Rule, client_ip, limiter, rate_limit

logger = logging.getLogger("gghightech.reviews")

router = APIRouter(prefix="/api/v1/reviews", tags=["reviews"])

_MODERATORS = ("SUPER_ADMIN", "PROJECT_MANAGER")

# One person leaving several reviews is the cheapest way to skew the
# average, so on top of the per-IP limit each email address gets a small
# daily allowance of its own.
_PER_EMAIL_RULES: list[Rule] = [(2, 24 * 60 * 60)]


def _submit_rules() -> list[Rule]:
    return [
        (settings.REVIEW_SUBMIT_LIMIT_PER_HOUR, 60 * 60),
        (settings.REVIEW_SUBMIT_LIMIT_PER_DAY, 24 * 60 * 60),
    ]


def _initial_status() -> str:
    return "PUBLISHED" if settings.REVIEWS_AUTO_PUBLISH else "PENDING"


@router.post(
    "",
    response_model=ReviewSubmitted,
    status_code=201,
    dependencies=[Depends(rate_limit("review-submit", _submit_rules))],
)
def submit_review(payload: ReviewCreate, request: Request, db: Session = Depends(get_db)) -> ReviewSubmitted:
    status = _initial_status()

    if payload.website:
        # A bot filled in the hidden field. Answer exactly as if it had
        # worked, so it has no reason to try a different approach.
        logger.warning("Review spam trap triggered from %s", client_ip(request))
        return ReviewSubmitted(status=status)

    retry_after = limiter.hit(f"review-email:{payload.email.lower()}", _PER_EMAIL_RULES)
    if retry_after > 0:
        raise HTTPException(
            429,
            "You've already left a review recently. Thank you!",
            headers={"Retry-After": str(math.ceil(retry_after))},
        )

    review = Review(
        name=payload.name,
        email=payload.email.lower(),
        rating=payload.rating,
        message=payload.message,
        status=status,
    )
    db.add(review)
    db.flush()  # assigns review.id so the audit row below can reference it
    record_audit_event(
        db,
        user=None,  # public endpoint, no signed-in caller
        action="review.create",
        resource_type="review",
        resource_id=review.id,
        metadata={"rating": payload.rating, "status": status},
    )
    db.commit()
    return ReviewSubmitted(status=status)


def _published(db: Session):
    return db.query(Review).filter(Review.status == "PUBLISHED")


@router.get("", response_model=list[ReviewPublic])
def list_reviews(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[Review]:
    """Published reviews for the public Reviews page — featured first,
    then newest."""
    return (
        _published(db)
        .order_by(Review.is_featured.desc(), Review.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.get("/highlights", response_model=list[ReviewPublic])
def list_highlights(limit: int = Query(default=3, ge=1, le=12), db: Session = Depends(get_db)) -> list[Review]:
    """A handful of reviews for the home and portfolio pages: the ones
    staff featured, or — until anything has been featured — the newest
    4- and 5-star ones."""
    featured = (
        _published(db).filter(Review.is_featured.is_(True)).order_by(Review.created_at.desc()).limit(limit).all()
    )
    if featured:
        return featured
    return _published(db).filter(Review.rating >= 4).order_by(Review.created_at.desc()).limit(limit).all()


@router.get("/summary", response_model=ReviewSummary)
def review_summary(db: Session = Depends(get_db)) -> ReviewSummary:
    rows = (
        db.query(Review.rating, func.count(Review.id))
        .filter(Review.status == "PUBLISHED")
        .group_by(Review.rating)
        .all()
    )
    breakdown = {stars: 0 for stars in (5, 4, 3, 2, 1)}
    for rating, count in rows:
        breakdown[rating] = count
    total = sum(breakdown.values())
    average = round(sum(stars * count for stars, count in breakdown.items()) / total, 1) if total else None
    return ReviewSummary(average=average, count=total, breakdown=breakdown)


# ---- Staff moderation ----


@router.get("/admin", response_model=list[ReviewAdmin])
def list_all_reviews(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_MODERATORS)),
) -> list[Review]:
    """Every review in every status, with the reviewer's email."""
    if status is not None and status not in REVIEW_STATUSES:
        raise HTTPException(422, f"status must be one of {REVIEW_STATUSES}")
    query = db.query(Review)
    if status:
        query = query.filter(Review.status == status)
    return query.order_by(Review.created_at.desc()).all()


@router.patch("/{review_id}", response_model=ReviewAdmin)
def moderate_review(
    review_id: uuid.UUID,
    payload: ReviewModerate,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_MODERATORS)),
) -> Review:
    review = db.get(Review, review_id)
    if not review:
        raise HTTPException(404, "Review not found")

    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    new_status = changes.get("status", review.status)
    if changes.get("is_featured") and new_status != "PUBLISHED":
        raise HTTPException(422, "Only a published review can be featured")

    review.status = new_status
    if "is_featured" in changes:
        review.is_featured = changes["is_featured"]
    if new_status != "PUBLISHED":
        # Taking a review down also takes it off the home page.
        review.is_featured = False
    review.moderated_at = datetime.now(timezone.utc)
    review.moderated_by = user.email

    record_audit_event(
        db,
        user=user,
        action="review.moderate",
        resource_type="review",
        resource_id=review.id,
        metadata=changes,
    )
    db.commit()
    db.refresh(review)
    return review


@router.delete("/{review_id}", status_code=204)
def delete_review(
    review_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("SUPER_ADMIN")),
) -> None:
    """Permanent. Hiding (PATCH status=HIDDEN) is the reversible option."""
    review = db.get(Review, review_id)
    if not review:
        raise HTTPException(404, "Review not found")
    record_audit_event(
        db,
        user=user,
        action="review.delete",
        resource_type="review",
        resource_id=review.id,
        metadata={"name": review.name, "rating": review.rating, "status": review.status},
    )
    db.delete(review)
    db.commit()
