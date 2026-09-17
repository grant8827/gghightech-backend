from typing import Optional

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.project import DEPLOYMENT_STATUSES, PROJECT_STATUSES


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
    last_deploy_commit_sha: Optional[str]
    last_deploy_status: Optional[str]
    last_deployed_at: Optional[datetime]
    latest_commit_sha: Optional[str]
    latest_commit_message: Optional[str]
    latest_commit_synced_at: Optional[datetime]
    jira_project_key: Optional[str]
    jira_issue_count: Optional[int]
    jira_done_count: Optional[int]
    jira_synced_at: Optional[datetime]
    overall_progress: int = Field(..., description="Average milestone progress_percentage (GGH-301)")
    created_at: datetime

    class Config:
        from_attributes = True


class ProjectUpdate(BaseModel):
    """Staff-only PATCH /projects/{id} — everything optional, only the
    fields sent get changed. Closes the long-standing "no way to set
    staging_url" gap and gives Jira sync somewhere to point at."""

    title: Optional[str] = None
    status: Optional[str] = None
    budget_estimate: Optional[float] = None
    staging_url: Optional[str] = None
    repository_url: Optional[str] = None
    jira_project_key: Optional[str] = None


class ProjectDeploymentUpdate(BaseModel):
    """GGH-302 — recorded manually via PATCH until a CI webhook exists."""

    commit_sha: str = Field(..., min_length=7, max_length=40)
    status: str = Field(..., description=f"One of: {', '.join(DEPLOYMENT_STATUSES)}")
