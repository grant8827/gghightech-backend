"""Email Server pass-through: SUPER_ADMIN only, off until configured, forwards
the service token and the admin's identity, maps errors, audits changes
without ever recording an SMTP password."""

import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.routes import email_server
from app.db.session import get_db
from app.main import app as real_app
from app.models.audit_log import AuditLog

from tests.conftest import FakeSession, bearer, token_for

BASE = "/api/v1/email-server"
SERVICE = "http://email-service.internal:8080"
DOMAIN_ID = str(uuid.uuid4())
ACCOUNT_ID = str(uuid.uuid4())


class FakeService:
    """Stands in for httpx.request to the Java service."""

    def __init__(self):
        self.calls = []
        self.status = 200
        self.payload = {}
        self.raises = None

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.raises:
            raise self.raises
        return httpx.Response(self.status, json=self.payload, request=httpx.Request(method, url))


@pytest.fixture
def service(monkeypatch, configure):
    configure(EMAIL_SERVICE_URL=SERVICE, EMAIL_SERVICE_TOKEN="svc-token")
    fake = FakeService()
    monkeypatch.setattr(email_server.httpx, "request", fake)
    return fake


@pytest.fixture
def db():
    session = FakeSession()
    real_app.dependency_overrides[get_db] = lambda: session
    return session


@pytest.fixture
def client(db):
    return TestClient(real_app)


def admin():
    return bearer(token_for("SUPER_ADMIN", email="owner@gghightech.test"))


@pytest.mark.parametrize("role", ["PROJECT_MANAGER", "LEAD_ENGINEER", "CLIENT_ADMIN", "CLIENT_VIEWER"])
def test_only_super_admins_can_use_it(client, service, role):
    assert client.get(f"{BASE}/status", headers=bearer(token_for(role))).status_code == 403
    assert client.get(f"{BASE}/domains", headers=bearer(token_for(role))).status_code == 403
    assert service.calls == []


def test_anonymous_callers_are_refused(client, service):
    assert client.get(f"{BASE}/domains").status_code == 401


def test_off_until_configured(client, configure):
    configure(EMAIL_SERVICE_URL="", EMAIL_SERVICE_TOKEN="")
    assert client.get(f"{BASE}/status", headers=admin()).json() == {"enabled": False}
    assert client.get(f"{BASE}/domains", headers=admin()).status_code == 503


def test_status_on_when_configured(client, service):
    assert client.get(f"{BASE}/status", headers=admin()).json() == {"enabled": True}


def test_forwards_the_service_token_and_the_admins_email(client, service):
    service.payload = [{"id": DOMAIN_ID, "name": "shop.test"}]
    response = client.get(f"{BASE}/domains", headers=admin())

    assert response.status_code == 200
    assert response.json() == service.payload
    assert response.headers["cache-control"] == "no-store"
    (call,) = service.calls
    assert call["url"] == f"{SERVICE}/api/v1/email/domains"
    assert call["headers"] == {"X-Service-Token": "svc-token", "X-Actor-Email": "owner@gghightech.test"}


def test_non_uuid_ids_never_reach_the_service(client, service):
    assert client.get(f"{BASE}/domains/..%2F..%2Factuator", headers=admin()).status_code in (404, 422)
    assert client.post(f"{BASE}/accounts/not-a-uuid/revoke", headers=admin()).status_code == 422
    assert service.calls == []


def test_service_errors_come_back_with_their_message(client, service):
    service.status = 409
    service.payload = {"status": 409, "title": "Conflict",
                       "detail": "Verify the domain's DNS records before creating SMTP credentials"}
    response = client.post(f"{BASE}/domains/{DOMAIN_ID}/accounts", json={"local_part": "app"}, headers=admin())
    assert response.status_code == 409
    assert "Verify the domain" in response.json()["detail"]


def test_a_rejected_service_token_is_a_502_not_a_logout(client, service):
    service.status = 401
    service.payload = {}
    assert client.get(f"{BASE}/domains", headers=admin()).status_code == 502


def test_unreachable_service_is_a_502(client, service):
    service.raises = httpx.ConnectError("refused")
    assert client.get(f"{BASE}/overview", headers=admin()).status_code == 502


def test_creating_a_credential_audits_the_address_but_never_the_password(client, service, db):
    service.status = 201
    service.payload = {
        "account": {"id": ACCOUNT_ID, "address": "app@shop.test"},
        "smtpHost": "mail.gghightech.test", "smtpPort": 587, "username": "app@shop.test",
        "password": "s3cret-shown-once",
    }
    response = client.post(f"{BASE}/domains/{DOMAIN_ID}/accounts",
                           json={"local_part": "app", "description": "HRBooks360"}, headers=admin())

    assert response.status_code == 201
    assert response.json()["password"] == "s3cret-shown-once"
    assert service.calls[0]["json"] == {"localPart": "app", "description": "HRBooks360"}
    (audit,) = [o for o in db.added if isinstance(o, AuditLog)]
    assert audit.action == "email_server.smtp_account_create"
    assert audit.event_metadata == {"address": "app@shop.test"}
    assert "s3cret" not in repr(audit.event_metadata)


def test_domain_add_and_revoke_are_audited(client, service, db):
    service.status = 201
    service.payload = {"id": DOMAIN_ID, "name": "shop.test"}
    client.post(f"{BASE}/domains", json={"name": "shop.test"}, headers=admin())
    service.status = 200
    service.payload = {"id": ACCOUNT_ID, "address": "app@shop.test", "status": "REVOKED"}
    client.post(f"{BASE}/accounts/{ACCOUNT_ID}/revoke", headers=admin())

    actions = [o.action for o in db.added if isinstance(o, AuditLog)]
    assert actions == ["email_server.domain_add", "email_server.smtp_account_revoke"]


def test_message_limit_is_clamped(client, service):
    service.payload = []
    client.get(f"{BASE}/messages?limit=5000&domain=shop.test", headers=admin())
    assert service.calls[0]["params"] == {"domain": "shop.test", "limit": 200}
