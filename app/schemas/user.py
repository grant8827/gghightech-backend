from typing import Optional

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, field_validator

from app.models.user import USER_ROLES


class UserInvite(BaseModel):
    org_id: uuid.UUID
    email: EmailStr
    full_name: str
    role: str = "CLIENT_VIEWER"

    @field_validator("role")
    @classmethod
    def role_must_be_valid(cls, value: str) -> str:
        if value not in USER_ROLES:
            raise ValueError(f"role must be one of {USER_ROLES}")
        return value


class UserOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    email: str
    full_name: str
    role: str
    avatar_url: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True
