import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field


class InvoiceOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    project_id: uuid.UUID
    milestone_id: Optional[uuid.UUID]
    description: Optional[str]
    amount: float
    status: str
    customer_email: Optional[str]
    created_at: datetime
    paid_at: Optional[datetime]
    payment_method: Optional[str] = None
    payment_reference: Optional[str] = None

    class Config:
        from_attributes = True


class InvoiceCreate(BaseModel):
    """Staff-only direct creation (POST /invoices) — not tied to a
    milestone, for retainers/one-off charges. Milestone-driven invoices
    are created only via POST /milestones/{id}/approve, not this."""

    project_id: uuid.UUID
    amount: float = Field(..., gt=0)
    description: str = Field(..., min_length=1, max_length=500)
    customer_email: Optional[EmailStr] = None


class InvoiceMarkPaid(BaseModel):
    """Optional body for PATCH /invoices/{id}/mark-paid — how the client
    paid outside Stripe. Shown on the client's receipt. CARD isn't offered:
    only the Stripe webhook records card payments."""

    payment_method: Literal["CHECK", "WIRE", "CASH", "OTHER"] = "OTHER"
    payment_reference: Optional[str] = Field(None, max_length=120)
