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
from app.models.invoice import Invoice

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
    elif event_type in (
        "invoice.paid",
        "invoice.payment_failed",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        logger.info("Stripe subscription event received: %s (%s)", event_type, event.get("id"))
    else:
        logger.info("Unhandled Stripe event received: %s", event_type)

    return {"received": True}
