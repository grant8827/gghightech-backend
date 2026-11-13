from typing import Optional

import re
import uuid
from datetime import datetime
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from app.models.project import DEPLOYMENT_STATUSES, PROJECT_STATUSES


_GITHUB_REPO_RE = re.compile(
    r"^(?:(?:https?://)?(?:www\.)?github\.com/|git@github\.com:)?"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$"
)


def normalize_repository_url(value: Optional[str]) -> Optional[str]:
    """Accepts the forms people paste — owner/repo, github.com/owner/repo,
    https://github.com/owner/repo(.git), git@github.com:owner/repo.git — and
    stores one canonical https URL. Blank clears it. Only GitHub is
    supported because that's all GitHub sync can read."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    match = _GITHUB_REPO_RE.match(value)
    if not match:
        raise ValueError("Enter a GitHub repository, e.g. https://github.com/owner/repo or owner/repo")
    return f"https://github.com/{match.group('owner')}/{match.group('repo')}"


def normalize_site_url(value: Optional[str]) -> Optional[str]:
    """A website address. A bare domain (acme.com) gets https:// added.
    Blank clears it."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if (
        parsed.scheme not in ("http", "https")
        or not host
        or any(c.isspace() for c in value)
        or ("." not in host and host != "localhost")
        or len(value) > 2048
    ):
        raise ValueError("Enter a website address, e.g. acme.com or https://staging.acme.com")
    return value.rstrip("/") if parsed.path in ("", "/") and not parsed.query else value


class CommitOut(BaseModel):
    sha: str
    message: str
    author: Optional[str] = None
    committed_at: Optional[str] = None
    url: Optional[str] = None


class ActivityWeek(BaseModel):
    start: str
    commits: int


class CommitActivity(BaseModel):
    weeks: list[ActivityWeek]
    capped: bool = False


class ProjectCreate(BaseModel):
    org_id: uuid.UUID
    title: str
    slug: str
    status: str = "DISCOVERY"
    budget_estimate: Optional[float] = None
    repository_url: Optional[str] = None
    live_url: Optional[str] = None
    staging_url: Optional[str] = None

    @field_validator("repository_url")
    @classmethod
    def _repo(cls, v: Optional[str]) -> Optional[str]:
        return normalize_repository_url(v)

    @field_validator("staging_url", "live_url")
    @classmethod
    def _site(cls, v: Optional[str]) -> Optional[str]:
        return normalize_site_url(v)


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
    live_url: Optional[str] = None
    recent_commits: Optional[list[CommitOut]] = None
    commit_activity: Optional[CommitActivity] = None
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
    live_url: Optional[str] = None
    repository_url: Optional[str] = None
    jira_project_key: Optional[str] = None

    @field_validator("staging_url", "live_url")
    @classmethod
    def _site(cls, v: Optional[str]) -> Optional[str]:
        return normalize_site_url(v)

    @field_validator("repository_url")
    @classmethod
    def _repo(cls, v: Optional[str]) -> Optional[str]:
        return normalize_repository_url(v)


class ProjectDeploymentUpdate(BaseModel):
    """GGH-302 — recorded manually via PATCH until a CI webhook exists."""

    commit_sha: str = Field(..., min_length=7, max_length=40)
    status: str = Field(..., description=f"One of: {', '.join(DEPLOYMENT_STATUSES)}")
