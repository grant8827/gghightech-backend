"""Itemized invoices (numbering, totals, PDF, sending) and project contracts
(draft -> send -> client e-signs, edits, void, visibility).

Real SQL against in-memory SQLite (same approach as test_instant_payments.py);
Stripe and email are replaced so nothing leaves the process.
"""

import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import contracts as contract_routes
from app.api.routes import invoices as invoice_routes
from app.db.session import get_db
from app.main import app as real_app
from app.models.audit_log import AuditLog
from app.models.contract import Contract
from app.models.invoice import Invoice
from app.models.milestone import Milestone
from app.models.organization import Organization
from app.models.project import Project
from app.models.user import User
from app.services.local_auth import create_access_token_for
from app.services.stripe_service import StripeIntegrationError

from tests.conftest import bearer

INVOICES = "/api/v1/invoices"
CONTRACTS = "/api/v1/contracts"
YEAR = date.today().year


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(element, compiler, **kw):
    return "JSON"


class _SqliteSession(Session):
    def execute(self, statement, *args, **kwargs):
        if str(statement).lstrip().upper().startswith("SET LOCAL"):
            return None
        return super().execute(statement, *args, **kwargs)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for model in (Organization, User, Project, Milestone, Invoice, Contract, AuditLog):
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
def outbox(monkeypatch):
    """Replaces Stripe and email; records what would have gone out."""
    box = {"invoice": [], "contract": [], "signed": [], "checkout": []}

    def checkout(invoice_id, amount, customer_email=None, description=None):
        box["checkout"].append((invoice_id, amount, customer_email))
        return "https://checkout.stripe.test/s"

    monkeypatch.setattr(invoice_routes, "create_checkout_session", checkout)
    monkeypatch.setattr(invoice_routes, "send_invoice_email", lambda **kw: box["invoice"].append(kw) or True)
    monkeypatch.setattr(contract_routes, "send_contract_email", lambda **kw: box["contract"].append(kw) or True)
    monkeypatch.setattr(
        contract_routes, "send_contract_signed_email", lambda *a, **kw: box["signed"].append(a) or True
    )
    return box


@pytest.fixture
def project(db) -> Project:
    org = Organization(id=uuid.uuid4(), name="Acme Corp")
    db.add(org)
    db.flush()
    p = Project(id=uuid.uuid4(), org_id=org.id, title="Acme site", slug=uuid.uuid4().hex)
    db.add(p)
    db.commit()
    return p


def as_role(db, role="SUPER_ADMIN", org_id=None, name="Gregory Grant") -> dict:
    user = User(
        id=uuid.uuid4(),
        org_id=org_id or uuid.uuid4(),
        email=f"{role.lower()}-{uuid.uuid4().hex[:6]}@example.com",
        full_name=name,
        role=role,
        password_hash="$2b$04$not-a-real-hash-but-non-empty",
        token_version=0,
    )
    db.add(user)
    db.commit()
    return bearer(create_access_token_for(user))


ITEMS = [
    {"description": "Design", "quantity": 3, "unit_price": 19.99},
    {"description": "Hosting", "quantity": 1, "unit_price": 10},
]


# ------------------------------------------------------------------ invoices


def test_itemized_invoice_totals_number_and_defaults(client, db, project):
    response = client.post(
        INVOICES,
        json={
            "project_id": str(project.id),
            "line_items": ITEMS,
            "tax_rate": 8.25,
            "customer_email": "jane@acme.com",
            "bill_to_name": "Jane Smith",
            "notes": "Thanks!",
        },
        headers=as_role(db),
    )
    assert response.status_code == 201
    body = response.json()
    assert (body["subtotal"], body["tax_amount"], body["amount"]) == (69.97, 5.77, 75.74)
    assert body["invoice_number"] == f"INV-{YEAR}-0001"
    assert body["description"] == "Design + 1 more"
    assert body["issue_date"] == date.today().isoformat()
    assert body["due_date"] == (date.today() + timedelta(days=14)).isoformat()
    assert [i["description"] for i in body["line_items"]] == ["Design", "Hosting"]

    second = client.post(INVOICES, json={"project_id": str(project.id), "line_items": ITEMS}, headers=as_role(db))
    assert second.json()["invoice_number"] == f"INV-{YEAR}-0002"


