from typing import Optional

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field


class MilestoneCreate(BaseModel):
    project_id: uuid.UUID
    title: str
    due_date: Optional[date] = None


class MilestoneUpdate(BaseModel):
    progress_percentage: Optional[int] = Field(default=None, ge=0, le=100)
    status: Optional[str] = None
    due_date: Optional[date] = None


class MilestoneOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    progress_percentage: int
    status: str
    due_date: Optional[date]
    created_at: datetime

    class Config:
        from_attributes = True
