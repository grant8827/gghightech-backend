from typing import Optional

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.invoice import Invoice
from app.models.milestone import Milestone
from app.models.project import Project
from app.schemas.invoice import InvoiceCreate, InvoiceOut
from app.services.audit import record_audit_event
from app.services.auth import (
    AuthenticatedUser,
    commit_with_rls_refresh,
    get_current_user,
    get_current_user_org_id,
    require_roles,
)
from app.services.pdf import build_invoice_pdf
from app.services.stripe_service import create_checkout_session

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

    invoice = Invoice(
        org_id=project.org_id,
        project_id=project.id,
        milestone_id=None,
        description=payload.description,
        amount=payload.amount,
        status="PENDING",
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
        metadata={"amount": payload.amount, "description": payload.description},
    )
    commit_with_rls_refresh(db, invoice, None)
    return invoice


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
    milestone = db.get(Milestone, invoice.milestone_id) if invoice.milestone_id else None
    project = db.get(Project, invoice.project_id)

    billed_for = milestone.title if milestone else (invoice.description or "Ad-hoc invoice")
    pdf_bytes = build_invoice_pdf(
        invoice_id=invoice.id,
        project_title=project.title if project else "Unknown project",
        billed_for=billed_for,
        amount=float(invoice.amount),
        status=invoice.status,
        created_at=invoice.created_at,
        paid_at=invoice.paid_at,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="gghightech-invoice-{invoice.id}.pdf"'},
    )


@router.post("/{invoice_id}/pay")
async def pay_invoice(
    invoice_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
    caller_org_id: Optional[uuid.UUID] = Depends(get_current_user_org_id),
) -> dict:
    """CLIENT_ADMIN-gated like approve_milestone. Stripe stays stubbed
    (app/services/stripe_service.py) until a real account exists — this
    responds 503 rather than ever pretending a payment happened."""
    if user.role != "CLIENT_ADMIN":
        raise HTTPException(403, "Only a CLIENT_ADMIN can pay invoices")

    invoice = _get_scoped_invoice(db, invoice_id, caller_org_id)
    if invoice.status == "PAID":
        raise HTTPException(409, "This invoice is already paid")

    session = create_checkout_session(invoice.id, float(invoice.amount))
    if session is None:
        raise HTTPException(
            503,
            "Online payment isn't set up yet — contact GG HighTech for other payment instructions.",
        )
    return {"checkout_url": session}  # pragma: no cover — unreachable until Stripe is wired up


@router.patch("/{invoice_id}/mark-paid", response_model=InvoiceOut)
def mark_invoice_paid(
    invoice_id: uuid.UUID,
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

    invoice.status = "PAID"
    invoice.paid_at = datetime.now(timezone.utc)
    record_audit_event(
        db,
        user=user,
        action="invoice.mark_paid",
        resource_type="invoice",
        resource_id=invoice.id,
        org_id=invoice.org_id,
        metadata={"amount": float(invoice.amount)},
    )
    commit_with_rls_refresh(db, invoice, None)
    return invoice
