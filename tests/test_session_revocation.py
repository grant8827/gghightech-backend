"""SEC-06 — a signed token is only as good as the account behind it.

Every signed-in request must be checked against the users table: the
account still exists, its sessions haven't been revoked, and the role used
for authorization is the one in the database right now — not the one that
was written into the token when it was issued.
"""

import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.routes import ws as ws_routes
from app.core.config import settings
from app.db.session import get_db
from app.main import app as real_app
from app.models.audit_log import AuditLog
from app.models.organization import Organization
from app.models.user import User
from app.services.local_auth import (
    ALGORITHM,
    create_access_token,
    create_access_token_for,
    create_or_update_superadmin,
    revoke_sessions,
)

from tests.conftest import _USERS, FakeSession, bearer, make_user

ME = "/api/v1/auth/me"
LOGOUT = "/api/v1/auth/logout"
LOGIN = "/api/v1/auth/login"


def staff_only_url() -> str:
    """A staff-only route that needs no data: 404 for staff, 403 for
    clients, 401 for nobody."""
    return f"/api/v1/estimates/{uuid.uuid4()}"


def revoke_url(user) -> str:
    return f"/api/v1/users/{user.id}/revoke-sessions"


@pytest.fixture
def db():
    fake = FakeSession()
    real_app.dependency_overrides[get_db] = lambda: fake
    return fake


@pytest.fixture
def client(db):
    return TestClient(real_app)


def audit_actions(db):
    return [row.action for row in db.added if isinstance(row, AuditLog)]


# --- The account must still exist ---


def test_valid_token_for_an_existing_user_is_accepted(client):
    user = make_user("PROJECT_MANAGER", email="pm@example.com")
    response = client.get(ME, headers=bearer(create_access_token_for(user)))
    assert response.status_code == 200
    assert response.json() == {"email": "pm@example.com", "role": "PROJECT_MANAGER"}


def test_token_stops_working_the_moment_the_user_is_deleted(client):
    user = make_user("SUPER_ADMIN")
    token = create_access_token_for(user)
    assert client.get(staff_only_url(), headers=bearer(token)).status_code == 404  # authorized

    del _USERS[user.id]

    assert client.get(ME, headers=bearer(token)).status_code == 401
    assert client.get(staff_only_url(), headers=bearer(token)).status_code == 401


def test_deleting_a_user_through_the_api_ends_their_session(client):
    admin = make_user("SUPER_ADMIN")
    leaver = make_user("LEAD_ENGINEER")
    leaver_token = create_access_token_for(leaver)
    assert client.get(ME, headers=bearer(leaver_token)).status_code == 200

    deleted = client.delete(f"/api/v1/users/{leaver.id}", headers=bearer(create_access_token_for(admin)))

    assert deleted.status_code == 204
    assert client.get(ME, headers=bearer(leaver_token)).status_code == 401


def test_token_for_a_user_that_never_existed_is_rejected(client):
    orphan = create_access_token(str(uuid.uuid4()), "ghost@example.com", "SUPER_ADMIN")
    assert client.get(ME, headers=bearer(orphan)).status_code == 401


def test_invited_user_who_never_set_a_password_has_no_session(client):
    pending = make_user("CLIENT_ADMIN", password_hash=None)
    assert client.get(ME, headers=bearer(create_access_token_for(pending))).status_code == 401


