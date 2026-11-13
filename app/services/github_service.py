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


def fetch_recent_commits(repository_url: str, limit: int = 5) -> list[dict]:
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
            params={"per_page": limit},
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
    if not commits:
        raise GithubSyncError(f"{owner}/{repo} has no commits")
    return commits


def fetch_latest_commit(repository_url: str) -> dict:
    """Latest commit only — see fetch_recent_commits."""
    return fetch_recent_commits(repository_url, limit=1)[0]
