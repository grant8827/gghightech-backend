import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ProjectUpdateCreate(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


class ProjectUpdateOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    author_name: str
    author_role: str
    message: str
    created_at: datetime

    class Config:
        from_attributes = True