@pytest.mark.parametrize("subject", ["not-a-uuid", "", "12345"])
def test_token_with_a_malformed_subject_is_rejected(client, subject):
    token = jwt.encode(
        {"sub": subject, "role": "SUPER_ADMIN", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        settings.SECRET_KEY,
        algorithm=ALGORITHM,
    )
    assert client.get(ME, headers=bearer(token)).status_code == 401


# --- The role comes from the database, not the token ---


def test_demotion_takes_effect_immediately(client):
    user = make_user("SUPER_ADMIN")
    token = create_access_token_for(user)  # the token still says SUPER_ADMIN
    assert client.get(staff_only_url(), headers=bearer(token)).status_code == 404

    user.role = "CLIENT_VIEWER"

    assert client.get(staff_only_url(), headers=bearer(token)).status_code == 403
    assert client.get(ME, headers=bearer(token)).json()["role"] == "CLIENT_VIEWER"


def test_role_claim_in_the_token_is_never_trusted(client):
    """Even a correctly signed token can't grant more than the user row
    does — e.g. one issued before a demotion."""
    user = make_user("CLIENT_VIEWER")
    inflated = create_access_token(str(user.id), user.email, "SUPER_ADMIN", user.token_version)
    assert client.get(staff_only_url(), headers=bearer(inflated)).status_code == 403
    assert client.get(ME, headers=bearer(inflated)).json()["role"] == "CLIENT_VIEWER"


def test_promotion_also_takes_effect_without_a_new_login(client):
    user = make_user("CLIENT_VIEWER")
    token = create_access_token_for(user)
    assert client.get(staff_only_url(), headers=bearer(token)).status_code == 403
    user.role = "PROJECT_MANAGER"
    assert client.get(staff_only_url(), headers=bearer(token)).status_code == 404


def test_email_comes_from_the_database(client):
    user = make_user("SUPER_ADMIN", email="old@example.com")
    token = create_access_token_for(user)
    user.email = "new@example.com"
    assert client.get(ME, headers=bearer(token)).json()["email"] == "new@example.com"


# --- Token version ---


def test_revoking_sessions_invalidates_every_outstanding_token(client):
    user = make_user("SUPER_ADMIN")
    laptop, phone = create_access_token_for(user), create_access_token_for(user)

    revoke_sessions(user)

    assert client.get(ME, headers=bearer(laptop)).status_code == 401
    assert client.get(ME, headers=bearer(phone)).status_code == 401


def test_a_token_issued_after_revocation_works(client):
    user = make_user("SUPER_ADMIN")
    revoke_sessions(user)
    assert client.get(ME, headers=bearer(create_access_token_for(user))).status_code == 200


def test_token_claiming_a_future_version_is_rejected(client):
    user = make_user("SUPER_ADMIN")
    ahead = create_access_token(str(user.id), user.email, user.role, token_version=user.token_version + 1)
    assert client.get(ME, headers=bearer(ahead)).status_code == 401


def legacy_token(user) -> str:
    """As issued before token_version existed: no "ver" claim."""
    return jwt.encode(
        {
            "sub": str(user.id),
            "email": user.email,
            "role": user.role,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        settings.SECRET_KEY,
        algorithm=ALGORITHM,
    )


def test_pre_upgrade_tokens_keep_working_so_deploying_signs_nobody_out(client):
    user = make_user("SUPER_ADMIN")
    assert client.get(ME, headers=bearer(legacy_token(user))).status_code == 200


def test_pre_upgrade_tokens_are_revocable_too(client):
    user = make_user("SUPER_ADMIN")
    token = legacy_token(user)
    revoke_sessions(user)
    assert client.get(ME, headers=bearer(token)).status_code == 401


def test_revoking_one_user_does_not_affect_another(client):
    alice, bob = make_user("SUPER_ADMIN"), make_user("SUPER_ADMIN")
    bob_token = create_access_token_for(bob)
    revoke_sessions(alice)
    assert client.get(ME, headers=bearer(bob_token)).status_code == 200


# --- POST /auth/logout ---


def test_logout_ends_the_session_on_the_server(client, db):
    user = make_user("PROJECT_MANAGER")
    token = create_access_token_for(user)

    assert client.post(LOGOUT, headers=bearer(token)).status_code == 204

    assert client.get(ME, headers=bearer(token)).status_code == 401
    assert user.token_version == 1
    assert audit_actions(db) == ["auth.logout"]
    assert db.commits == 1


def test_logout_ends_the_users_other_sessions_as_well(client):
    user = make_user("PROJECT_MANAGER")
    laptop, phone = create_access_token_for(user), create_access_token_for(user)
    client.post(LOGOUT, headers=bearer(laptop))
    assert client.get(ME, headers=bearer(phone)).status_code == 401


def test_logout_requires_a_valid_session(client):
    assert client.post(LOGOUT).status_code == 401
    assert client.post(LOGOUT, headers=bearer("not-a-jwt")).status_code == 401


def test_logging_out_twice_with_the_same_token_fails_the_second_time(client):
    user = make_user("PROJECT_MANAGER")
    token = create_access_token_for(user)
    assert client.post(LOGOUT, headers=bearer(token)).status_code == 204
    assert client.post(LOGOUT, headers=bearer(token)).status_code == 401
    assert user.token_version == 1


def test_signing_in_again_after_logout_works(client, db):
    password = "correct horse battery staple"
    user = make_user(
        "PROJECT_MANAGER",
        email="pm@gghightech.example",
        password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode(),
    )
    db.first_results[User] = lambda email: user if email == user.email else None

    first = client.post(LOGIN, json={"email": user.email, "password": password}).json()["access_token"]
    client.post(LOGOUT, headers=bearer(first))
    second = client.post(LOGIN, json={"email": user.email, "password": password}).json()["access_token"]

    assert client.get(ME, headers=bearer(first)).status_code == 401
    assert client.get(ME, headers=bearer(second)).status_code == 200


# --- POST /users/{id}/revoke-sessions ---


def test_super_admin_can_revoke_another_users_sessions(client, db):
    admin, target = make_user("SUPER_ADMIN"), make_user("CLIENT_ADMIN", email="client@example.com")
    admin_token, target_token = create_access_token_for(admin), create_access_token_for(target)

    response = client.post(revoke_url(target), headers=bearer(admin_token))

    assert response.status_code == 204
    assert client.get(ME, headers=bearer(target_token)).status_code == 401
    assert client.get(ME, headers=bearer(admin_token)).status_code == 200
    assert audit_actions(db) == ["user.revoke_sessions"]
    assert target.password_hash  # still able to sign in again


@pytest.mark.parametrize("role", ["PROJECT_MANAGER", "LEAD_ENGINEER", "CLIENT_ADMIN", "CLIENT_VIEWER"])
def test_only_super_admin_can_revoke_sessions(client, role):
    caller, target = make_user(role), make_user("CLIENT_ADMIN")
    target_token = create_access_token_for(target)
    assert client.post(revoke_url(target), headers=bearer(create_access_token_for(caller))).status_code == 403
    assert client.get(ME, headers=bearer(target_token)).status_code == 200


def test_revoke_sessions_requires_sign_in_and_a_real_user(client):
    target = make_user("CLIENT_ADMIN")
    assert client.post(revoke_url(target)).status_code == 401
    admin_token = create_access_token_for(make_user("SUPER_ADMIN"))
    missing = f"/api/v1/users/{uuid.uuid4()}/revoke-sessions"
    assert client.post(missing, headers=bearer(admin_token)).status_code == 404


# --- A password reset signs the old sessions out ---


def test_resetting_a_superadmin_password_revokes_existing_sessions(client, db):
    user = make_user("PROJECT_MANAGER", email="root@gghightech.example")
    old_token = create_access_token_for(user)
    db.first_results[Organization] = lambda _: Organization(id=uuid.uuid4(), name="Internal")
    db.first_results[User] = lambda email: user if email == user.email else None

    create_or_update_superadmin(db, user.email, "a-brand-new-long-password", "Root Admin")

    assert client.get(ME, headers=bearer(old_token)).status_code == 401
    assert client.get(ME, headers=bearer(create_access_token_for(user))).json()["role"] == "SUPER_ADMIN"


# --- WebSocket uses the same check ---


def ws_close_code(client, token) -> int:
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(f"/api/v1/ws/projects/{uuid.uuid4()}?token={token}"):
            pass
    return closed.value.code


def test_websocket_rejects_revoked_and_deleted_users(client, monkeypatch):
    monkeypatch.setattr(ws_routes, "SessionLocal", FakeSession)
    user = make_user("SUPER_ADMIN")
    token = create_access_token_for(user)
    # 4404 = signed in fine, project not found. 4401 = not signed in.
    assert ws_close_code(client, token) == 4404

    revoke_sessions(user)
    assert ws_close_code(client, token) == 4401

    fresh = create_access_token_for(user)
    assert ws_close_code(client, fresh) == 4404
    del _USERS[user.id]
    assert ws_close_code(client, fresh) == 4401
