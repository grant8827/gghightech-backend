"""Keeps each project's GitHub activity current for the client dashboard.

apply_commits() is shared by the staff "Sync from GitHub" button
(POST /projects/{id}/sync-github) and run_github_autosync(), a background
loop started with the app (app/main.py) that re-syncs every project with a
repository_url every GITHUB_SYNC_INTERVAL_MINUTES and pings open client
dashboards over the project WebSocket when something new landed.

The app runs as a single uvicorn process (see Procfile), so exactly one
loop runs. If that ever changes, the work stays correct (each pass is
idempotent), just repeated.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.project import Project
from app.services.github_service import GithubSyncError, fetch_repo_activity
from app.ws import manager

logger = logging.getLogger(__name__)

# Give the app a moment to finish starting before the first pass.
_FIRST_PASS_DELAY_SECONDS = 30


def apply_commits(project: Project, commits: list[dict], activity: Optional[dict] = None) -> bool:
    """Store a fetch_repo_activity() result on the project. Returns True
    when the newest commit changed (so clients are worth notifying)."""
    changed = project.latest_commit_sha != commits[0]["sha"]
    project.recent_commits = commits
    if activity is not None:
        project.commit_activity = activity
    project.latest_commit_sha = commits[0]["sha"]
    project.latest_commit_message = commits[0]["message"]
    project.latest_commit_synced_at = datetime.now(timezone.utc)
    return changed


def _bypass_rls(db: Session) -> None:
    # A system job with no signed-in user; the same trusted bypass the Stripe
    # webhook uses. SET LOCAL lasts for the current transaction only.
    db.execute(text("SET LOCAL app.bypass_rls = 'true'"))


def _repos_to_sync() -> list[tuple[uuid.UUID, str]]:
    db = SessionLocal()
    try:
        _bypass_rls(db)
        rows = db.query(Project.id, Project.repository_url).filter(Project.repository_url.isnot(None)).all()
        return [(row.id, row.repository_url) for row in rows]
    finally:
        db.close()


def _save(project_id: uuid.UUID, repository_url: str, commits: list[dict], activity: dict) -> bool:
    db = SessionLocal()
    try:
        _bypass_rls(db)
        project = db.get(Project, project_id)
        # Skip if the project was deleted or re-pointed at another repo
        # while GitHub was being asked.
        if not project or project.repository_url != repository_url:
            return False
        changed = apply_commits(project, commits, activity)
        db.commit()
        return changed
    finally:
        db.close()


async def sync_all_projects() -> int:
    """One pass over every project with a repo. Returns how many changed.
    One project's failure (private repo without a token, deleted repo)
    never stops the rest."""
    changed = 0
    for project_id, repository_url in await asyncio.to_thread(_repos_to_sync):
        try:
            commits, activity = await asyncio.to_thread(fetch_repo_activity, repository_url)
            if await asyncio.to_thread(_save, project_id, repository_url, commits, activity):
                changed += 1
                await manager.broadcast(project_id, {"type": "github_synced"})
        except GithubSyncError as exc:
            logger.warning("GitHub auto-sync skipped project %s: %s", project_id, exc)
        except Exception:  # noqa: BLE001 — a background loop must not die
            logger.exception("GitHub auto-sync failed for project %s", project_id)
    return changed


async def run_github_autosync() -> None:
    interval = settings.GITHUB_SYNC_INTERVAL_MINUTES * 60
    await asyncio.sleep(_FIRST_PASS_DELAY_SECONDS)
    while True:
        try:
            changed = await sync_all_projects()
            if changed:
                logger.info("GitHub auto-sync: %d project(s) had new commits", changed)
        except Exception:  # noqa: BLE001
            logger.exception("GitHub auto-sync pass failed")
        await asyncio.sleep(interval)
