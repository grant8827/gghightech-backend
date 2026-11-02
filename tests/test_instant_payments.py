"""Instant payments — staff send a Stripe link to an email for an amount,
and the Stripe webhook marks it paid.

Real SQL against in-memory SQLite (same approach as test_reviews.py);
Stripe and Mailgun are replaced so nothing leaves the process.
"""

import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import instant_payments as routes
from app.core.config import settings
from app.db.session import get_db
from app.main import app as real_app
from app.models.audit_log import AuditLog
from app.models.instant_payment import InstantPayment
from app.models.user import User
from app.services.local_auth import create_access_token_for
from app.services.stripe_service import StripeIntegrationError

from tests.conftest import bearer

PAYMENTS = "/api/v1/instant-payments"
VALID = {"customer_email": "pay@client.example", "amount": 149.5, "description": "Logo refresh"}


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
    for model in (User, InstantPayment, AuditLog):
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
def stripe_calls(monkeypatch):
    """Replaces Stripe and Mailgun; records what would have been sent."""
    calls = {"checkout": [], "email": []}

    def fake_checkout(payment_id, amount, customer_email, description=None):
        calls["checkout"].append((payment_id, amount, customer_email, description))
        return f"https://checkout.stripe.test/{len(calls['checkout'])}"

    def fake_email(to, url, description):
        calls["email"].append((to, url, description))
        return True

    monkeypatch.setattr(routes, "create_instant_checkout_session", fake_checkout)
    monkeypatch.setattr(routes, "send_payment_link_email", fake_email)
    return calls


def as_role(db, role="SUPER_ADMIN") -> dict:
    user = User(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        email=f"{role.lower()}-{uuid.uuid4().hex[:6]}@gghightech.example",
        full_name="Someone",
        role=role,
        password_hash="$2b$04$not-a-real-hash-but-non-empty",
        token_version=0,
    )
    db.add(user)
    db.commit()
    return bearer(create_access_token_for(user))


def test_staff_send_a_link_to_an_email_for_an_amount(client, db, stripe_calls):
    response = client.post(PAYMENTS, json=VALID, headers=as_role(db))

    assert response.status_code == 201
    body = response.json()
    assert body["checkout_url"] == "https://checkout.stripe.test/1"
    assert body["email_sent"] is True
    assert body["payment"]["status"] == "PENDING"
    assert body["payment"]["amount"] == 149.5

    (payment_id, amount, email, description), = stripe_calls["checkout"]
    assert (amount, email, description) == (149.5, "pay@client.example", "Logo refresh")
    assert stripe_calls["email"] == [("pay@client.example", "https://checkout.stripe.test/1", "Logo refresh")]

    stored = db.get(InstantPayment, payment_id)
    assert stored.checkout_url == "https://checkout.stripe.test/1"


def test_description_is_optional(client, db, stripe_calls):
    response = client.post(PAYMENTS, json={"customer_email": "a@b.example", "amount": 20}, headers=as_role(db))
    assert response.status_code == 201
    assert stripe_calls["email"][0][2] == "GG HighTech payment"


@pytest.mark.parametrize("bad", [{"customer_email": "not-an-email"}, {"amount": 0}, {"amount": -5}])
def test_invalid_input_is_rejected(client, db, stripe_calls, bad):
    response = client.post(PAYMENTS, json={**VALID, **bad}, headers=as_role(db))
    assert response.status_code == 422
    assert stripe_calls["checkout"] == []


@pytest.mark.parametrize("role", ["CLIENT_ADMIN", "CLIENT_VIEWER"])
def test_clients_cannot_create_or_list(client, db, stripe_calls, role):
    headers = as_role(db, role)
    assert client.post(PAYMENTS, json=VALID, headers=headers).status_code == 403
    assert client.get(PAYMENTS, headers=headers).status_code == 403


def test_payment_is_kept_when_stripe_fails(client, db, monkeypatch):
    def broken(*args, **kwargs):
        raise StripeIntegrationError("Stripe is not configured")

    monkeypatch.setattr(routes, "create_instant_checkout_session", broken)
    headers = as_role(db)
    response = client.post(PAYMENTS, json=VALID, headers=headers)

    assert response.status_code == 502
    listed = client.get(PAYMENTS, headers=headers).json()
    assert [p["customer_email"] for p in listed] == ["pay@client.example"]


def test_resend_makes_a_fresh_link_but_not_once_paid(client, db, stripe_calls):
    headers = as_role(db)
    payment_id = client.post(PAYMENTS, json=VALID, headers=headers).json()["payment"]["id"]

    resent = client.post(f"{PAYMENTS}/{payment_id}/resend", headers=headers)
    assert resent.status_code == 200
    assert resent.json()["checkout_url"] == "https://checkout.stripe.test/2"

    db.get(InstantPayment, uuid.UUID(payment_id)).status = "PAID"
    db.commit()
    assert client.post(f"{PAYMENTS}/{payment_id}/resend", headers=headers).status_code == 409


def test_stripe_webhook_marks_it_paid(client, db, stripe_calls, monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    payment_id = client.post(PAYMENTS, json=VALID, headers=as_role(db)).json()["payment"]["id"]

    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "mode": "payment",
                "payment_status": "paid",
                "metadata": {"instant_payment_id": payment_id},
            }
        },
    }
    payload = json.dumps(event).encode()
    timestamp = int(time.time())
    signature = hmac.new(b"whsec_test", f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    response = client.post(
        "/api/v1/stripe/webhook",
        content=payload,
        headers={"Stripe-Signature": f"t={timestamp},v1={signature}", "Content-Type": "application/json"},
    )

    assert response.status_code == 200
    stored = db.get(InstantPayment, uuid.UUID(payment_id))
    assert stored.status == "PAID"
    assert stored.paid_at is not None
