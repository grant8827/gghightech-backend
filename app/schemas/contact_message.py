import re
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.contact_message import MESSAGE_SOURCES, MESSAGE_STATUSES

# Digits plus the usual separators; checked loosely, since numbers are
# written many ways (+1 (876) 555-0100, 876.555.0100 ...).
_PHONE_RE = re.compile(r"^\+?[0-9 ()./-]{7,40}$")


def _clean(value: str) -> str:
    """Trim, collapse runs of whitespace, and drop control characters."""
    return " ".join("".join(ch for ch in value if ch.isprintable() or ch.isspace()).split())


class ContactMessageCreate(BaseModel):
    name: str = Field(..., max_length=80)
    email: EmailStr
    phone: Optional[str] = Field(default=None, max_length=40)
    subject: Optional[str] = Field(default=None, max_length=150)
    message: str = Field(..., max_length=5000)
    source: str = "CONTACT_PAGE"
    # Spam trap: an input real visitors never see (hidden in the form), so
    # anything in it means a bot filled the form in.
    website: Optional[str] = Field(default=None, max_length=200)

    @field_validator("name")
    @classmethod
    def name_is_a_name(cls, value: str) -> str:
        value = _clean(value)
        if len(value) < 2:
            raise ValueError("Please enter your name")
        return value

    @field_validator("phone", "subject")
    @classmethod
    def optional_text(cls, value: Optional[str]) -> Optional[str]:
        value = _clean(value or "")
        return value or None

    @field_validator("phone")
    @classmethod
    def phone_looks_like_a_number(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _PHONE_RE.match(value):
            raise ValueError("Please enter a valid phone number, or leave it blank")
        return value

    @field_validator("message")
    @classmethod
    def message_has_content(cls, value: str) -> str:
        # Keep the sender's own line breaks; tidy everything else.
        value = "\n".join(line for line in (_clean(line) for line in value.splitlines()) if line)
        if len(value) < 10:
            raise ValueError("Please write a little more so we can help")
        return value

    @field_validator("source")
    @classmethod
    def source_is_known(cls, value: str) -> str:
        # Unknown values are recorded as the default rather than refused:
        # it's context for staff, not something a visitor should fail on.
        return value if value in MESSAGE_SOURCES else "CONTACT_PAGE"


class ContactMessageSubmitted(BaseModel):
    ok: bool = True


class ContactMessageAdmin(BaseModel):
    id: uuid.UUID
    name: str
    email: str
    phone: Optional[str]
    subject: Optional[str]
    message: str
    source: str
    status: str
    created_at: datetime
    handled_at: Optional[datetime]
    handled_by: Optional[str]

    class Config:
        from_attributes = True


class ContactMessageUpdate(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def status_must_be_valid(cls, value: str) -> str:
        if value not in MESSAGE_STATUSES:
            raise ValueError(f"status must be one of {MESSAGE_STATUSES}")
        return value


class UnreadCount(BaseModel):
    unread: int
