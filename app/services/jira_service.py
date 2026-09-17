"""Jira sync — outbound polling, triggered by staff hitting
POST /projects/{id}/sync-jira (see app/api/routes/projects.py). Unlike
GitHub, every Jira Cloud REST call needs auth, so unlike github_service.py
this can't do *anything* until JIRA_BASE_URL/JIRA_EMAIL/JIRA_API_TOKEN are
all set — there is no real Jira instance to test against right now, so this
is verified structurally only (request/auth shape), not against live data.

Deliberately not a webhook receiver, same reasoning as github_service.py.
"""

import httpx

from app.core.config import settings


class JiraSyncError(Exception):
    """Message is safe to show the caller directly — never includes the token."""


class JiraNotConfiguredError(JiraSyncError):
    pass


def fetch_issue_progress(project_key: str) -> dict:
    """Issue counts for a Jira project key. Only counts the first 100
    issues returned (no pagination) — fine for the small teams this app
    targets, but a real limitation worth knowing about for a large backlog.
    """
    if not (settings.JIRA_BASE_URL and settings.JIRA_EMAIL and settings.JIRA_API_TOKEN):
        raise JiraNotConfiguredError(
            "Jira isn't connected yet — set JIRA_BASE_URL, JIRA_EMAIL, and JIRA_API_TOKEN."
        )

    try:
        resp = httpx.get(
            f"{settings.JIRA_BASE_URL.rstrip('/')}/rest/api/3/search",
            params={"jql": f"project={project_key}", "fields": "status", "maxResults": 100},
            auth=(settings.JIRA_EMAIL, settings.JIRA_API_TOKEN),
            headers={"Accept": "application/json"},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise JiraSyncError(f"Could not reach Jira: {exc}") from exc

    if resp.status_code != 200:
        raise JiraSyncError(f"Jira API returned {resp.status_code}: {resp.text}")

    issues = resp.json().get("issues", [])
    done = sum(1 for issue in issues if issue["fields"]["status"]["statusCategory"]["key"] == "done")
    return {"issue_count": len(issues), "done_count": done}
