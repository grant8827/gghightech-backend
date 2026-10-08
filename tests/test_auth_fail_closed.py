"""SEC-01 — request authentication must fail closed.

Scenario letters (A-G) match the remediation brief. HTTP tests go through
the real FastAPI app; role and tenant-scope tests use a small app built on
the real dependencies (require_roles, get_current_user_org_id) with the DB
session replaced by FakeSession, so no database is touched.
"""

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings, settings
from app.db.session import get_db
from app.main import app as real_app
from app.services.auth import get_current_user_org_id, require_roles
from app.services.local_auth import ALGORITHM, create_access_token, create_invite_token

from tests.conftest import FakeSession, bearer, make_user, token_for

ME = "/api/v1/auth/me"
DEV_HEADERS = {"X-Dev-User-Role": "SUPER_ADMIN", "X-Dev-User-Email": "attacker@example.com"}

BYPASS_ON = "SET LOCAL app.bypass_rls = 'true'"
BYPASS_OFF = "SET LOCAL app.bypass_rls = 'false'"


@pytest.fixture
def client():
    return TestClient(real_app)


@pytest.fixture
def scoped():
    """A minimal app exposing the real RBAC / tenant-scope dependencies,
    plus the FakeSession they run against."""
    db = FakeSession()
    mini = FastAPI()

    @mini.get("/staff-only")
    def staff_only(user=Depends(require_roles("SUPER_ADMIN", "PROJECT_MANAGER"))):
        return {"role": user.role}

    @mini.get("/tenant-scoped")
    def tenant_scoped(org_id=Depends(get_current_user_org_id)):
        return {"org_id": str(org_id) if org_id else None}

    mini.dependency_overrides[get_db] = lambda: db
    return TestClient(mini), db


# --- A. Production + no token + dev headers => 401 ---


def test_a_production_rejects_dev_headers(client, configure):
    configure(ENVIRONMENT="production", ALLOW_DEV_AUTH_HEADERS=False)
    response = client.get(ME, headers=DEV_HEADERS)
    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated"


def test_a_production_rejects_dev_headers_on_a_staff_route(client, configure):
    configure(ENVIRONMENT="production", ALLOW_DEV_AUTH_HEADERS=False)
    real_app.dependency_overrides[get_db] = lambda: FakeSession()
    try:
        response = client.get("/api/v1/users", headers=DEV_HEADERS)
    finally:
        real_app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 401


def test_a_production_ignores_dev_headers_even_if_flag_is_forced_on(client, configure):
    """Startup validation already forbids this combination; the request
    path must not depend on that being the only safeguard."""
    configure(ENVIRONMENT="production", ALLOW_DEV_AUTH_HEADERS=True)
    assert client.get(ME, headers=DEV_HEADERS).status_code == 401


def test_a_production_rejects_request_with_no_credentials(client, configure):
    configure(ENVIRONMENT="production")
    assert client.get(ME).status_code == 401


# --- B. Production + valid JWT => authenticated ---


def test_b_production_accepts_valid_jwt(client, configure):
    configure(ENVIRONMENT="production")
    response = client.get(ME, headers=bearer(token_for("PROJECT_MANAGER", email="pm@example.com")))
    assert response.status_code == 200
    assert response.json() == {"email": "pm@example.com", "role": "PROJECT_MANAGER"}


def test_b_jwt_identity_wins_over_dev_headers(client, configure):
    configure(ENVIRONMENT="development", ALLOW_DEV_AUTH_HEADERS=True)
    headers = {**DEV_HEADERS, **bearer(token_for("CLIENT_VIEWER", email="viewer@example.com"))}
    response = client.get(ME, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"email": "viewer@example.com", "role": "CLIENT_VIEWER"}


# --- C. Development + dev auth explicitly disabled + dev headers => 401 ---


def test_c_development_with_dev_auth_disabled_rejects_dev_headers(client, configure):
    configure(ENVIRONMENT="development", ALLOW_DEV_AUTH_HEADERS=False)
    assert client.get(ME, headers=DEV_HEADERS).status_code == 401


def test_c_development_without_secret_key_still_rejects_dev_headers(client, configure):
    """The old trigger: a blank SECRET_KEY in development used to switch
    header auth on by itself."""
    configure(ENVIRONMENT="development", ALLOW_DEV_AUTH_HEADERS=False, SECRET_KEY="")
    assert client.get(ME, headers=DEV_HEADERS).status_code == 401


# --- D. Development + dev auth explicitly enabled + dev headers => permitted ---


def test_d_development_with_dev_auth_enabled_permits_dev_headers(client, configure):
    configure(ENVIRONMENT="development", ALLOW_DEV_AUTH_HEADERS=True)
    response = client.get(ME, headers=DEV_HEADERS)
    assert response.status_code == 200
    assert response.json() == {"email": "attacker@example.com", "role": "SUPER_ADMIN"}


def test_d_dev_auth_enabled_still_requires_the_role_header(client, configure):
    configure(ENVIRONMENT="development", ALLOW_DEV_AUTH_HEADERS=True)
    assert client.get(ME).status_code == 401
    assert client.get(ME, headers={"X-Dev-User-Email": "someone@example.com"}).status_code == 401


# --- E. Missing ENVIRONMENT must NOT enable development authentication ---