def test_single_amount_invoices_still_work(client, db, project):
    response = client.post(
        INVOICES, json={"project_id": str(project.id), "amount": 500, "description": "Retainer"}, headers=as_role(db)
    )
    assert response.status_code == 201
    assert response.json()["line_items"] == [{"description": "Retainer", "quantity": 1.0, "unit_price": 500.0}]


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"line_items": [{"description": "Free", "quantity": 1, "unit_price": 0}]},
        {"line_items": ITEMS, "issue_date": "2026-05-10", "due_date": "2026-05-01"},
        {"line_items": [{"description": "", "quantity": 1, "unit_price": 5}]},
        {"line_items": ITEMS, "tax_rate": 101},
    ],
)
def test_bad_invoices_are_rejected(client, db, project, bad):
    response = client.post(INVOICES, json={"project_id": str(project.id), **bad}, headers=as_role(db))
    assert response.status_code == 422


def test_clients_cannot_create_invoices(client, db, project):
    headers = as_role(db, "CLIENT_ADMIN", project.org_id)
    assert client.post(INVOICES, json={"project_id": str(project.id), "line_items": ITEMS}, headers=headers).status_code == 403


def test_client_downloads_the_invoice_pdf(client, db, project):
    invoice_id = client.post(INVOICES, json={"project_id": str(project.id), "line_items": ITEMS}, headers=as_role(db)).json()["id"]
    response = client.get(f"{INVOICES}/{invoice_id}/pdf", headers=as_role(db, "CLIENT_VIEWER", project.org_id))
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    assert f"INV-{YEAR}-0001" in response.headers["content-disposition"]


def test_milestone_style_invoice_without_items_still_prints(client, db, project):
    milestone = Milestone(id=uuid.uuid4(), project_id=project.id, org_id=project.org_id, title="Design")
    db.add(milestone)
    db.flush()
    invoice = Invoice(org_id=project.org_id, project_id=project.id, milestone_id=milestone.id, amount=1200, status="PENDING")
    db.add(invoice)
    db.commit()
    assert invoice.invoice_number == f"INV-{YEAR}-0001"
    response = client.get(f"{INVOICES}/{invoice.id}/pdf", headers=as_role(db, "CLIENT_ADMIN", project.org_id))
    assert response.status_code == 200


