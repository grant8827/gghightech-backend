import uuid
from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


class PaymentStep(BaseModel):
    label: str = Field(..., min_length=1, max_length=120)
    amount: float = Field(..., ge=0, le=100_000_000)
    # Free text: "On signing", "Week 3", "2026-12-01".
    due: str = Field("", max_length=80)


def _clean_deliverables(items: Optional[list[str]]) -> Optional[list[str]]:
    if items is None:
        return None
    cleaned = [i.strip() for i in items if i and i.strip()]
    if any(len(i) > 300 for i in cleaned):
        raise ValueError("Each deliverable must be 300 characters or fewer")
    return cleaned


class _ContractFields(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    client_name: str = Field(..., min_length=2, max_length=255)
    client_email: Optional[EmailStr] = None
    effective_date: Optional[date] = None
    scope: str = Field(..., min_length=10, max_length=20_000)
    deliverables: list[str] = Field(default_factory=list, max_length=50)
    timeline: Optional[str] = Field(None, max_length=5_000)
    payment_schedule: list[PaymentStep] = Field(default_factory=list, max_length=24)
    # Used only when there's no payment schedule; otherwise the schedule's
    # sum is the contract value.
    total_value: float = Field(0, ge=0, le=100_000_000)
    payment_terms: Optional[str] = Field(None, max_length=5_000)
    terms: str = Field(..., min_length=20, max_length=50_000)

    @field_validator("deliverables")
    @classmethod
    def _deliverables(cls, items: list[str]) -> list[str]:
        return _clean_deliverables(items)


class ContractCreate(_ContractFields):
    project_id: uuid.UUID


class ContractUpdate(BaseModel):
    """Every field optional; only what's sent changes."""

    title: Optional[str] = Field(None, min_length=3, max_length=200)
    client_name: Optional[str] = Field(None, min_length=2, max_length=255)
    client_email: Optional[EmailStr] = None
    effective_date: Optional[date] = None
    scope: Optional[str] = Field(None, min_length=10, max_length=20_000)
    deliverables: Optional[list[str]] = Field(None, max_length=50)
    timeline: Optional[str] = Field(None, max_length=5_000)
    payment_schedule: Optional[list[PaymentStep]] = Field(None, max_length=24)
    total_value: Optional[float] = Field(None, ge=0, le=100_000_000)
    payment_terms: Optional[str] = Field(None, max_length=5_000)
    terms: Optional[str] = Field(None, min_length=20, max_length=50_000)

    @field_validator("deliverables")
    @classmethod
    def _deliverables(cls, items: Optional[list[str]]) -> Optional[list[str]]:
        return _clean_deliverables(items)


class ContractSign(BaseModel):
    signer_name: str = Field(..., min_length=2, max_length=255)
    agree: Literal[True] = Field(..., description="Must be true: the signer agrees to the contract's terms")


class ContractOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    project_id: uuid.UUID
    contract_number: Optional[str]
    title: str
    status: str
    client_name: str
    client_email: Optional[str]
    effective_date: Optional[date]
    scope: str
    deliverables: list[str]
    timeline: Optional[str]
    payment_schedule: list[PaymentStep]
    total_value: float
    payment_terms: Optional[str]
    terms: str
    created_by: Optional[str]
    created_at: datetime
    updated_at: Optional[datetime]
    sent_at: Optional[datetime]
    provider_signer_name: Optional[str]
    signed_at: Optional[datetime]
    signer_name: Optional[str]
    signer_email: Optional[str]
    signed_content_hash: Optional[str]
    voided_at: Optional[datetime]

    class Config:
        from_attributes = True


class ContractSent(BaseModel):
    contract: ContractOut
    email_sent: bool


class ContractDefaults(BaseModel):
    terms: str
    payment_terms: str
