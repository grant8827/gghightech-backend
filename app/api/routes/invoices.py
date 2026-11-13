from typing import Optional

import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.invoice import Invoice
from app.core.config import settings
from app.models.milestone import Milestone
from app.models.organization import Organization
from app.models.project import Project
from app.schemas.invoice import InvoiceCreate, InvoiceMarkPaid, InvoiceOut, InvoiceSent, invoice_totals
from app.services.audit import record_audit_event
from app.services.auth import (
    AuthenticatedUser,
    commit_with_rls_refresh,
    get_current_user,
    get_current_user_org_id,
    require_roles,
)
from app.services.documents import InvoiceDoc, build_invoice_document
from app.services.email import send_invoice_email, send_payment_link_email
from app.services.pdf import build_receipt_pdf
from app.services.stripe_service import StripeIntegrationError, create_checkout_session

router = APIRouter(prefix="/api/v1/invoices", tags=["invoices"])

_STAFF_ROLES = ("SUPER_ADMIN", "PROJECT_MANAGER", "LEAD_ENGINEER")


@router.get("", response_model=list[InvoiceOut])
def list_invoices(
    project_id: Optional[uuid.UUID] = None,
    db: Session = Depends(get_db),
    caller_org_id: Optional[uuid.UUID] = Depends(get_current_user_org_id),
) -> list[Invoice]:
    query = db.query(Invoice)
    if caller_org_id is not None:
        query = query.filter(Invoice.org_id == caller_org_id)
    if project_id:
        query = query.filter(Invoice.project_id == project_id)
    return query.order_by(Invoice.created_at.desc()).all()


