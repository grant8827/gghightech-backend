"""Fixed-length plans ("5 monthly payments"): manual invoicing stops at the
length, and a Stripe subscription is told not to renew once its last
installment is paid.

Real SQL against in-memory SQLite (same approach as test_reviews.py);
Stripe and email are replaced so nothing leaves the process.
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

from app.api.routes import stripe_webhooks
from app.api.routes import subscriptions as routes
from app.core.config import settings
from app.db.session import get_db
from app.main import app as real_app
from app.models.audit_log import AuditLog
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.subscription_plan import SubscriptionPlan
from app.models.user import User
from app.services.local_auth import create_access_token_for
from app.services.stripe_service import StripeIntegrationError

from tests.conftest import bearer

PLANS = "/api/v1/subscriptions"
SUB_ID = "sub_test123"


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
    for model in (Organization, User, SubscriptionPlan, Invoice, AuditLog):
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
def stripe(monkeypatch):
    """Replaces every Stripe and email call these routes make."""
    calls = {"checkout_names": [], "ended": [], "canceled": [], "fail_end": False}

    def fake_subscription_checkout(plan_id, amount, name, interval="month", customer_email=None):
        calls["checkout_names"].append(name)
        return "https://checkout.stripe.test/sub"

    def fake_end(subscription_id):
        if calls["fail_end"]:
            raise StripeIntegrationError("Could not reach Stripe")
        calls["ended"].append(subscription_id)

    monkeypatch.setattr(routes, "create_subscription_checkout_session", fake_subscription_checkout)
    monkeypatch.setattr(routes, "create_checkout_session", lambda *a, **k: "https://checkout.stripe.test/inv")
    monkeypatch.setattr(routes, "send_payment_link_email", lambda *a, **k: True)
    monkeypatch.setattr(routes, "cancel_subscription_now", lambda sub: calls["canceled"].append(sub))
    monkeypatch.setattr(stripe_webhooks, "end_subscription_after_current_period", fake_end)
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    return calls


@pytest.fixture
def staff(db) -> dict:
    user = User(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        email=f"admin-{uuid.uuid4().hex[:6]}@gghightech.example",
        full_name="Admin",
        role="SUPER_ADMIN",
        password_hash="$2b$04$not-a-real-hash-but-non-empty",
        token_version=0,
    )
    db.add(user)
    db.commit()
    return bearer(create_access_token_for(user))


@pytest.fixture
def org(db) -> Organization:
    org = Organization(id=uuid.uuid4(), name="Client Co", domain="client.example", plan_tier="STANDARD")
    db.add(org)
    db.commit()
    return org


def create_plan(client, staff, org, **fields) -> dict:
    body = {
        "org_id": str(org.id),
        "project_id": str(uuid.uuid4()),
        "name": "Retainer",
        "amount": 500,
        "billing_frequency": "MONTHLY",
        "is_subscription": True,
        "customer_email": "client@client.example",
        **fields,
    }
    response = client.post(PLANS, json=body, headers=staff)
    assert response.status_code == 201, response.text
    return response.json()


def send_event(client, event_type: str, obj: dict):
    payload = json.dumps({"type": event_type, "data": {"object": obj}}).encode()
    timestamp = int(time.time())
    signature = hmac.new(b"whsec_test", f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return client.post(
        "/api/v1/stripe/webhook",
        content=payload,
        headers={"Stripe-Signature": f"t={timestamp},v1={signature}", "Content-Type": "application/json"},
    )


def paid_invoice(plan_id: str, invoice_id: str, new_api: bool = True) -> dict:
    """An invoice.paid object in either Stripe API shape."""
    details = {"metadata": {"plan_id": plan_id}, "subscription": SUB_ID}
    invoice = {"object": "invoice", "id": invoice_id, "amount_paid": 50000}
    if new_api:
        invoice["parent"] = {"subscription_details": details}
    else:
        invoice["subscription_details"] = {"metadata": details["metadata"]}
        invoice["subscription"] = SUB_ID
    return invoice


def plan_row(db, plan: dict) -> SubscriptionPlan:
    row = db.get(SubscriptionPlan, uuid.UUID(plan["id"]))
    db.refresh(row)
    return row


# --- Creating ---


def test_a_plan_can_have_a_length(client, staff, org, stripe):
    plan = create_plan(client, staff, org, total_cycles=5)
    assert plan["total_cycles"] == 5
    assert plan["cycles_billed"] == 0


def test_no_length_means_ongoing(client, staff, org, stripe):
    assert create_plan(client, staff, org)["total_cycles"] is None


@pytest.mark.parametrize("bad", [0, -1, 121])
def test_length_out_of_range_is_rejected(client, staff, org, bad):
    body = {"org_id": str(org.id), "name": "X", "amount": 10, "is_subscription": True, "total_cycles": bad}
    assert client.post(PLANS, json=body, headers=staff).status_code == 422


def test_the_customer_sees_the_length_on_checkout(client, staff, org, stripe):
    plan = create_plan(client, staff, org, total_cycles=5)
    client.post(f"{PLANS}/{plan['id']}/checkout", headers=staff)
    assert stripe["checkout_names"] == ["Retainer (5 monthly payments)"]

    annual = create_plan(client, staff, org, name="Domain", billing_frequency="ANNUAL", total_cycles=2)
    client.post(f"{PLANS}/{annual['id']}/checkout", headers=staff)
    assert stripe["checkout_names"][-1] == "Domain (2 annual payments)"


# --- Manual monthly plans ---


def test_manual_plan_stops_invoicing_at_its_length(client, staff, org, stripe, db):
    plan = create_plan(client, staff, org, is_subscription=False, billing_day=1, total_cycles=2)
    generate = f"{PLANS}/{plan['id']}/generate-invoice"

    first = client.post(generate, headers=staff)
    second = client.post(generate, headers=staff)
    assert first.json()["description"].endswith("payment 1 of 2")
    assert second.json()["description"].endswith("payment 2 of 2")

    assert plan_row(db, plan).status == "COMPLETED"
    assert client.post(generate, headers=staff).status_code == 409


# --- Stripe subscriptions ---


@pytest.mark.parametrize("new_api", [True, False])
def test_subscription_ends_after_the_last_paid_installment(client, staff, org, stripe, db, new_api):
    plan = create_plan(client, staff, org, total_cycles=3)

    for n in (1, 2):
        assert send_event(client, "invoice.paid", paid_invoice(plan["id"], f"in_{n}", new_api)).status_code == 200
    row = plan_row(db, plan)
    assert (row.cycles_billed, row.status, row.stripe_subscription_id) == (2, "ACTIVE", SUB_ID)
    assert stripe["ended"] == []

    send_event(client, "invoice.paid", paid_invoice(plan["id"], "in_3", new_api))
    row = plan_row(db, plan)
    assert (row.cycles_billed, row.status) == (3, "COMPLETED")
    assert stripe["ended"] == [SUB_ID]


def test_a_redelivered_payment_is_counted_once(client, staff, org, stripe, db):
    plan = create_plan(client, staff, org, total_cycles=5)
    for _ in range(3):
        send_event(client, "invoice.paid", paid_invoice(plan["id"], "in_same"))
    assert plan_row(db, plan).cycles_billed == 1


def test_if_stripe_cant_be_reached_the_event_is_retried_not_lost(client, staff, org, stripe, db):
    plan = create_plan(client, staff, org, total_cycles=1)
    stripe["fail_end"] = True

    assert send_event(client, "invoice.paid", paid_invoice(plan["id"], "in_1")).status_code == 502
    db.rollback()
    assert plan_row(db, plan).cycles_billed == 0

    stripe["fail_end"] = False  # Stripe retries the event later
    assert send_event(client, "invoice.paid", paid_invoice(plan["id"], "in_1")).status_code == 200
    row = plan_row(db, plan)
    assert (row.cycles_billed, row.status) == (1, "COMPLETED")


def test_the_final_cancellation_keeps_the_plan_completed(client, staff, org, stripe, db):
    plan = create_plan(client, staff, org, total_cycles=1)
    send_event(client, "invoice.paid", paid_invoice(plan["id"], "in_1"))
    send_event(
        client,
        "customer.subscription.deleted",
        {"object": "subscription", "id": SUB_ID, "status": "canceled", "metadata": {"plan_id": plan["id"]}},
    )
    assert plan_row(db, plan).status == "COMPLETED"


def test_ongoing_plans_never_end_on_their_own(client, staff, org, stripe, db):
    plan = create_plan(client, staff, org)
    for n in range(1, 8):
        send_event(client, "invoice.paid", paid_invoice(plan["id"], f"in_{n}"))
    row = plan_row(db, plan)
    assert (row.cycles_billed, row.status) == (7, "ACTIVE")
    assert stripe["ended"] == []


def test_a_failed_payment_pauses_and_the_retry_resumes(client, staff, org, stripe, db):
    plan = create_plan(client, staff, org, total_cycles=5)
    failed = {**paid_invoice(plan["id"], "in_1"), "amount_paid": 0}
    send_event(client, "invoice.payment_failed", failed)
    assert plan_row(db, plan).status == "PAUSED"

    send_event(client, "invoice.paid", paid_invoice(plan["id"], "in_1"))
    row = plan_row(db, plan)
    assert (row.status, row.cycles_billed) == ("ACTIVE", 1)


def test_canceling_a_plan_cancels_it_on_stripe(client, staff, org, stripe, db):
    plan = create_plan(client, staff, org, total_cycles=5)
    send_event(client, "invoice.paid", paid_invoice(plan["id"], "in_1"))

    response = client.patch(f"{PLANS}/{plan['id']}", json={"status": "CANCELED"}, headers=staff)
    assert response.status_code == 200
    assert stripe["canceled"] == [SUB_ID]
