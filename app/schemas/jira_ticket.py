import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class JiraTicketCreate(BaseModel):
    summary: str = Field(..., min_length=1, max_length=255)
    description: str = Field(..., min_length=1)
    issue_type: str = Field(default="Task", max_length=50)


class JiraTicketOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    org_id: uuid.UUID
    jira_issue_key: str
    jira_url: str
    summary: str
    description: Optional[str]
    issue_type: str
    created_by_email: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True
