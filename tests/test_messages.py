"""Contact messages — public submit and the staff inbox.

Same setup as test_reviews.py: real SQL against in-memory SQLite, with only
the tables these routes touch.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import messages as messages_route
from app.db.session import get_db
from app.main import app as real_app
from app.models.audit_log import AuditLog
from app.models.contact_message import ContactMessage
from app.models.organization import Organization
from app.models.user import User
from app.services.local_auth import create_access_token_for

from tests.conftest import bearer

MESSAGES = "/api/v1/messages"
VALID = {
    "name": "Jane Doe",
    "email": "jane@client.example",
    "message": "Hi, we'd like a quote for a booking app for our clinic.",
}


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(element, compiler, **kw):
    return "JSON"


class _SqliteSession(Session):
    """Skip the Postgres-only `SET LOCAL` RLS statements (see test_reviews.py)."""

    def execute(self, statement, *args, **kwargs):
        if str(statement).lstrip().upper().startswith("SET LOCAL"):
            return None
        return super().execute(statement, *args, **kwargs)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for model in (Organization, User, ContactMessage, AuditLog):
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
def notifications(monkeypatch):
    sent = []
    monkeypatch.setattr(messages_route, "send_contact_notification", lambda **kw: sent.append(kw) or True)
    return sent


def staff(db, role="SUPER_ADMIN") -> dict:
    user = User(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        email=f"{role.lower()}-{uuid.uuid4().hex[:6]}@gghightech.example",
        full_name="Staff Member",
        role=role,
        password_hash="$2b$04$not-a-real-hash-but-non-empty",
        token_version=0,
    )
    db.add(user)
    db.commit()
    return bearer(create_access_token_for(user))


def send(client, ip=None, **overrides):
    headers = {"X-Forwarded-For": ip} if ip else {}
    return client.post(MESSAGES, json={**VALID, **overrides}, headers=headers)


# --- Sending ---


def test_a_message_is_saved_as_new_and_the_team_is_notified(client, db, notifications):
    response = send(client, phone="+1 (876) 555-0100", subject="Clinic app", source="WIDGET")
    assert response.status_code == 201
    msg = db.query(ContactMessage).one()
    assert (msg.status, msg.source, msg.phone, msg.subject) == ("NEW", "WIDGET", "+1 (876) 555-0100", "Clinic app")
    (sent,) = notifications
    assert sent["email"] == "jane@client.example"
    assert sent["message"] == VALID["message"]


def test_submission_is_audited(client, db, notifications):
    send(client)
    (row,) = db.query(AuditLog).all()
    assert row.action == "message.create"
    assert row.actor_role == "ANONYMOUS"


def test_phone_and_subject_are_optional(client, db, notifications):
    assert send(client, phone="  ", subject="").status_code == 201
    msg = db.query(ContactMessage).one()
    assert msg.phone is None and msg.subject is None


def test_unknown_source_is_recorded_as_the_contact_page(client, db, notifications):
    send(client, source="SOMETHING_ELSE")
    assert db.query(ContactMessage).one().source == "CONTACT_PAGE"


def test_links_are_allowed_since_messages_are_never_public(client, db, notifications):
    assert send(client, message="Our current site is https://clinic.example, please take a look.").status_code == 201


@pytest.mark.parametrize(
    "field,value",
    [("name", "J"), ("email", "not-an-email"), ("message", "hi"), ("phone", "call me maybe"), ("message", "x" * 5001)],
)
def test_invalid_submissions_are_rejected_and_nothing_is_saved(client, db, notifications, field, value):
    assert send(client, **{field: value}).status_code == 422
    assert db.query(ContactMessage).count() == 0
    assert notifications == []


def test_submitter_cannot_set_status(client, db, notifications):
    send(client, status="ARCHIVED")
    assert db.query(ContactMessage).one().status == "NEW"


def test_spam_trap_reports_success_but_saves_and_sends_nothing(client, db, notifications):
    assert send(client, website="http://bot.example").status_code == 201
    assert db.query(ContactMessage).count() == 0
    assert notifications == []


def test_submissions_are_rate_limited_per_address(client, db, notifications, configure):
    configure(CONTACT_SUBMIT_LIMIT_PER_HOUR=2)
    statuses = [send(client, email=f"p{i}@client.example").status_code for i in range(4)]
    assert statuses == [201, 201, 429, 429]


def test_one_email_cannot_flood_from_many_addresses(client, db, notifications, configure):
    configure(TRUSTED_PROXY_HOPS=1)
    statuses = [send(client, ip=f"198.51.100.{i}", email="Same@Client.Example").status_code for i in range(7)]
    assert statuses == [201] * 5 + [429, 429]


# --- Staff inbox ---


def test_staff_see_every_message_with_contact_details_and_can_filter(client, db, notifications):
    send(client, email="a@client.example")
    send(client, email="b@client.example")
    headers = staff(db)
    listed = client.get(f"{MESSAGES}/admin", headers=headers).json()
    assert sorted(m["email"] for m in listed) == ["a@client.example", "b@client.example"]
    first = listed[0]["id"]
    client.patch(f"{MESSAGES}/{first}", json={"status": "ARCHIVED"}, headers=headers)
    assert len(client.get(f"{MESSAGES}/admin?status=NEW", headers=headers).json()) == 1
    assert client.get(f"{MESSAGES}/admin?status=BOGUS", headers=headers).status_code == 422


def test_unread_count_drops_when_a_message_is_read(client, db, notifications):
    send(client)
    send(client, email="other@client.example")
    headers = staff(db, "PROJECT_MANAGER")
    assert client.get(f"{MESSAGES}/admin/unread-count", headers=headers).json() == {"unread": 2}
    msg_id = client.get(f"{MESSAGES}/admin", headers=headers).json()[0]["id"]
    updated = client.patch(f"{MESSAGES}/{msg_id}", json={"status": "READ"}, headers=headers).json()
    assert updated["status"] == "READ" and updated["handled_by"].startswith("project_manager-")
    assert client.get(f"{MESSAGES}/admin/unread-count", headers=headers).json() == {"unread": 1}


def test_update_rejects_unknown_status_and_unknown_message(client, db, notifications):
    send(client)
    headers = staff(db)
    msg_id = db.query(ContactMessage).one().id
    assert client.patch(f"{MESSAGES}/{msg_id}", json={"status": "SPAM"}, headers=headers).status_code == 422
    assert client.patch(f"{MESSAGES}/{uuid.uuid4()}", json={"status": "READ"}, headers=headers).status_code == 404


def test_super_admin_can_delete(client, db, notifications):
    send(client)
    msg_id = db.query(ContactMessage).one().id
    assert client.delete(f"{MESSAGES}/{msg_id}", headers=staff(db)).status_code == 204
    assert db.query(ContactMessage).count() == 0


def test_project_manager_cannot_delete(client, db, notifications):
    send(client)
    msg_id = db.query(ContactMessage).one().id
    assert client.delete(f"{MESSAGES}/{msg_id}", headers=staff(db, "PROJECT_MANAGER")).status_code == 403
    assert db.query(ContactMessage).count() == 1


@pytest.mark.parametrize("role", ["LEAD_ENGINEER", "CLIENT_ADMIN", "CLIENT_VIEWER"])
def test_other_roles_cannot_read_the_inbox(client, db, notifications, role):
    send(client)
    headers = staff(db, role)
    msg_id = db.query(ContactMessage).one().id
    assert client.get(f"{MESSAGES}/admin", headers=headers).status_code == 403
    assert client.get(f"{MESSAGES}/admin/unread-count", headers=headers).status_code == 403
    assert client.patch(f"{MESSAGES}/{msg_id}", json={"status": "READ"}, headers=headers).status_code == 403


def test_anonymous_cannot_read_the_inbox(client, db, notifications):
    send(client)
    msg_id = db.query(ContactMessage).one().id
    assert client.get(f"{MESSAGES}/admin").status_code == 401
    assert client.patch(f"{MESSAGES}/{msg_id}", json={"status": "READ"}).status_code == 401
    assert client.delete(f"{MESSAGES}/{msg_id}").status_code == 401
