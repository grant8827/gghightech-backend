from typing import Optional

import uuid
from datetime import datetime

from pydantic import BaseModel


class OrganizationCreate(BaseModel):
    name: str
    domain: Optional[str] = None
    plan_tier: str = "STANDARD"


class OrganizationOut(BaseModel):
    id: uuid.UUID
    name: str
    domain: Optional[str]
    plan_tier: str
    created_at: datetime

    class Config:
        from_attributes = True