@router.post("", response_model=InvoiceOut, status_code=201)
def create_invoice(
    payload: InvoiceCreate,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> Invoice:
    """Staff-only, ad-hoc — not tied to a milestone (retainers, one-off
    charges). Milestone-driven invoices only ever come from
    POST /milestones/{id}/approve, never this route."""
    project = db.get(Project, payload.project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    subtotal, tax, total = invoice_totals(payload.line_items, payload.tax_rate)
    issue = payload.issue_date or date.today()
    invoice = Invoice(
        org_id=project.org_id,
        project_id=project.id,
        milestone_id=None,
        description=payload.description,
        amount=total,
        status="PENDING",
        customer_email=payload.customer_email,
        bill_to_name=(payload.bill_to_name or "").strip() or None,
        line_items=[item.model_dump() for item in payload.line_items],
        subtotal=subtotal,
        tax_rate=payload.tax_rate,
        tax_amount=tax,
        issue_date=issue,
        due_date=payload.due_date or issue + timedelta(days=settings.INVOICE_DEFAULT_DUE_DAYS),
        notes=(payload.notes or "").strip() or None,
    )
    db.add(invoice)
    db.flush()  # assigns invoice.id so the audit row below can reference it
    record_audit_event(
        db,
        user=user,
        action="invoice.create_manual",
        resource_type="invoice",
        resource_id=invoice.id,
        org_id=invoice.org_id,
        metadata={"amount": total, "description": payload.description, "items": len(payload.line_items)},
    )
    commit_with_rls_refresh(db, invoice, None)
    return invoice


@router.post("/{invoice_id}/send-payment-link")
def send_invoice_payment_link(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> dict:
    """One-time payment path — this Invoice was never a SubscriptionPlan
    (see that model's docstring: one-time charges never create one).
    Creates a one-time Stripe Checkout session and emails it to
    invoice.customer_email; also callable again later to resend."""
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    if invoice.status == "PAID":
        raise HTTPException(409, "This invoice is already paid")
    if not invoice.customer_email:
        raise HTTPException(422, "This invoice has no customer_email to send a link to")

    try:
        checkout_url = create_checkout_session(
            invoice.id,
            float(invoice.amount),
            customer_email=invoice.customer_email,
            description=invoice.description,
        )
    except StripeIntegrationError as exc:
        raise HTTPException(502, str(exc)) from exc
    send_payment_link_email(invoice.customer_email, checkout_url, invoice.description or "GG HighTech invoice")
    return {"checkout_url": checkout_url}


def _invoice_filename(invoice: Invoice) -> str:
    return f"gghightech-invoice-{invoice.invoice_number or invoice.id}.pdf"


def _invoice_pdf(db: Session, invoice: Invoice, pay_url: Optional[str] = None) -> bytes:
    """The client-facing PDF. Older invoices with no line items (milestone
    approvals, subscriptions) print as a single line."""
    milestone = db.get(Milestone, invoice.milestone_id) if invoice.milestone_id else None
    project = db.get(Project, invoice.project_id)
    org = db.get(Organization, invoice.org_id)
    amount = float(invoice.amount)
    items = invoice.line_items or [
        {
            "description": (f"Milestone: {milestone.title}" if milestone else invoice.description) or "Services",
            "quantity": 1,
            "unit_price": amount,
        }
    ]
    return build_invoice_document(
        InvoiceDoc(
            invoice_id=invoice.id,
            number=invoice.invoice_number or str(invoice.id)[:8].upper(),
            project_title=project.title if project else "Project",
            client_org=org.name if org else "",
            bill_to_name=invoice.bill_to_name,
            bill_to_email=invoice.customer_email,
            line_items=items,
            subtotal=float(invoice.subtotal) if invoice.subtotal is not None else amount,
            tax_rate=float(invoice.tax_rate or 0),
            tax_amount=float(invoice.tax_amount or 0),
            total=amount,
            status=invoice.status,
            issue_date=invoice.issue_date or invoice.created_at.date(),
            due_date=invoice.due_date,
            paid_at=invoice.paid_at,
            notes=invoice.notes,
            pay_url=pay_url,
        )
    )


@router.post("/{invoice_id}/send", response_model=InvoiceSent)
def send_invoice(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> dict:
    """Email the invoice to invoice.customer_email: a summary, the PDF
    attached, a Stripe "pay online" link (when Stripe is set up) and a link
    to the client dashboard. Callable again to resend."""
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    if invoice.status == "PAID":
        raise HTTPException(409, "This invoice is already paid")
    if not invoice.customer_email:
        raise HTTPException(422, "Add the client's email to this invoice first")

    checkout_url: Optional[str] = None
    try:
        checkout_url = create_checkout_session(
            invoice.id,
            float(invoice.amount),
            customer_email=invoice.customer_email,
            description=f"{invoice.invoice_number}: {invoice.description or 'Invoice'}",
        )
    except StripeIntegrationError:
        # Still send it — the client can pay from the dashboard later.
        checkout_url = None

    project = db.get(Project, invoice.project_id)
    sent = send_invoice_email(
        to=invoice.customer_email,
        name=invoice.bill_to_name,
        number=invoice.invoice_number or "",
        project_title=project.title if project else "your project",
        total=float(invoice.amount),
        due_date=invoice.due_date,
        pay_url=checkout_url,
        portal_url=f"{settings.FRONTEND_URL.rstrip('/')}/portal?tab=payments",
        pdf=(_invoice_filename(invoice), _invoice_pdf(db, invoice, pay_url=checkout_url)),
    )
    if sent:
        invoice.sent_at = datetime.now(timezone.utc)
    record_audit_event(
        db,
        user=user,
        action="invoice.send",
        resource_type="invoice",
        resource_id=invoice.id,
        org_id=invoice.org_id,
        metadata={"email_sent": sent, "pay_link": bool(checkout_url)},
    )
    commit_with_rls_refresh(db, invoice, None)
    return {"invoice": invoice, "email_sent": sent, "checkout_url": checkout_url}


def _get_scoped_invoice(db: Session, invoice_id: uuid.UUID, caller_org_id: Optional[uuid.UUID]) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    if not invoice or (caller_org_id is not None and invoice.org_id != caller_org_id):
        raise HTTPException(404, "Invoice not found")
    return invoice


@router.get("/{invoice_id}/pdf")
def get_invoice_pdf(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    caller_org_id: Optional[uuid.UUID] = Depends(get_current_user_org_id),
) -> Response:
    invoice = _get_scoped_invoice(db, invoice_id, caller_org_id)
    pdf_bytes = _invoice_pdf(db, invoice)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{_invoice_filename(invoice)}"'},
    )


@router.get("/{invoice_id}/receipt")
def get_invoice_receipt(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    caller_org_id: Optional[uuid.UUID] = Depends(get_current_user_org_id),
) -> Response:
    """Receipt PDF for a paid invoice — whether the client paid online by
    card (Stripe webhook) or staff recorded a check / wire / cash payment."""
    invoice = _get_scoped_invoice(db, invoice_id, caller_org_id)
    if invoice.status != "PAID":
        raise HTTPException(409, "This invoice hasn't been paid yet, so there's no receipt")
    milestone = db.get(Milestone, invoice.milestone_id) if invoice.milestone_id else None
    project = db.get(Project, invoice.project_id)

    pdf_bytes = build_receipt_pdf(
        invoice_id=invoice.id,
        project_title=project.title if project else "Unknown project",
        paid_for=milestone.title if milestone else (invoice.description or "Ad-hoc invoice"),
        amount=float(invoice.amount),
        issued_at=invoice.created_at,
        paid_at=invoice.paid_at or invoice.created_at,
        payment_method=invoice.payment_method or "OTHER",
        payment_reference=invoice.payment_reference,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="gghightech-receipt-{invoice.id}.pdf"'},
    )


@router.post("/{invoice_id}/pay")
async def pay_invoice(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
    caller_org_id: Optional[uuid.UUID] = Depends(get_current_user_org_id),
) -> dict:
    """Create a Stripe-hosted Checkout Session for a pending invoice."""
    if user.role != "CLIENT_ADMIN":
        raise HTTPException(403, "Only a CLIENT_ADMIN can pay invoices")

    invoice = _get_scoped_invoice(db, invoice_id, caller_org_id)
    if invoice.status == "PAID":
        raise HTTPException(409, "This invoice is already paid")

    try:
        checkout_url = create_checkout_session(
            invoice.id,
            float(invoice.amount),
            customer_email=user.email,
            description=invoice.description,
        )
    except StripeIntegrationError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"checkout_url": checkout_url}


@router.patch("/{invoice_id}/mark-paid", response_model=InvoiceOut)
def mark_invoice_paid(
    invoice_id: uuid.UUID,
    # Optional so existing callers that send no body still work (recorded
    # as OTHER).
    payload: Optional[InvoiceMarkPaid] = Body(None),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> Invoice:
    """Staff-only — for real-world payments (check, wire) made outside
    Stripe until it's wired up."""
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    if invoice.status == "PAID":
        raise HTTPException(409, "This invoice is already paid")

    payload = payload or InvoiceMarkPaid()
    invoice.status = "PAID"
    invoice.paid_at = datetime.now(timezone.utc)
    invoice.payment_method = payload.payment_method
    invoice.payment_reference = (payload.payment_reference or "").strip() or None
    record_audit_event(
        db,
        user=user,
        action="invoice.mark_paid",
        resource_type="invoice",
        resource_id=invoice.id,
        org_id=invoice.org_id,
        metadata={"amount": float(invoice.amount), "payment_method": payload.payment_method},
    )
    commit_with_rls_refresh(db, invoice, None)
    return invoice
