"""SEC-07 — who may invite which kind of account.

Staff roles are not tenant-scoped: they see every client. So only a
SUPER_ADMIN may grant one, and the organization an account is attached to
has to match its kind. Runs against the real app with a fake database.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app as real_app
from app.models.audit_log import AuditLog
from app.models.organization import Organization
from app.models.user import CLIENT_USER_ROLES, STAFF_USER_ROLES, USER_ROLES, User
from app.services.local_auth import INTERNAL_ORG_DOMAIN, is_internal_org

from tests.conftest import FakeSession, bearer, token_for

INVITE = "/api/v1/users/invite"


@pytest.fixture
def db():
    fake = FakeSession()
    fake.internal = Organization(id=uuid.uuid4(), name="GG HighTech (Internal)", domain=INTERNAL_ORG_DOMAIN, plan_tier="INTERNAL")
    fake.client_org = Organization(id=uuid.uuid4(), name="Acme Ltd", domain="acme.example", plan_tier="STANDARD")
    for org in (fake.internal, fake.client_org):
        fake._objects[(Organization, org.id)] = org
    real_app.dependency_overrides[get_db] = lambda: fake
    return fake


@pytest.fixture
def client(db):
    return TestClient(real_app)


def invite(client, caller_role, role, org, email=None):
    body = {
        "org_id": str(org.id),
        "email": email or f"{uuid.uuid4().hex[:8]}@example.com",
        "full_name": "New Person",
        "role": role,
    }
    return client.post(INVITE, json=body, headers=bearer(token_for(caller_role)))


def users_created(db):
    return [obj for obj in db.added if isinstance(obj, User)]


def audit_actions(db):
    return [obj.action for obj in db.added if isinstance(obj, AuditLog)]


def test_role_groups_cover_every_role_exactly_once():
    assert set(STAFF_USER_ROLES) | set(CLIENT_USER_ROLES) == set(USER_ROLES)
    assert not set(STAFF_USER_ROLES) & set(CLIENT_USER_ROLES)


# --- The escalation itself ---


def test_project_manager_cannot_invite_a_super_admin(client, db):
    response = invite(client, "PROJECT_MANAGER", "SUPER_ADMIN", db.internal)
    assert response.status_code == 403
    assert users_created(db) == []


@pytest.mark.parametrize("role", STAFF_USER_ROLES)
@pytest.mark.parametrize("org_name", ["internal", "client_org"])
def test_project_manager_cannot_invite_any_staff_role_into_any_org(client, db, role, org_name):
    response = invite(client, "PROJECT_MANAGER", role, getattr(db, org_name))
    assert response.status_code == 403
    assert "SUPER_ADMIN" in response.json()["detail"]
    assert users_created(db) == []


def test_denied_staff_invite_is_audited_with_who_tried_what(client, db):
    invite(client, "PROJECT_MANAGER", "SUPER_ADMIN", db.internal, email="me-again@example.com")
    (row,) = [obj for obj in db.added if isinstance(obj, AuditLog)]
    assert row.action == "user.invite_denied"
    assert row.actor_role == "PROJECT_MANAGER"
    assert row.event_metadata == {"email": "me-again@example.com", "role": "SUPER_ADMIN"}
    assert db.commits == 1


def test_denied_staff_invite_does_not_reveal_whether_the_org_exists(client, db):
    ghost_org = Organization(id=uuid.uuid4(), name="ghost")
    real = invite(client, "PROJECT_MANAGER", "SUPER_ADMIN", db.internal)
    missing = invite(client, "PROJECT_MANAGER", "SUPER_ADMIN", ghost_org)
    assert real.status_code == missing.status_code == 403
    assert real.json() == missing.json()


# --- What is still allowed ---


@pytest.mark.parametrize("role", CLIENT_USER_ROLES)
def test_project_manager_can_invite_client_users_into_a_client_org(client, db, role):
    response = invite(client, "PROJECT_MANAGER", role, db.client_org)
    assert response.status_code == 201
    (created,) = users_created(db)
    assert created.role == role
    assert created.org_id == db.client_org.id
    assert "user.invite" in audit_actions(db)


@pytest.mark.parametrize("role", STAFF_USER_ROLES)
def test_super_admin_can_invite_staff_into_the_internal_org(client, db, role):
    response = invite(client, "SUPER_ADMIN", role, db.internal)
    assert response.status_code == 201
    (created,) = users_created(db)
    assert created.role == role
    assert created.org_id == db.internal.id


@pytest.mark.parametrize("role", CLIENT_USER_ROLES)
def test_super_admin_can_invite_client_users_into_a_client_org(client, db, role):
    assert invite(client, "SUPER_ADMIN", role, db.client_org).status_code == 201


# --- Account kind must match organization kind ---


@pytest.mark.parametrize("role", STAFF_USER_ROLES)
def test_staff_accounts_cannot_be_attached_to_a_client_org(client, db, role):
    response = invite(client, "SUPER_ADMIN", role, db.client_org)
    assert response.status_code == 422
    assert users_created(db) == []


@pytest.mark.parametrize("caller", ["SUPER_ADMIN", "PROJECT_MANAGER"])
@pytest.mark.parametrize("role", CLIENT_USER_ROLES)
def test_client_accounts_cannot_be_attached_to_the_internal_org(client, db, caller, role):
    response = invite(client, caller, role, db.internal)
    assert response.status_code == 422
    assert users_created(db) == []


def test_unknown_organization_is_a_404_not_a_server_error(client, db):
    ghost_org = Organization(id=uuid.uuid4(), name="ghost")
    response = invite(client, "SUPER_ADMIN", "CLIENT_ADMIN", ghost_org)
    assert response.status_code == 404
    assert users_created(db) == []


def test_internal_org_is_recognised_by_domain_or_plan_tier():
    assert is_internal_org(Organization(name="a", domain=INTERNAL_ORG_DOMAIN, plan_tier="STANDARD"))
    assert is_internal_org(Organization(name="b", domain=None, plan_tier="INTERNAL"))
    assert not is_internal_org(Organization(name="c", domain="acme.example", plan_tier="STANDARD"))
    assert not is_internal_org(Organization(name="d", domain=None, plan_tier="ENTERPRISE"))


# --- Unchanged rules ---


@pytest.mark.parametrize("caller", ["LEAD_ENGINEER", "CLIENT_ADMIN", "CLIENT_VIEWER"])
def test_other_roles_still_cannot_invite_anyone(client, db, caller):
    assert invite(client, caller, "CLIENT_VIEWER", db.client_org).status_code == 403
    assert users_created(db) == []
    assert audit_actions(db) == []


def test_anonymous_cannot_invite(client, db):
    body = {"org_id": str(db.client_org.id), "email": "x@example.com", "full_name": "X", "role": "CLIENT_VIEWER"}
    assert client.post(INVITE, json=body).status_code == 401


def test_invalid_role_is_still_rejected(client, db):
    assert invite(client, "SUPER_ADMIN", "GOD_MODE", db.internal).status_code == 422


def test_duplicate_email_is_still_a_conflict(client, db):
    db.first_results[User] = lambda email: User(email=email) if email == "taken@example.com" else None
    response = invite(client, "SUPER_ADMIN", "CLIENT_ADMIN", db.client_org, email="taken@example.com")
    assert response.status_code == 409
    assert users_created(db) == []
