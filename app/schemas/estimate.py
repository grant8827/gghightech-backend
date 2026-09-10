from typing import Optional

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.services.pricing import DESIGN_TIER_PRICE_MULTIPLIER, FEATURE_DELTAS, PROJECT_TYPES


class EstimateCreate(BaseModel):
    project_type: str = Field(..., description=f"One of: {', '.join(PROJECT_TYPES)}")
    features: list[str] = Field(default_factory=list, description=f"Any of: {', '.join(FEATURE_DELTAS)}")
    design_tier: str = Field(default="STANDARD", description=f"One of: {', '.join(DESIGN_TIER_PRICE_MULTIPLIER)}")
    client_email: Optional[EmailStr] = None
    project_description: Optional[str] = Field(
        default=None,
        max_length=4000,
        description="Free-text description of what the client wants — reviewed by staff "
        "against the toggle-based price, not used in the price calculation itself.",
    )


class EstimatePreview(BaseModel):
    calculated_min_price: float
    calculated_max_price: float
    estimated_weeks_min: int
    estimated_weeks_max: int


class EstimateOut(BaseModel):
    id: uuid.UUID
    client_email: Optional[str]
    scope_configuration: dict
    project_description: Optional[str]
    calculated_min_price: float
    calculated_max_price: float
    estimated_weeks_min: int
    estimated_weeks_max: int
    status: str
    created_at: datetime

    class Config:
        from_attributes = True
