"""Receipts — a PDF for every PAID invoice, whether the client paid online
(Stripe webhook records CARD) or staff recorded a check / wire / cash
payment (PATCH /invoices/{id}/mark-paid).

Real SQL against in-memory SQLite (same approach as test_instant_payments.py).
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

from app.core.config import settings
from app.db.session import get_db
from app.main import app as real_app
from app.models.audit_log import AuditLog
from app.models.invoice import Invoice
from app.models.milestone import Milestone
from app.models.organization import Organization
from app.models.project import Project
from app.models.user import User
from app.services.local_auth import create_access_token_for
from app.services.pdf import receipt_number

from tests.conftest import bearer

INVOICES = "/api/v1/invoices"


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
    for model in (Organization, User, Project, Milestone, Invoice, AuditLog):
        model.__table__.create(engine)
    session = sessionmaker(bind=engine, class_=_SqliteSession, expire_on_commit=False)()
    real_app.dependency_overrides[get_db] = lambda: session
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def client(db):
    return TestClient(real_app)


def make_org(db, name="Acme") -> Organization:
    org = Organization(id=uuid.uuid4(), name=name)
    db.add(org)
    db.commit()
    return org


def make_invoice(db, org: Organization, amount=500.0, description="Retainer — November") -> Invoice:
    project = Project(id=uuid.uuid4(), org_id=org.id, title=f"{org.name} site", slug=uuid.uuid4().hex)
    db.add(project)
    db.flush()
    invoice = Invoice(
        id=uuid.uuid4(),
        org_id=org.id,
        project_id=project.id,
        description=description,
        amount=amount,
        status="PENDING",
    )
    db.add(invoice)
    db.commit()
    return invoice


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


def test_no_receipt_until_the_invoice_is_paid(client, db):
    org = make_org(db)
    invoice = make_invoice(db, org)
    response = client.get(f"{INVOICES}/{invoice.id}/receipt", headers=as_role(db, "CLIENT_ADMIN", org.id))
    assert response.status_code == 409


def test_staff_record_a_check_and_the_client_downloads_the_receipt(client, db):
    org = make_org(db)
    invoice = make_invoice(db, org)

    marked = client.patch(
        f"{INVOICES}/{invoice.id}/mark-paid",
        json={"payment_method": "CHECK", "payment_reference": " #1042 "},
        headers=as_role(db),
    )
    assert marked.status_code == 200
    body = marked.json()
    assert (body["status"], body["payment_method"], body["payment_reference"]) == ("PAID", "CHECK", "#1042")

    audit = db.query(AuditLog).filter(AuditLog.action == "invoice.mark_paid").one()
    assert audit.event_metadata["payment_method"] == "CHECK"

    for role in ("CLIENT_ADMIN", "CLIENT_VIEWER"):
        receipt = client.get(f"{INVOICES}/{invoice.id}/receipt", headers=as_role(db, role, org.id))
        assert receipt.status_code == 200
        assert receipt.headers["content-type"] == "application/pdf"
        assert receipt.content.startswith(b"%PDF")
        assert f"gghightech-receipt-{invoice.id}.pdf" in receipt.headers["content-disposition"]


def test_mark_paid_without_a_body_still_works(client, db):
    """The admin Invoicing tab's existing call sends no body."""
    invoice = make_invoice(db, make_org(db))
    marked = client.patch(f"{INVOICES}/{invoice.id}/mark-paid", headers=as_role(db))
    assert marked.status_code == 200
    assert marked.json()["payment_method"] == "OTHER"
    assert marked.json()["payment_reference"] is None


@pytest.mark.parametrize(
    "bad",
    [{"payment_method": "CARD"}, {"payment_method": "BITCOIN"}, {"payment_reference": "x" * 121}],
)
def test_mark_paid_rejects_bad_methods(client, db, bad):
    invoice = make_invoice(db, make_org(db))
    response = client.patch(f"{INVOICES}/{invoice.id}/mark-paid", json=bad, headers=as_role(db))
    assert response.status_code == 422
    assert db.get(Invoice, invoice.id).status == "PENDING"


def test_another_org_cannot_see_the_receipt(client, db):
    org = make_org(db)
    invoice = make_invoice(db, org)
    client.patch(f"{INVOICES}/{invoice.id}/mark-paid", headers=as_role(db))

    outsider = as_role(db, "CLIENT_ADMIN", make_org(db, "Other").id)
    assert client.get(f"{INVOICES}/{invoice.id}/receipt", headers=outsider).status_code == 404
    assert client.get(f"{INVOICES}/{invoice.id}/pdf", headers=outsider).status_code == 404


def test_anonymous_cannot_download_receipt_or_invoice(client, db):
    invoice = make_invoice(db, make_org(db))
    assert client.get(f"{INVOICES}/{invoice.id}/receipt").status_code == 401
    assert client.get(f"{INVOICES}/{invoice.id}/pdf").status_code == 401


def test_stripe_payment_records_card_and_payment_intent(client, db, monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    org = make_org(db)
    invoice = make_invoice(db, org)

    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "mode": "payment",
                "payment_status": "paid",
                "payment_intent": "pi_3Abc",
                "metadata": {"invoice_id": str(invoice.id)},
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

    stored = db.get(Invoice, invoice.id)
    assert (stored.status, stored.payment_method, stored.payment_reference) == ("PAID", "CARD", "pi_3Abc")
    receipt = client.get(f"{INVOICES}/{invoice.id}/receipt", headers=as_role(db, "CLIENT_ADMIN", org.id))
    assert receipt.status_code == 200


def test_receipt_number_is_stable_and_short():
    invoice_id = uuid.UUID("74fc307c-9e78-4726-ac7f-42c82261228b")
    assert receipt_number(invoice_id) == "R-74FC307C"
