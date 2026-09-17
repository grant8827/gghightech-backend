import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class InvoiceOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    project_id: uuid.UUID
    milestone_id: uuid.UUID
    amount: float
    status: str
    created_at: datetime
    paid_at: Optional[datetime]

    class Config:
        from_attributes = True
