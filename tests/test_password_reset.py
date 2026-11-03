"""Forgot / reset password, and the API docs being off in production.

Real SQL against in-memory SQLite (same approach as test_reviews.py); the
reset email is captured instead of sent.
"""

import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import auth as auth_routes
from app.db.session import get_db
from app.main import app as real_app
from app.models.audit_log import AuditLog
from app.models.user import User
from app.services.local_auth import create_invite_token, hash_password

from tests.conftest import bearer

FORGOT = "/api/v1/auth/forgot-password"
RESET = "/api/v1/auth/reset-password"
LOGIN = "/api/v1/auth/login"
OLD_PASSWORD = "the-old-password-123"
NEW_PASSWORD = "a-brand-new-password-456"


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
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for model in (User, AuditLog):
        model.__table__.create(engine)
    session = sessionmaker(bind=engine, class_=_SqliteSession, expire_on_commit=False)()
    real_app.dependency_overrides[get_db] = lambda: session
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def client(db):
    return TestClient(real_app)


@pytest.fixture
def emails(monkeypatch):
    sent = []
    monkeypatch.setattr(
        auth_routes, "send_password_reset_email", lambda to, name, link: sent.append((to, link)) or True
    )
    return sent


def add_user(db, email="client@example.com", password=OLD_PASSWORD) -> User:
    user = User(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        email=email,
        full_name="Client Person",
        role="CLIENT_ADMIN",
        password_hash=hash_password(password) if password else None,
        token_version=0,
    )
    db.add(user)
    db.commit()
    return user


def token_from(link: str) -> str:
    return parse_qs(urlparse(link).query)["token"][0]


def test_forgot_password_emails_a_link_to_the_reset_page(client, db, emails):
    add_user(db)
    response = client.post(FORGOT, json={"email": "client@example.com"})

    assert response.status_code == 202
    ((to, link),) = emails
    assert to == "client@example.com"
    assert link.startswith("http://localhost:3000/reset-password?token=")


def test_unknown_email_gets_the_same_answer_and_no_email(client, db, emails):
    add_user(db)
    known = client.post(FORGOT, json={"email": "client@example.com"})
    unknown = client.post(FORGOT, json={"email": "nobody@example.com"})

    assert (unknown.status_code, unknown.json()) == (known.status_code, known.json())
    assert [to for to, _ in emails] == ["client@example.com"]


def test_reset_sets_the_new_password_and_signs_in(client, db, emails):
    add_user(db)
    client.post(FORGOT, json={"email": "client@example.com"})
    token = token_from(emails[0][1])

    response = client.post(RESET, json={"token": token, "password": NEW_PASSWORD})
    assert response.status_code == 200
    assert response.json()["email"] == "client@example.com"

    assert client.post(LOGIN, json={"email": "client@example.com", "password": OLD_PASSWORD}).status_code == 401
    assert client.post(LOGIN, json={"email": "client@example.com", "password": NEW_PASSWORD}).status_code == 200


def test_a_reset_link_works_only_once(client, db, emails):
    add_user(db)
    client.post(FORGOT, json={"email": "client@example.com"})
    token = token_from(emails[0][1])

    assert client.post(RESET, json={"token": token, "password": NEW_PASSWORD}).status_code == 200
    again = client.post(RESET, json={"token": token, "password": "yet-another-password-789"})
    assert again.status_code == 401


def test_reset_signs_out_existing_sessions(client, db, emails):
    user = add_user(db)
    old_session = client.post(LOGIN, json={"email": user.email, "password": OLD_PASSWORD}).json()["access_token"]
    assert client.get("/api/v1/auth/me", headers=bearer(old_session)).status_code == 200

    client.post(FORGOT, json={"email": user.email})
    client.post(RESET, json={"token": token_from(emails[0][1]), "password": NEW_PASSWORD})

    assert client.get("/api/v1/auth/me", headers=bearer(old_session)).status_code == 401


def test_short_passwords_are_refused(client, db, emails):
    add_user(db)
    client.post(FORGOT, json={"email": "client@example.com"})
    response = client.post(RESET, json={"token": token_from(emails[0][1]), "password": "short"})
    assert response.status_code == 422


@pytest.mark.parametrize("token", ["not-a-token", "eyJhbGciOiJIUzI1NiJ9.e30.bad-signature"])
def test_garbage_tokens_are_refused(client, db, token):
    assert client.post(RESET, json={"token": token, "password": NEW_PASSWORD}).status_code == 401


def test_an_invite_token_is_not_a_reset_token(client, db):
    user = add_user(db)
    response = client.post(RESET, json={"token": create_invite_token(str(user.id)), "password": NEW_PASSWORD})
    assert response.status_code == 401


def test_an_invited_user_whose_invite_expired_can_set_a_password(client, db, emails):
    add_user(db, email="invited@example.com", password=None)
    client.post(FORGOT, json={"email": "invited@example.com"})
    response = client.post(RESET, json={"token": token_from(emails[0][1]), "password": NEW_PASSWORD})
    assert response.status_code == 200


def test_reset_emails_to_one_address_are_capped(client, db, emails):
    add_user(db)
    for _ in range(3):
        client.post(FORGOT, json={"email": "client@example.com"}, headers={"X-Forwarded-For": f"203.0.113.{_}"})
    client.post(FORGOT, json={"email": "client@example.com"})
    assert len(emails) == 3


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_are_not_served_in_production(path):
    assert TestClient(real_app).get(path).status_code == 404
