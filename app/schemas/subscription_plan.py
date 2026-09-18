import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class SubscriptionPlanCreate(BaseModel):
    org_id: uuid.UUID
    project_id: Optional[uuid.UUID] = None
    name: str = Field(..., min_length=1, max_length=255)
    amount: float = Field(..., gt=0)
    billing_day: int = Field(..., ge=1, le=28)


class SubscriptionPlanUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    amount: Optional[float] = Field(default=None, gt=0)
    billing_day: Optional[int] = Field(default=None, ge=1, le=28)
    status: Optional[str] = None


class SubscriptionPlanOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    project_id: Optional[uuid.UUID]
    name: str
    amount: float
    billing_day: int
    status: str
    created_at: datetime
    last_invoiced_at: Optional[datetime]

    class Config:
        from_attributes = True
