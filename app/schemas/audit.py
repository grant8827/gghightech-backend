from typing import Optional

import uuid
from datetime import datetime

from pydantic import BaseModel


class AuditLogOut(BaseModel):
    id: uuid.UUID
    org_id: Optional[uuid.UUID]
    actor_id: Optional[str]
    actor_email: Optional[str]
    actor_role: str
    action: str
    resource_type: str
    resource_id: Optional[str]
    event_metadata: Optional[dict]
    created_at: datetime

    class Config:
        from_attributes = True
