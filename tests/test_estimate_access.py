"""SEC-04 — a saved estimate holds a lead's email, phone and project brief,
so reading one must require either staff sign-in or the short-lived PDF
token issued to the visitor who created it.

Runs against the real app with the DB replaced by FakeSession.
"""

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db.session import get_db
from app.main import app as real_app
from app.services.local_auth import (
    ALGORITHM,
    ESTIMATE_PDF_PURPOSE,
    ESTIMATE_PDF_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    create_estimate_pdf_token,
    create_invite_token,
    decode_estimate_pdf_token,
    decode_invite_token,
)

from tests.conftest import FakeSession, token_for

CREATE = "/api/v1/estimates"
LEAD_EMAIL = "lead@prospect.example"
LEAD_PHONE = "555-010-7788"
BRIEF = "We need a customer portal where our clients can sign in and review their invoices online."
CREATE_BODY = {
    "project_type": "WEB_APPLICATION",
    "features": ["AUTH"],
    "design_tier": "STANDARD",
    "request_type": "NEW",
    "client_email": LEAD_EMAIL,
    "client_phone": LEAD_PHONE,
    "project_description": BRIEF,
}

STAFF_ROLES = ["SUPER_ADMIN", "PROJECT_MANAGER", "LEAD_ENGINEER"]
CLIENT_ROLES = ["CLIENT_ADMIN", "CLIENT_VIEWER"]


def bearer(role: str) -> dict:
    return {"Authorization": f"Bearer {token_for(role)}"}


def detail_url(estimate_id) -> str:
    return f"{CREATE}/{estimate_id}"


def pdf_url(estimate_id) -> str:
    return f"{CREATE}/{estimate_id}/pdf"


@pytest.fixture
def client():
    fake = FakeSession()
    real_app.dependency_overrides[get_db] = lambda: fake
    yield TestClient(real_app)
    real_app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def created(client):
    """An estimate submitted through the public form, as a visitor would."""
    response = client.post(CREATE, json=CREATE_BODY)
    assert response.status_code == 201
    return response.json()


def assert_no_lead_data(response):
    body = response.text
    assert LEAD_EMAIL not in body
    assert LEAD_PHONE not in body
    assert BRIEF not in body


# --- GET /estimates/{id}: staff only ---


def test_anonymous_cannot_read_an_estimate(client, created):
    response = client.get(detail_url(created["id"]))
    assert response.status_code == 401
    assert_no_lead_data(response)


def test_pdf_token_does_not_open_the_json_record(client, created):
    response = client.get(detail_url(created["id"]), headers={"X-Estimate-Token": created["pdf_token"]})
    assert response.status_code == 401
    assert_no_lead_data(response)


@pytest.mark.parametrize("role", CLIENT_ROLES)
def test_client_roles_cannot_read_an_estimate(client, created, role):
    response = client.get(detail_url(created["id"]), headers=bearer(role))
    assert response.status_code == 403
    assert_no_lead_data(response)


@pytest.mark.parametrize("role", STAFF_ROLES)
def test_staff_can_read_an_estimate(client, created, role):
    response = client.get(detail_url(created["id"]), headers=bearer(role))
    assert response.status_code == 200
    assert response.json()["client_email"] == LEAD_EMAIL
    assert "pdf_token" not in response.json()


def test_staff_get_404_for_an_unknown_estimate(client):
    assert client.get(detail_url(uuid.uuid4()), headers=bearer("SUPER_ADMIN")).status_code == 404


# --- GET /estimates/{id}/pdf: staff, or the submitter's token ---


def test_anonymous_cannot_download_the_pdf(client, created):
    response = client.get(pdf_url(created["id"]))
    assert response.status_code == 401
    assert response.headers["content-type"] != "application/pdf"


def test_anonymous_gets_the_same_answer_for_real_and_unknown_ids(client, created):
    real = client.get(pdf_url(created["id"]))
    unknown = client.get(pdf_url(uuid.uuid4()))
    assert real.status_code == unknown.status_code == 401
    assert real.json() == unknown.json()


def test_submitter_can_download_their_pdf_with_the_issued_token(client, created):
    response = client.get(pdf_url(created["id"]), headers={"X-Estimate-Token": created["pdf_token"]})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert "no-store" in response.headers["cache-control"]


def test_token_in_the_query_string_is_not_accepted(client, created):
    """Tokens must not travel in URLs, so a leaked link alone is useless."""
    for param in ("token", "pdf_token", "x_estimate_token", "X-Estimate-Token"):
        assert client.get(pdf_url(created["id"]), params={param: created["pdf_token"]}).status_code == 401


def test_token_for_one_estimate_does_not_open_another(client, created):
    other = client.post(CREATE, json=CREATE_BODY).json()
    assert other["id"] != created["id"]
    response = client.get(pdf_url(other["id"]), headers={"X-Estimate-Token": created["pdf_token"]})
    assert response.status_code == 401


def test_expired_pdf_token_is_rejected(client, created):
    expired = jwt.encode(
        {
            "sub": created["id"],
            "purpose": ESTIMATE_PDF_PURPOSE,
            "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
        },
        settings.SECRET_KEY,
        algorithm=ALGORITHM,
    )
    assert client.get(pdf_url(created["id"]), headers={"X-Estimate-Token": expired}).status_code == 401