def test_e_missing_environment_rejects_dev_headers(client, configure, monkeypatch):
    for name in ("ENVIRONMENT", "ALLOW_DEV_AUTH_HEADERS"):
        monkeypatch.delenv(name, raising=False)
    from_empty_env = Settings(_env_file=None)

    configure(
        ENVIRONMENT=from_empty_env.ENVIRONMENT,
        ALLOW_DEV_AUTH_HEADERS=from_empty_env.ALLOW_DEV_AUTH_HEADERS,
    )
    assert settings.ENVIRONMENT == "production"
    assert client.get(ME, headers=DEV_HEADERS).status_code == 401


# --- F. Invalid / expired JWT => rejected ---


def test_f_expired_jwt_is_rejected(client, configure):
    configure(ENVIRONMENT="production")
    expired = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "email": "old@example.com",
            "role": "SUPER_ADMIN",
            "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
        },
        settings.SECRET_KEY,
        algorithm=ALGORITHM,
    )
    response = client.get(ME, headers=bearer(expired))
    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()


def test_f_jwt_signed_with_wrong_key_is_rejected(client, configure):
    configure(ENVIRONMENT="production")
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "role": "SUPER_ADMIN",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        "not-the-server-key-" + "x" * 32,
        algorithm=ALGORITHM,
    )
    assert client.get(ME, headers=bearer(forged)).status_code == 401


@pytest.mark.parametrize("garbage", ["not-a-jwt", "a.b.c", "null"])
def test_f_malformed_jwt_is_rejected(client, configure, garbage):
    configure(ENVIRONMENT="production")
    assert client.get(ME, headers=bearer(garbage)).status_code == 401


def test_f_unsigned_alg_none_jwt_is_rejected(client, configure):
    configure(ENVIRONMENT="production")
    unsigned = jwt.encode({"sub": str(uuid.uuid4()), "role": "SUPER_ADMIN"}, key=None, algorithm="none")
    assert client.get(ME, headers=bearer(unsigned)).status_code == 401


def test_f_invite_token_is_not_a_session(client, configure):
    configure(ENVIRONMENT="production")
    assert client.get(ME, headers=bearer(create_invite_token(str(uuid.uuid4())))).status_code == 401


def test_f_invalid_jwt_never_falls_back_to_dev_headers(client, configure):
    configure(ENVIRONMENT="development", ALLOW_DEV_AUTH_HEADERS=True)
    headers = {**DEV_HEADERS, **bearer("not-a-jwt")}
    assert client.get(ME, headers=headers).status_code == 401


def test_f_token_without_signing_key_never_falls_back_to_dev_headers(client, configure):
    token = token_for("SUPER_ADMIN")
    configure(ENVIRONMENT="development", ALLOW_DEV_AUTH_HEADERS=True, SECRET_KEY="")
    headers = {**DEV_HEADERS, **bearer(token)}
    assert client.get(ME, headers=headers).status_code == 401


# --- G. Existing role authorization and RLS context keep working ---


def test_g_allowed_staff_role_passes_and_sets_rls_bypass(scoped, configure):
    configure(ENVIRONMENT="production")
    http, db = scoped
    response = http.get("/staff-only", headers=bearer(token_for("SUPER_ADMIN")))
    assert response.status_code == 200
    assert response.json() == {"role": "SUPER_ADMIN"}
    assert db.statements == [BYPASS_ON]


@pytest.mark.parametrize("role", ["LEAD_ENGINEER", "CLIENT_ADMIN", "CLIENT_VIEWER"])
def test_g_disallowed_role_gets_403_and_no_rls_bypass(scoped, configure, role):
    configure(ENVIRONMENT="production")
    http, db = scoped
    response = http.get("/staff-only", headers=bearer(token_for(role)))
    assert response.status_code == 403
    assert db.statements == []


def test_g_staff_route_without_credentials_is_401_not_403(scoped, configure):
    configure(ENVIRONMENT="production")
    http, db = scoped
    assert http.get("/staff-only", headers=DEV_HEADERS).status_code == 401
    assert db.statements == []


def test_g_client_is_scoped_to_their_own_org(scoped, configure):
    configure(ENVIRONMENT="production")
    http, db = scoped
    org_id = uuid.uuid4()

    response = http.get("/tenant-scoped", headers=bearer(token_for("CLIENT_ADMIN", org_id=org_id)))

    assert response.status_code == 200
    assert response.json() == {"org_id": str(org_id)}
    assert db.statements == [BYPASS_OFF, f"SET LOCAL app.current_org_id = '{org_id}'"]


def test_g_client_with_no_user_row_is_refused(scoped, configure):
    """Since SEC-06 this is caught at authentication (401), before tenant
    scoping ever runs — a token for a user that doesn't exist is no token."""
    configure(ENVIRONMENT="production")
    http, db = scoped
    orphan = create_access_token(str(uuid.uuid4()), "ghost@example.com", "CLIENT_VIEWER")
    response = http.get("/tenant-scoped", headers=bearer(orphan))
    assert response.status_code == 401
    assert db.statements == []


def test_g_staff_on_tenant_scoped_route_is_unrestricted(scoped, configure):
    configure(ENVIRONMENT="production")
    http, db = scoped
    response = http.get("/tenant-scoped", headers=bearer(token_for("PROJECT_MANAGER")))
    assert response.status_code == 200
    assert response.json() == {"org_id": None}
    assert db.statements == [BYPASS_ON]


def test_g_tenant_scoped_route_rejects_dev_headers_in_production(scoped, configure):
    configure(ENVIRONMENT="production")
    http, db = scoped
    response = http.get("/tenant-scoped", headers={"X-Dev-User-Role": "CLIENT_ADMIN", "X-Dev-User-Email": "a@b.co"})
    assert response.status_code == 401
    assert db.statements == []
