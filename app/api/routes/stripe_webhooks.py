"""Stripe webhook receiver.

Stripe signs the exact raw request body. Keep this route independent of
browser authentication and verify the Stripe-Signature header before doing
any database work.
"""

import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.instant_payment import InstantPayment
from app.models.invoice import Invoice
from app.models.subscription_plan import SubscriptionPlan
from app.services.stripe_service import StripeIntegrationError, end_subscription_after_current_period

router = APIRouter(prefix="/api/v1/stripe", tags=["stripe"])
logger = logging.getLogger("gghightech.stripe_webhooks")
SIGNATURE_TOLERANCE_SECONDS = 300


def _verify_signature(payload: bytes, signature_header: str) -> None:
    """Verify Stripe's timestamped v1 HMAC signature."""
    try:
        parts: dict[str, list[str]] = {}
        for item in signature_header.split(","):
            key, value = item.split("=", 1)
            parts.setdefault(key, []).append(value)
        timestamp = int(parts["t"][0])
        signatures = parts["v1"]
    except (KeyError, ValueError, IndexError) as exc:
        raise HTTPException(400, "Invalid Stripe-Signature header") from exc

    if abs(int(time.time()) - timestamp) > SIGNATURE_TOLERANCE_SECONDS:
        raise HTTPException(400, "Expired Stripe webhook signature")

    signed_payload = str(timestamp).encode() + b"." + payload
    expected = hmac.new(
        settings.STRIPE_WEBHOOK_SECRET.encode(), signed_payload, hashlib.sha256
    ).hexdigest()
    if not any(hmac.compare_digest(expected, signature) for signature in signatures):
        raise HTTPException(400, "Invalid Stripe webhook signature")


def _mark_invoice_paid(db: Session, session: dict) -> None:
    metadata = session.get("metadata") or {}
    raw_invoice_id = metadata.get("invoice_id")
    if not raw_invoice_id or session.get("payment_status") != "paid":
        return
    try:
        invoice_id = uuid.UUID(raw_invoice_id)
    except ValueError:
        logger.warning("Stripe session contains invalid invoice_id metadata: %s", raw_invoice_id)
        return

    # Webhooks are trusted system events after signature verification and
    # need the same RLS bypass that authenticated staff billing routes use.
    db.execute(text("SET LOCAL app.bypass_rls = 'true'"))
    invoice = db.get(Invoice, invoice_id)
    if not invoice or invoice.status == "PAID":
        return
    invoice.status = "PAID"
    invoice.paid_at = datetime.now(timezone.utc)
    db.commit()


def _mark_instant_payment_paid(db: Session, session: dict) -> None:
    metadata = session.get("metadata") or {}
    raw_payment_id = metadata.get("instant_payment_id")
    if not raw_payment_id or session.get("payment_status") != "paid":
        return
    try:
        payment_id = uuid.UUID(raw_payment_id)
    except ValueError:
        logger.warning("Stripe session contains invalid instant_payment_id metadata: %s", raw_payment_id)
        return

    payment = db.get(InstantPayment, payment_id)
    if not payment or payment.status == "PAID":
        return
    payment.status = "PAID"
    payment.paid_at = datetime.now(timezone.utc)
    db.commit()


def _subscription_refs(stripe_object: dict) -> tuple[Optional[str], Optional[str]]:
    """(plan_id from metadata, Stripe subscription id) from whichever kind
    of object the event carries. Checkout puts plan_id on the subscription's
    metadata (subscription_data[metadata]); an invoice only has a copy of
    that under subscription_details — top-level on older Stripe API
    versions, under `parent` on newer ones."""
    kind = stripe_object.get("object")
    if kind == "subscription":
        return (stripe_object.get("metadata") or {}).get("plan_id"), stripe_object.get("id")
    if kind == "invoice":
        details = stripe_object.get("subscription_details") or (stripe_object.get("parent") or {}).get(
            "subscription_details"
        ) or {}
        subscription = stripe_object.get("subscription") or details.get("subscription")
        if isinstance(subscription, dict):
            subscription = subscription.get("id")
        return (details.get("metadata") or {}).get("plan_id"), subscription
    # checkout.session
    return (stripe_object.get("metadata") or {}).get("plan_id"), stripe_object.get("subscription")


