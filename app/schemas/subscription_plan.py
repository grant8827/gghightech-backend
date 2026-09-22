import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.models.subscription_plan import BILLING_FREQUENCIES


class SubscriptionPlanCreate(BaseModel):
    org_id: uuid.UUID
    project_id: Optional[uuid.UUID] = None
    name: str = Field(..., min_length=1, max_length=255)
    amount: float = Field(..., gt=0)
    billing_frequency: str = Field(default="MONTHLY", description=f"One of: {', '.join(BILLING_FREQUENCIES)}")
    is_subscription: bool = False
    billing_day: Optional[int] = Field(default=None, ge=1, le=28)
    customer_email: Optional[EmailStr] = None

    @model_validator(mode="after")
    def validate_frequency(self) -> "SubscriptionPlanCreate":
        if self.billing_frequency not in BILLING_FREQUENCIES:
            raise ValueError(f"billing_frequency must be one of {BILLING_FREQUENCIES}")
        # Annual pass-through costs (domain, App Store fees, etc.) are
        # inherently recurring — there's nothing to ask, so a request that
        # disagrees is a client bug, not a legitimate one-off annual charge
        # (which belongs on a plain Invoice instead).
        if self.billing_frequency == "ANNUAL" and not self.is_subscription:
            raise ValueError("Annual plans are always a subscription (is_subscription must be true)")
        return self


class SubscriptionPlanUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    amount: Optional[float] = Field(default=None, gt=0)
    billing_day: Optional[int] = Field(default=None, ge=1, le=28)
    customer_email: Optional[EmailStr] = None
    status: Optional[str] = None


class SubscriptionPlanOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    project_id: Optional[uuid.UUID]
    name: str
    amount: float
    billing_frequency: str
    is_subscription: bool
    billing_day: Optional[int]
    customer_email: Optional[str]
    status: str
    created_at: datetime
    last_invoiced_at: Optional[datetime]

    class Config:
        from_attributes = True
