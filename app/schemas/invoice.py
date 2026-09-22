import uuid
from datetime import datetime
from typing import Optional

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