def _find_plan(db: Session, raw_plan_id: Optional[str], subscription_id: Optional[str]) -> Optional[SubscriptionPlan]:
    if raw_plan_id:
        try:
            plan = db.get(SubscriptionPlan, uuid.UUID(raw_plan_id))
        except ValueError:
            logger.warning("Stripe event contains invalid plan_id metadata: %s", raw_plan_id)
            plan = None
        if plan:
            return plan
    if subscription_id:
        return db.query(SubscriptionPlan).filter(SubscriptionPlan.stripe_subscription_id == subscription_id).first()
    return None


def _count_paid_installment(plan: SubscriptionPlan, invoice: dict) -> None:
    """One paid Stripe invoice = one billing period. On the last one of a
    fixed-length plan, tell Stripe not to renew. That call happens before
    anything is committed: if it fails, the webhook errors, Stripe retries
    the event later, and nothing has been counted twice."""
    invoice_id = invoice.get("id")
    if invoice_id and invoice_id == plan.last_stripe_invoice_id:
        return  # Stripe redelivered an event already counted
    if invoice.get("amount_paid", 1) == 0:
        return  # $0 invoices (e.g. a trial) aren't a paid period

    plan.cycles_billed += 1
    plan.last_stripe_invoice_id = invoice_id
    plan.last_invoiced_at = datetime.now(timezone.utc)
    if plan.total_cycles and plan.cycles_billed >= plan.total_cycles:
        if plan.stripe_subscription_id:
            try:
                end_subscription_after_current_period(plan.stripe_subscription_id)
            except StripeIntegrationError as exc:
                logger.error("Could not end Stripe subscription for completed plan %s", plan.id)
                raise HTTPException(502, "Could not end the Stripe subscription; retry later") from exc
        plan.status = "COMPLETED"
    elif plan.status == "PAUSED":
        plan.status = "ACTIVE"  # a failed payment that has now gone through


def _sync_subscription(db: Session, stripe_object: dict, event_type: str) -> None:
    raw_plan_id, subscription_id = _subscription_refs(stripe_object)
    plan = _find_plan(db, raw_plan_id, subscription_id)
    if not plan:
        return
    if subscription_id and not plan.stripe_subscription_id:
        plan.stripe_subscription_id = subscription_id

    if plan.status == "COMPLETED":
        pass  # finished — the remaining events (cancel_at_period_end, deleted) change nothing
    elif event_type == "invoice.paid":
        _count_paid_installment(plan, stripe_object)
    elif event_type == "invoice.payment_failed":
        plan.status = "PAUSED"
    elif event_type == "customer.subscription.deleted":
        plan.status = "CANCELED"
    elif event_type == "customer.subscription.updated":
        plan.status = "PAUSED" if stripe_object.get("status") in ("past_due", "unpaid", "paused") else "ACTIVE"
    elif plan.status != "CANCELED":
        plan.status = "ACTIVE"  # checkout completed
    db.commit()


@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: Optional[str] = Header(default=None, alias="Stripe-Signature"),
    db: Session = Depends(get_db),
) -> dict:
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(503, "Stripe webhook is not configured")
    if not stripe_signature:
        raise HTTPException(400, "Missing Stripe-Signature header")

    payload = await request.body()
    _verify_signature(payload, stripe_signature)
    try:
        event = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Invalid JSON payload") from exc

    event_type = event.get("type")
    data_object = (event.get("data") or {}).get("object") or {}
    if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        _mark_invoice_paid(db, data_object)
        _mark_instant_payment_paid(db, data_object)
        if data_object.get("mode") == "subscription":
            _sync_subscription(db, data_object, event_type)
    elif event_type in (
        "invoice.paid",
        "invoice.payment_failed",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        _sync_subscription(db, data_object, event_type)
        logger.info("Stripe subscription event received: %s (%s)", event_type, event.get("id"))
    else:
        logger.info("Unhandled Stripe event received: %s", event_type)

    return {"received": True}
