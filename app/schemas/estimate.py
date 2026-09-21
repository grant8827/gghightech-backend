from typing import Optional

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.services.pricing import DESIGN_TIER_PRICE_MULTIPLIER, FEATURE_DELTAS, PROJECT_TYPES


REQUEST_TYPES = ("NEW", "UPDATE", "MAINTENANCE")


class EstimateOptions(BaseModel):
    project_type: str = Field(..., description=f"One of: {', '.join(PROJECT_TYPES)}")
    features: list[str] = Field(default_factory=list, description=f"Any of: {', '.join(FEATURE_DELTAS)}")
    design_tier: str = Field(default="STANDARD", description=f"One of: {', '.join(DESIGN_TIER_PRICE_MULTIPLIER)}")
    request_type: str = Field(default="NEW", description=f"One of: {', '.join(REQUEST_TYPES)}")
    client_email: Optional[EmailStr] = None
    client_phone: Optional[str] = Field(default=None, max_length=30)

    @field_validator("request_type")
    @classmethod
    def request_type_must_be_valid(cls, value: str) -> str:
        if value not in REQUEST_TYPES:
            raise ValueError(f"request_type must be one of {REQUEST_TYPES}")
        return value

    @field_validator("client_phone")
    @classmethod
    def phone_looks_like_a_phone_number(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            return None
        if not all(c.isdigit() or c in "+-. ()" for c in stripped):
            raise ValueError("Phone number contains unexpected characters")
        if sum(c.isdigit() for c in stripped) < 7:
            raise ValueError("Phone number looks too short")
        return stripped


class EstimateCreate(EstimateOptions):
    project_description: str = Field(
        ...,
        min_length=40,
        max_length=4000,
        description="Required project brief used for AI-assisted scope and complexity analysis.",
    )

    @field_validator("project_description")
    @classmethod
    def description_must_be_meaningful(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped.split()) < 8:
            raise ValueError("Please describe the users, main workflow, and desired outcome in at least 8 words")
        return stripped


class InfrastructureCostOut(BaseModel):
    name: str
    monthly_min: float
    monthly_max: float
    annual_min: float
    annual_max: float
    note: str


class EstimatePreview(BaseModel):
    request_type: str = "NEW"
    calculated_min_price: float
    calculated_max_price: float
    estimated_weeks_min: int
    estimated_weeks_max: int
    infrastructure: list[InfrastructureCostOut]
    monthly_operating_min: float
    monthly_operating_max: float
    first_year_operating_min: float
    first_year_operating_max: float
    # Only populated when request_type == MAINTENANCE — calculated_*/
    # estimated_weeks_* are 0 in that case, since a retainer isn't a
    # one-time build price with a build timeline. See
    # app/services/pricing.py's MAINTENANCE_PLANS.
    maintenance_monthly_min: float = 0
    maintenance_monthly_max: float = 0
    maintenance_hours_per_week_min: int = 0
    maintenance_hours_per_week_max: int = 0


class EstimateOut(BaseModel):
    id: uuid.UUID
    client_email: Optional[str]
    client_phone: Optional[str]
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
