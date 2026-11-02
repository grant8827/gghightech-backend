"""Instant payments — see app/models/instant_payment.py. Staff-only."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.instant_payment import InstantPayment
from app.schemas.instant_payment import InstantPaymentCreate, InstantPaymentOut, InstantPaymentSent
from app.services.audit import record_audit_event
from app.services.auth import AuthenticatedUser, require_roles
from app.services.email import send_payment_link_email
from app.services.stripe_service import StripeIntegrationError, create_instant_checkout_session

router = APIRouter(prefix="/api/v1/instant-payments", tags=["instant-payments"])

_STAFF_ROLES = ("SUPER_ADMIN", "PROJECT_MANAGER", "LEAD_ENGINEER")


def _send_link(db: Session, payment: InstantPayment) -> InstantPaymentSent:
    """Creates a fresh Checkout session (they expire after 24 hours, so a
    resend can't reuse the old one) and emails it."""
    try:
        checkout_url = create_instant_checkout_session(
            payment.id,
            float(payment.amount),
            customer_email=payment.customer_email,
            description=payment.description,
        )
    except StripeIntegrationError as exc:
        raise HTTPException(502, str(exc)) from exc
    payment.checkout_url = checkout_url
    db.commit()
    db.refresh(payment)
    email_sent = send_payment_link_email(
        payment.customer_email, checkout_url, payment.description or "GG HighTech payment"
    )
    return InstantPaymentSent(payment=payment, checkout_url=checkout_url, email_sent=email_sent)


@router.get("", response_model=list[InstantPaymentOut])
def list_instant_payments(
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> list[InstantPayment]:
    return db.query(InstantPayment).order_by(InstantPayment.created_at.desc()).limit(200).all()


@router.post("", response_model=InstantPaymentSent, status_code=201)
def create_instant_payment(
    payload: InstantPaymentCreate,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> InstantPaymentSent:
    """Records the request first, so it shows up in the list even if the
    Stripe step fails (e.g. Stripe isn't configured) — it can be resent."""
    description = (payload.description or "").strip() or None
    payment = InstantPayment(
        customer_email=payload.customer_email,
        amount=payload.amount,
        description=description,
        status="PENDING",
        created_by=user.email,
    )
    db.add(payment)
    db.flush()
    record_audit_event(
        db,
        user=user,
        action="instant_payment.create",
        resource_type="instant_payment",
        resource_id=payment.id,
        metadata={"amount": payload.amount, "description": description},
    )
    db.commit()
    db.refresh(payment)
    return _send_link(db, payment)


@router.post("/{payment_id}/resend", response_model=InstantPaymentSent)
def resend_instant_payment(
    payment_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> InstantPaymentSent:
    payment = db.get(InstantPayment, payment_id)
    if not payment:
        raise HTTPException(404, "Payment not found")
    if payment.status == "PAID":
        raise HTTPException(409, "This payment is already paid")
    return _send_link(db, payment)