def test_pdf_token_without_expiry_is_rejected(client, created):
    eternal = jwt.encode(
        {"sub": created["id"], "purpose": ESTIMATE_PDF_PURPOSE}, settings.SECRET_KEY, algorithm=ALGORITHM
    )
    assert client.get(pdf_url(created["id"]), headers={"X-Estimate-Token": eternal}).status_code == 401


def test_forged_pdf_token_is_rejected(client, created):
    forged = jwt.encode(
        {
            "sub": created["id"],
            "purpose": ESTIMATE_PDF_PURPOSE,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        "not-the-server-key-" + "x" * 32,
        algorithm=ALGORITHM,
    )
    assert client.get(pdf_url(created["id"]), headers={"X-Estimate-Token": forged}).status_code == 401


@pytest.mark.parametrize("garbage", ["nope", "a.b.c", " "])
def test_malformed_pdf_token_is_rejected(client, created, garbage):
    assert client.get(pdf_url(created["id"]), headers={"X-Estimate-Token": garbage}).status_code == 401


def test_other_token_kinds_are_not_pdf_tokens(client, created):
    """A session token or invite token whose subject happens to equal the
    estimate id must still not work as a download pass."""
    session_token = create_access_token(created["id"], "user@example.com", "CLIENT_ADMIN")
    invite_token = create_invite_token(created["id"])
    for token in (session_token, invite_token):
        assert client.get(pdf_url(created["id"]), headers={"X-Estimate-Token": token}).status_code == 401


def test_bad_pdf_token_is_not_rescued_by_valid_staff_login(client, created):
    headers = {**bearer("SUPER_ADMIN"), "X-Estimate-Token": "nope"}
    assert client.get(pdf_url(created["id"]), headers=headers).status_code == 401


@pytest.mark.parametrize("role", STAFF_ROLES)
def test_staff_can_download_the_pdf(client, created, role):
    response = client.get(pdf_url(created["id"]), headers=bearer(role))
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


@pytest.mark.parametrize("role", CLIENT_ROLES)
def test_client_roles_cannot_download_the_pdf(client, created, role):
    assert client.get(pdf_url(created["id"]), headers=bearer(role)).status_code == 403


def test_staff_get_404_for_an_unknown_pdf(client):
    assert client.get(pdf_url(uuid.uuid4()), headers=bearer("SUPER_ADMIN")).status_code == 404


def test_dev_headers_do_not_open_the_pdf_in_production(client, created, configure):
    configure(ENVIRONMENT="production", ALLOW_DEV_AUTH_HEADERS=False)
    response = client.get(pdf_url(created["id"]), headers={"X-Dev-User-Role": "SUPER_ADMIN"})
    assert response.status_code == 401


# --- The list stays staff-only ---


def test_anonymous_cannot_list_estimates(client, created):
    response = client.get(CREATE)
    assert response.status_code == 401
    assert_no_lead_data(response)


# --- The issued token ---


def test_create_issues_a_token_scoped_to_that_estimate_with_a_short_life(created):
    claims = decode_estimate_pdf_token(created["pdf_token"])
    assert claims["sub"] == created["id"]
    assert claims["purpose"] == ESTIMATE_PDF_PURPOSE
    lifetime = datetime.fromtimestamp(claims["exp"], tz=timezone.utc) - datetime.now(timezone.utc)
    assert timedelta(0) < lifetime <= timedelta(minutes=ESTIMATE_PDF_TOKEN_EXPIRE_MINUTES)
    assert set(claims) == {"sub", "purpose", "exp"}  # no contact details inside the token


def test_no_token_is_issued_without_a_signing_key(client, configure):
    configure(ENVIRONMENT="development", SECRET_KEY="")
    body = client.post(CREATE, json=CREATE_BODY).json()
    assert body["pdf_token"] is None
    assert client.get(pdf_url(body["id"])).status_code == 401
    assert client.get(pdf_url(body["id"]), headers={"X-Estimate-Token": "anything"}).status_code == 401


# --- A PDF token is not a session and not an invite ---


def test_pdf_token_cannot_be_used_as_a_session(client, created):
    as_session = {"Authorization": f"Bearer {created['pdf_token']}"}
    assert client.get("/api/v1/auth/me", headers=as_session).status_code == 401
    assert client.get(CREATE, headers=as_session).status_code == 401
    assert client.get(detail_url(created["id"]), headers=as_session).status_code == 401
    assert client.get(pdf_url(created["id"]), headers=as_session).status_code == 401


def test_pdf_token_cannot_be_used_as_an_invite(created):
    with pytest.raises(jwt.PyJWTError):
        decode_invite_token(created["pdf_token"])


def test_signed_token_missing_role_is_rejected_not_a_server_error(client):
    roleless = jwt.encode(
        {"sub": str(uuid.uuid4()), "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        settings.SECRET_KEY,
        algorithm=ALGORITHM,
    )
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {roleless}"}).status_code == 401
