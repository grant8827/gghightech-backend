"""Projects & Git — repo / live domain / staging settings, GitHub sync
storing recent commits, and the background auto-sync pass that keeps the
client dashboard current.

Real SQL against in-memory SQLite (same approach as test_instant_payments.py);
GitHub is replaced so nothing leaves the process.
"""

import asyncio
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import get_db
from app.main import app as real_app
from app.models.audit_log import AuditLog
from app.models.milestone import Milestone
from app.models.organization import Organization
from app.models.project import Project
from app.models.user import User
from app.services import github_service, project_sync
from app.services.local_auth import create_access_token_for

from tests.conftest import bearer

PROJECTS = "/api/v1/projects"


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(element, compiler, **kw):
    return "JSON"


class _SqliteSession(Session):
    """Skip the Postgres-only `SET LOCAL` RLS statements."""

    def execute(self, statement, *args, **kwargs):
        if str(statement).lstrip().upper().startswith("SET LOCAL"):
            return None
        return super().execute(statement, *args, **kwargs)


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for model in (Organization, User, Project, Milestone, AuditLog):
        model.__table__.create(engine)
    yield sessionmaker(bind=engine, class_=_SqliteSession, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def db(factory):
    session = factory()
    real_app.dependency_overrides[get_db] = lambda: session
    yield session
    session.close()


@pytest.fixture
def client(db):
    return TestClient(real_app)


def github_commit(sha: str, message: str) -> dict:
    return {
        "sha": sha,
        "html_url": f"https://github.com/acme/site/commit/{sha}",
        "author": {"login": "dev-gg"},
        "commit": {
            "message": f"{message}\n\nLonger body that clients don't need.",
            "author": {"name": "Dev"},
            "committer": {"date": "2026-11-13T10:00:00Z"},
        },
    }


@pytest.fixture
def github(monkeypatch):
    """Fake GitHub: `github["commits"]` is what the commits API returns;
    `github["status"]` its HTTP status. Records the URLs asked for."""
    state = {"commits": [github_commit("a" * 40, "Add checkout page")], "status": 200, "calls": []}

    def fake_get(url, headers=None, params=None, timeout=None):
        state["calls"].append(url)
        body = state["commits"] if state["status"] == 200 else {"message": "Not Found"}
        return httpx.Response(state["status"], json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(github_service.httpx, "get", fake_get)
    return state


def make_project(db, **fields) -> Project:
    org = Organization(id=uuid.uuid4(), name="Acme")
    db.add(org)
    db.flush()
    project = Project(id=uuid.uuid4(), org_id=org.id, title="Acme site", slug=uuid.uuid4().hex, **fields)
    db.add(project)
    db.commit()
    return project


def as_role(db, role="SUPER_ADMIN", org_id=None) -> dict:
    user = User(
        id=uuid.uuid4(),
        org_id=org_id or uuid.uuid4(),
        email=f"{role.lower()}-{uuid.uuid4().hex[:6]}@gghightech.example",
        full_name="Someone",
        role=role,
        password_hash="$2b$04$not-a-real-hash-but-non-empty",
        token_version=0,
    )
    db.add(user)
    db.commit()
    return bearer(create_access_token_for(user))


def test_settings_are_cleaned_up_on_save(client, db):
    project = make_project(db)
    response = client.patch(
        f"{PROJECTS}/{project.id}",
        json={
            "repository_url": "git@github.com:acme/site.git",
            "live_url": " acme.com ",
            "staging_url": "https://staging.acme.com/",
        },
        headers=as_role(db),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["repository_url"] == "https://github.com/acme/site"
    assert body["live_url"] == "https://acme.com"
    assert body["staging_url"] == "https://staging.acme.com"


def test_blank_clears_a_setting(client, db):
    project = make_project(db, live_url="https://acme.com")
    response = client.patch(f"{PROJECTS}/{project.id}", json={"live_url": ""}, headers=as_role(db))
    assert response.status_code == 200
    assert response.json()["live_url"] is None


@pytest.mark.parametrize(
    "bad",
    [
        {"repository_url": "https://gitlab.com/acme/site"},
        {"live_url": "not a website"},
        {"staging_url": "ftp://acme.com"},
        {"live_url": "javascript:alert(1)"},
    ],
)
def test_bad_settings_are_rejected(client, db, bad):
    project = make_project(db)
    assert client.patch(f"{PROJECTS}/{project.id}", json=bad, headers=as_role(db)).status_code == 422


@pytest.mark.parametrize("role", ["CLIENT_ADMIN", "CLIENT_VIEWER"])
def test_clients_cannot_change_settings(client, db, role):
    project = make_project(db)
    headers = as_role(db, role, project.org_id)
    assert client.patch(f"{PROJECTS}/{project.id}", json={"live_url": "evil.com"}, headers=headers).status_code == 403


def test_sync_stores_recent_commits_the_client_can_read(client, db, github):
    project = make_project(db, repository_url="https://github.com/acme/site", live_url="https://acme.com")
    github["commits"] = [github_commit("b" * 40, "Polish the homepage"), github_commit("a" * 40, "Add checkout page")]

    synced = client.post(f"{PROJECTS}/{project.id}/sync-github", headers=as_role(db))
    assert synced.status_code == 200
    # The 12-week window first; it held fewer than 5 commits, so the newest
    # 5 are fetched separately for the "recent work" list.
    assert github["calls"] == ["https://api.github.com/repos/acme/site/commits"] * 2

    seen = client.get(f"{PROJECTS}/{project.id}", headers=as_role(db, "CLIENT_VIEWER", project.org_id)).json()
    assert seen["live_url"] == "https://acme.com"
    assert seen["latest_commit_message"] == "Polish the homepage"
    assert [c["message"] for c in seen["recent_commits"]] == ["Polish the homepage", "Add checkout page"]
    assert seen["recent_commits"][0]["author"] == "dev-gg"
    assert seen["recent_commits"][0]["url"].endswith("b" * 40)


def test_sync_error_is_explained(client, db, github):
    project = make_project(db, repository_url="https://github.com/acme/private")
    github["status"] = 404
    response = client.post(f"{PROJECTS}/{project.id}/sync-github", headers=as_role(db))
    assert response.status_code == 422
    assert "GITHUB_TOKEN" in response.json()["detail"]


def test_changing_the_repo_drops_the_old_commits(client, db, github):
    project = make_project(db, repository_url="https://github.com/acme/site")
    headers = as_role(db)
    client.post(f"{PROJECTS}/{project.id}/sync-github", headers=headers)

    body = client.patch(f"{PROJECTS}/{project.id}", json={"repository_url": "acme/new-site"}, headers=headers).json()
    assert body["recent_commits"] is None
    assert body["latest_commit_sha"] is None

    # Saving the same repo again keeps them.
    client.post(f"{PROJECTS}/{project.id}/sync-github", headers=headers)
    again = client.patch(f"{PROJECTS}/{project.id}", json={"repository_url": "acme/new-site"}, headers=headers).json()
    assert again["recent_commits"] is not None


def test_background_pass_syncs_every_repo_and_survives_failures(factory, db, github, monkeypatch):
    monkeypatch.setattr(project_sync, "SessionLocal", factory)
    broadcasts = []

    async def fake_broadcast(project_id, message):
        broadcasts.append((project_id, message))

    monkeypatch.setattr(project_sync.manager, "broadcast", fake_broadcast)

    good = make_project(db, repository_url="https://github.com/acme/site")
    broken = make_project(db, repository_url="https://github.com/acme/broken")
    no_repo = make_project(db)

    real_get = github_service.httpx.get

    def picky_get(url, **kwargs):
        if "broken" in url:
            raise httpx.ConnectError("boom")
        return real_get(url, **kwargs)

    monkeypatch.setattr(github_service.httpx, "get", picky_get)

    assert asyncio.run(project_sync.sync_all_projects()) == 1
    assert broadcasts == [(good.id, {"type": "github_synced"})]

    fresh = factory()
    assert fresh.get(Project, good.id).latest_commit_message == "Add checkout page"
    assert fresh.get(Project, broken.id).recent_commits is None
    assert fresh.get(Project, no_repo.id).recent_commits is None

    # Nothing new on GitHub: no notification the second time.
    broadcasts.clear()
    assert asyncio.run(project_sync.sync_all_projects()) == 0
    assert broadcasts == []


def test_create_project_with_repo_and_domain_syncs_right_away(client, db, github):
    org = Organization(id=uuid.uuid4(), name="Acme")
    db.add(org)
    db.commit()
    response = client.post(
        PROJECTS,
        json={
            "org_id": str(org.id),
            "title": "Acme site",
            "slug": "acme-site",
            "repository_url": "acme/site",
            "live_url": "acme.com",
            "staging_url": "staging.acme.com",
        },
        headers=as_role(db),
    )
    assert response.status_code == 201
    body = response.json()
    assert (body["repository_url"], body["live_url"], body["staging_url"]) == (
        "https://github.com/acme/site",
        "https://acme.com",
        "https://staging.acme.com",
    )
    assert body["latest_commit_message"] == "Add checkout page"


def test_create_project_still_works_when_github_fails(client, db, github):
    org = Organization(id=uuid.uuid4(), name="Acme")
    db.add(org)
    db.commit()
    github["status"] = 404
    response = client.post(
        PROJECTS,
        json={"org_id": str(org.id), "title": "Acme", "slug": "acme", "repository_url": "acme/private"},
        headers=as_role(db),
    )
    assert response.status_code == 201
    assert response.json()["recent_commits"] is None


def test_weekly_activity_buckets_by_monday_week():
    from datetime import date

    from app.services.github_service import weekly_activity

    commits = [
        {"committed_at": "2026-11-13T10:00:00Z"},  # Fri, this week
        {"committed_at": "2026-11-09T00:30:00Z"},  # Mon, this week
        {"committed_at": "2026-11-08T23:59:00Z"},  # Sun, last week
        {"committed_at": "2026-08-01T12:00:00Z"},  # before the window: ignored
        {"committed_at": "not a date"},
    ]
    weeks = weekly_activity(commits, today=date(2026, 11, 13))
    assert len(weeks) == 12
    assert weeks[-1] == {"start": "2026-11-09", "commits": 2}
    assert weeks[-2] == {"start": "2026-11-02", "commits": 1}
    assert weeks[0]["start"] == "2026-08-24"
    assert sum(w["commits"] for w in weeks) == 3


def test_sync_stores_activity_for_the_chart(client, db, github):
    project = make_project(db, repository_url="https://github.com/acme/site")
    body = client.post(f"{PROJECTS}/{project.id}/sync-github", headers=as_role(db)).json()
    activity = body["commit_activity"]
    assert len(activity["weeks"]) == 12
    assert activity["capped"] is False
