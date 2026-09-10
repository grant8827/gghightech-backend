from typing import Optional

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.project import PROJECT_STATUSES


class ProjectCreate(BaseModel):
    org_id: uuid.UUID
    title: str
    slug: str
    status: str = "DISCOVERY"
    budget_estimate: Optional[float] = None
    repository_url: Optional[str] = None


class ProjectOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    title: str
    slug: str
    status: str = Field(..., description=f"One of: {', '.join(PROJECT_STATUSES)}")
    budget_estimate: Optional[float]
    health_score: int
    staging_url: Optional[str]
    repository_url: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True
