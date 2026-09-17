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


def fetch_latest_commit(repository_url: str) -> dict:
    """Latest commit on the repo's default branch. Raises GithubSyncError
    (never a raw httpx/JSON exception) on any failure — bad URL, network
    error, or a non-200 from GitHub (404 repo not found, 403 rate-limited,
    etc.) — with GitHub's own error message included, not swallowed."""
    owner, repo = _parse_repo(repository_url)
    headers = {"Accept": "application/vnd.github+json"}
    if settings.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"

    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/commits",
            headers=headers,
            params={"per_page": 1},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise GithubSyncError(f"Could not reach GitHub: {exc}") from exc

    if resp.status_code != 200:
        try:
            detail = resp.json().get("message", resp.text)
        except ValueError:
            detail = resp.text
        raise GithubSyncError(f"GitHub API returned {resp.status_code}: {detail}")

    commits = resp.json()
    if not commits:
        raise GithubSyncError(f"{owner}/{repo} has no commits")

    commit = commits[0]
    return {
        "sha": commit["sha"],
        "message": commit["commit"]["message"].splitlines()[0],
        "committed_at": commit["commit"]["committer"]["date"],
    }