def test_send_invoice_emails_pdf_and_pay_link(client, db, project, outbox):
    headers = as_role(db)
    invoice_id = client.post(
        INVOICES,
        json={"project_id": str(project.id), "line_items": ITEMS, "customer_email": "jane@acme.com", "bill_to_name": "Jane"},
        headers=headers,
    ).json()["id"]

    response = client.post(f"{INVOICES}/{invoice_id}/send", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["email_sent"] is True
    assert body["checkout_url"] == "https://checkout.stripe.test/s"
    assert body["invoice"]["sent_at"] is not None

    (mail,) = outbox["invoice"]
    assert mail["to"] == "jane@acme.com"
    assert mail["pay_url"] == "https://checkout.stripe.test/s"
    assert mail["portal_url"].endswith("/portal?tab=payments")
    filename, pdf = mail["pdf"]
    assert filename == f"gghightech-invoice-INV-{YEAR}-0001.pdf"
    assert pdf.startswith(b"%PDF")


def test_send_still_works_without_stripe(client, db, project, outbox, monkeypatch):
    def broken(*a, **kw):
        raise StripeIntegrationError("Stripe is not configured")

    monkeypatch.setattr(invoice_routes, "create_checkout_session", broken)
    headers = as_role(db)
    invoice_id = client.post(
        INVOICES, json={"project_id": str(project.id), "line_items": ITEMS, "customer_email": "j@acme.com"}, headers=headers
    ).json()["id"]
    body = client.post(f"{INVOICES}/{invoice_id}/send", headers=headers).json()
    assert body["email_sent"] is True
    assert body["checkout_url"] is None
    assert outbox["invoice"][0]["pay_url"] is None


def test_send_needs_an_email_and_an_unpaid_invoice(client, db, project, outbox):
    headers = as_role(db)
    no_email = client.post(INVOICES, json={"project_id": str(project.id), "line_items": ITEMS}, headers=headers).json()["id"]
    assert client.post(f"{INVOICES}/{no_email}/send", headers=headers).status_code == 422

    paid = client.post(
        INVOICES, json={"project_id": str(project.id), "line_items": ITEMS, "customer_email": "j@acme.com"}, headers=headers
    ).json()["id"]
    client.patch(f"{INVOICES}/{paid}/mark-paid", headers=headers)
    assert client.post(f"{INVOICES}/{paid}/send", headers=headers).status_code == 409
    assert client.post(f"{INVOICES}/{paid}/send", headers=as_role(db, "CLIENT_ADMIN", project.org_id)).status_code == 403


# ----------------------------------------------------------------- contracts

CONTRACT = {
    "title": "Website Design & Development Agreement",
    "client_name": "Jane Smith",
    "client_email": "jane@acme.com",
    "scope": "Design and build a marketing website with an online store.",
    "deliverables": ["Sitemap", "  ", "Visual design", "Responsive website"],
    "timeline": "8 weeks from signing.",
    "payment_schedule": [
        {"label": "Deposit", "amount": 2000, "due": "On signing"},
        {"label": "Launch", "amount": 4000.5, "due": "On launch"},
    ],
    "total_value": 1,
    "payment_terms": "Due within 14 days.",
    "terms": "1. Changes are quoted separately.\n\n2. Ownership transfers on final payment.",
}


def create_contract(client, db, project, **overrides) -> dict:
    response = client.post(CONTRACTS, json={"project_id": str(project.id), **CONTRACT, **overrides}, headers=as_role(db))
    assert response.status_code == 201, response.text
    return response.json()


def test_create_contract_draft(client, db, project):
    body = create_contract(client, db, project)
    assert body["status"] == "DRAFT"
    assert body["contract_number"] == f"CTR-{YEAR}-0001"
    assert body["total_value"] == 6000.5  # the schedule's sum, not the fallback
    assert body["deliverables"] == ["Sitemap", "Visual design", "Responsive website"]
    assert body["created_by"] == "Gregory Grant"


def test_value_without_schedule_uses_total_value(client, db, project):
    body = create_contract(client, db, project, payment_schedule=[], total_value=2500)
    assert body["total_value"] == 2500


def test_clients_never_see_drafts(client, db, project):
    contract_id = create_contract(client, db, project)["id"]
    headers = as_role(db, "CLIENT_ADMIN", project.org_id)
    assert client.get(CONTRACTS, headers=headers).json() == []
    assert client.get(f"{CONTRACTS}/{contract_id}", headers=headers).status_code == 404
    assert client.get(f"{CONTRACTS}/{contract_id}/pdf", headers=headers).status_code == 404


def test_full_flow_send_then_client_signs(client, db, project, outbox):
    staff = as_role(db)
    contract_id = create_contract(client, db, project)["id"]

    sent = client.post(f"{CONTRACTS}/{contract_id}/send", headers=staff)
    assert sent.status_code == 200
    assert sent.json()["contract"]["status"] == "SENT"
    assert sent.json()["contract"]["provider_signer_name"] == "Gregory Grant"
    (mail,) = outbox["contract"]
    assert mail["to"] == "jane@acme.com"
    assert mail["portal_url"].endswith("/portal?tab=contracts")
    assert mail["pdf"][1].startswith(b"%PDF")

    viewer = as_role(db, "CLIENT_VIEWER", project.org_id)
    assert [c["id"] for c in client.get(CONTRACTS, headers=viewer).json()] == [contract_id]
    assert client.post(f"{CONTRACTS}/{contract_id}/sign", json={"signer_name": "Jane", "agree": True}, headers=viewer).status_code == 403

    admin = as_role(db, "CLIENT_ADMIN", project.org_id)
    assert client.post(f"{CONTRACTS}/{contract_id}/sign", json={"signer_name": "Jane", "agree": False}, headers=admin).status_code == 422

    signed = client.post(f"{CONTRACTS}/{contract_id}/sign", json={"signer_name": " Jane Smith ", "agree": True}, headers=admin)
    assert signed.status_code == 200
    body = signed.json()
    assert body["status"] == "SIGNED"
    assert body["signer_name"] == "Jane Smith"
    assert len(body["signed_content_hash"]) == 64
    assert db.get(Contract, uuid.UUID(contract_id)).signer_ip
    assert outbox["signed"]  # signed copy emailed

    assert client.post(f"{CONTRACTS}/{contract_id}/sign", json={"signer_name": "Jane", "agree": True}, headers=admin).status_code == 409
    assert client.patch(f"{CONTRACTS}/{contract_id}", json={"title": "Changed title"}, headers=staff).status_code == 409
    pdf = client.get(f"{CONTRACTS}/{contract_id}/pdf", headers=admin)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


def test_other_orgs_cannot_see_or_sign(client, db, project, outbox):
    staff = as_role(db)
    contract_id = create_contract(client, db, project)["id"]
    client.post(f"{CONTRACTS}/{contract_id}/send", headers=staff)
    outsider = as_role(db, "CLIENT_ADMIN", uuid.uuid4())
    assert client.get(f"{CONTRACTS}/{contract_id}", headers=outsider).status_code == 404
    assert client.post(f"{CONTRACTS}/{contract_id}/sign", json={"signer_name": "X Y", "agree": True}, headers=outsider).status_code == 404


def test_editing_a_sent_contract_returns_it_to_draft(client, db, project, outbox):
    staff = as_role(db)
    contract_id = create_contract(client, db, project)["id"]
    client.post(f"{CONTRACTS}/{contract_id}/send", headers=staff)
    edited = client.patch(
        f"{CONTRACTS}/{contract_id}",
        json={"payment_schedule": [{"label": "All", "amount": 7000, "due": "On signing"}]},
        headers=staff,
    ).json()
    assert edited["status"] == "DRAFT"
    assert edited["total_value"] == 7000
    admin = as_role(db, "CLIENT_ADMIN", project.org_id)
    assert client.post(f"{CONTRACTS}/{contract_id}/sign", json={"signer_name": "Jane", "agree": True}, headers=admin).status_code == 404


def test_send_needs_client_email(client, db, project, outbox):
    contract_id = create_contract(client, db, project, client_email=None)["id"]
    assert client.post(f"{CONTRACTS}/{contract_id}/send", headers=as_role(db)).status_code == 422


def test_void_and_delete_rules(client, db, project, outbox):
    staff = as_role(db)
    draft = create_contract(client, db, project)["id"]
    assert client.delete(f"{CONTRACTS}/{draft}", headers=staff).status_code == 204

    sent = create_contract(client, db, project)["id"]
    client.post(f"{CONTRACTS}/{sent}/send", headers=staff)
    assert client.delete(f"{CONTRACTS}/{sent}", headers=staff).status_code == 409
    voided = client.post(f"{CONTRACTS}/{sent}/void", headers=staff)
    assert voided.json()["status"] == "VOID"
    assert client.post(f"{CONTRACTS}/{sent}/void", headers=staff).status_code == 409
    admin = as_role(db, "CLIENT_ADMIN", project.org_id)
    assert client.post(f"{CONTRACTS}/{sent}/sign", json={"signer_name": "Jane", "agree": True}, headers=admin).status_code == 409


def test_defaults_are_staff_only(client, db):
    response = client.get(f"{CONTRACTS}/defaults", headers=as_role(db))
    assert response.status_code == 200
    assert "Ownership" in response.json()["terms"]
    assert client.get(f"{CONTRACTS}/defaults", headers=as_role(db, "CLIENT_ADMIN")).status_code == 403


@pytest.mark.parametrize("role", ["CLIENT_ADMIN", "CLIENT_VIEWER"])
def test_clients_cannot_create_edit_send_or_void(client, db, project, outbox, role):
    contract_id = create_contract(client, db, project)["id"]
    headers = as_role(db, role, project.org_id)
    assert client.post(CONTRACTS, json={"project_id": str(project.id), **CONTRACT}, headers=headers).status_code == 403
    assert client.patch(f"{CONTRACTS}/{contract_id}", json={"title": "Mine now"}, headers=headers).status_code == 403
    assert client.post(f"{CONTRACTS}/{contract_id}/send", headers=headers).status_code == 403
    assert client.post(f"{CONTRACTS}/{contract_id}/void", headers=headers).status_code == 403
