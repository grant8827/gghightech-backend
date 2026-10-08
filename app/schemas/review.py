import re
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.review import REVIEW_STATUSES

# Unverified public text goes straight onto the site, so links are refused
# outright — they are what review spam exists to plant.
# Matched on "http(s)://" and "www." only: a wider net (bare ".net", ".io")
# would reject honest reviews that mention ASP.NET or Socket.io.
_LINK_RE = re.compile(r"(https?://|www\.)", re.IGNORECASE)


def _clean(value: str) -> str:
    """Trim, collapse runs of whitespace, and drop control characters."""
    return " ".join("".join(ch for ch in value if ch.isprintable() or ch.isspace()).split())


class ReviewCreate(BaseModel):
    name: str = Field(..., max_length=80)
    email: EmailStr
    rating: int = Field(..., ge=1, le=5)
    message: str = Field(..., max_length=1500)
    # Spam trap: an input real visitors never see (hidden in the form), so
    # anything in it means a bot filled the form in. Named to look tempting.
    website: Optional[str] = Field(default=None, max_length=200)

    @field_validator("name")
    @classmethod
    def name_is_a_name(cls, value: str) -> str:
        value = _clean(value)
        if len(value) < 2:
            raise ValueError("Please enter your name")
        if _LINK_RE.search(value):
            raise ValueError("Links aren't allowed in the name")
        return value

    @field_validator("message")
    @classmethod
    def message_is_a_review(cls, value: str) -> str:
        # Keep the reviewer's own line breaks; tidy everything else.
        value = "\n".join(line for line in (_clean(line) for line in value.splitlines()) if line)
        if len(value) < 10:
            raise ValueError("Please write at least a short sentence")
        if _LINK_RE.search(value):
            raise ValueError("Links aren't allowed in reviews")
        return value


class ReviewSubmitted(BaseModel):
    """What the submitter gets back. `status` tells the form whether to say
    "your review is live" or "it will appear once approved"."""

    status: str


class ReviewPublic(BaseModel):
    """Everything a public endpoint may reveal about a review. No email."""

    id: uuid.UUID
    name: str
    rating: int
    message: str
    is_featured: bool
    created_at: datetime

    class Config:
        from_attributes = True


class ReviewSummary(BaseModel):
    """Computed from PUBLISHED reviews only."""

    average: Optional[float] = Field(..., description="Mean rating to one decimal place; null when there are none")
    count: int
    breakdown: dict[int, int] = Field(..., description="How many reviews gave each star rating, 5 down to 1")


class ReviewAdmin(ReviewPublic):
    email: str
    status: str
    moderated_at: Optional[datetime]
    moderated_by: Optional[str]


class ReviewModerate(BaseModel):
    """Staff-only PATCH /reviews/{id} — only the fields sent get changed."""

    status: Optional[str] = None
    is_featured: Optional[bool] = None

    @field_validator("status")
    @classmethod
    def status_must_be_valid(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in REVIEW_STATUSES:
            raise ValueError(f"status must be one of {REVIEW_STATUSES}")
        return value
