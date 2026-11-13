"""GitHub sync — outbound polling, triggered by staff hitting
POST /projects/{id}/sync-github (see app/api/routes/projects.py). Unlike
Jira, GitHub's read API works unauthenticated for public repos (rate-limited
by IP), so this is fully testable without any credentials at all; set
GITHUB_TOKEN for private repos or a higher rate limit.

Deliberately not a webhook receiver: GitHub can't deliver a webhook to
localhost, so polling is the only approach actually usable in dev — and it
needs no public URL ever, in prod either.
"""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

from app.core.config import settings

_GITHUB_URL_RE = re.compile(r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")


class GithubSyncError(Exception):
    """Message is safe to show the caller directly — never includes the token."""


def _parse_repo(repository_url: str) -> tuple[str, str]:
    match = _GITHUB_URL_RE.search(repository_url)
    if not match:
        raise GithubSyncError(f"{repository_url!r} doesn't look like a github.com repository URL")
    return match.group("owner"), match.group("repo")


def fetch_recent_commits(repository_url: str, limit: int = 5, since: Optional[datetime] = None) -> list[dict]:
    """Newest-first commits on the repo's default branch, as
    [{sha, message (first line), author, committed_at, url}]. Raises
    GithubSyncError (never a raw httpx/JSON exception) on any failure — bad
    URL, network error, or a non-200 from GitHub (404 repo not found or
    private without GITHUB_TOKEN, 403 rate-limited, etc.) — with GitHub's
    own error message included, not swallowed."""
    owner, repo = _parse_repo(repository_url)
    headers = {"Accept": "application/vnd.github+json"}
    if settings.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"

    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/commits",
            headers=headers,
            params={"per_page": limit, **({"since": since.strftime("%Y-%m-%dT%H:%M:%SZ")} if since else {})},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise GithubSyncError(f"Could not reach GitHub: {type(exc).__name__}") from exc

    if resp.status_code != 200:
        try:
            detail = resp.json().get("message", resp.text)
        except ValueError:
            detail = resp.text
        if resp.status_code == 404 and not settings.GITHUB_TOKEN:
            detail += " (if the repository is private, set GITHUB_TOKEN on the backend)"
        raise GithubSyncError(f"GitHub API returned {resp.status_code}: {detail}")

    try:
        commits = [
            {
                "sha": c["sha"],
                "message": (c["commit"]["message"].splitlines() or [""])[0][:300],
                "author": (c.get("author") or {}).get("login") or c["commit"]["author"].get("name"),
                "committed_at": c["commit"]["committer"]["date"],
                "url": c.get("html_url"),
            }
            for c in resp.json()
        ]
    except (ValueError, KeyError, TypeError) as exc:
        raise GithubSyncError("GitHub returned an unexpected response") from exc
    if not commits and since is None:
        raise GithubSyncError(f"{owner}/{repo} has no commits")
    return commits


def fetch_latest_commit(repository_url: str) -> dict:
    """Latest commit only — see fetch_recent_commits."""
    return fetch_recent_commits(repository_url, limit=1)[0]


ACTIVITY_WEEKS = 12
# One page of the commits API; a busier repo is reported as "capped".
_ACTIVITY_PAGE = 100


def weekly_activity(commits: list[dict], today: date, weeks: int = ACTIVITY_WEEKS) -> list[dict]:
    """Commit counts per week (weeks start Monday, UTC), oldest first,
    ending with the current week: [{"start": "YYYY-MM-DD", "commits": n}]."""
    this_monday = today - timedelta(days=today.weekday())
    starts = [this_monday - timedelta(weeks=weeks - 1 - i) for i in range(weeks)]
    counts = dict.fromkeys(starts, 0)
    for c in commits:
        try:
            day = datetime.fromisoformat(c["committed_at"].replace("Z", "+00:00")).astimezone(timezone.utc).date()
        except (KeyError, AttributeError, ValueError):
            continue
        monday = day - timedelta(days=day.weekday())
        if monday in counts:
            counts[monday] += 1
    return [{"start": s.isoformat(), "commits": counts[s]} for s in starts]


def fetch_repo_activity(repository_url: str, now: Optional[datetime] = None) -> tuple[list[dict], dict]:
    """(recent commits, activity) for the client dashboard. recent is the
    newest 5 commits; activity is {"weeks": weekly_activity(...),
    "capped": bool} covering the last ACTIVITY_WEEKS weeks. Usually one
    GitHub request; a second only when the repo was quiet for 12 weeks."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    window_start = datetime.combine(
        today - timedelta(days=today.weekday()) - timedelta(weeks=ACTIVITY_WEEKS - 1),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    window = fetch_recent_commits(repository_url, limit=_ACTIVITY_PAGE, since=window_start)
    recent = window[:5] if len(window) >= 5 else fetch_recent_commits(repository_url, limit=5)
    activity = {"weeks": weekly_activity(window, today), "capped": len(window) >= _ACTIVITY_PAGE}
    return recent, activity
