import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


class InstantPaymentOut(BaseModel):
    id: uuid.UUID
    customer_email: str
    amount: float
    description: Optional[str]
    status: str
    checkout_url: Optional[str]
    created_by: Optional[str]
    created_at: datetime
    paid_at: Optional[datetime]

    class Config:
        from_attributes = True


class InstantPaymentCreate(BaseModel):
    customer_email: EmailStr
    # Stripe's ceiling for a single USD charge is $999,999.99.
    amount: float = Field(..., ge=0.5, le=999_999.99)
    description: Optional[str] = Field(default=None, max_length=500)


class InstantPaymentSent(BaseModel):
    payment: InstantPaymentOut
    checkout_url: str
    email_sent: bool
