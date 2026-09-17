"""Billing — stubbed until a real Stripe account exists (same shape as
app/services/email.py's stub). create_checkout_session() is the single call
site app/api/routes/invoices.py uses, so swapping in the real Stripe SDK
later is a one-file change.
"""

import logging
import uuid

from app.core.config import settings

logger = logging.getLogger("gghightech.billing")


def create_checkout_session(invoice_id: uuid.UUID, amount: float) -> None:
    """Returns None when Stripe isn't configured — callers must treat that
    as "payment did not happen," never as a stand-in success. Raises if
    STRIPE_SECRET_KEY is set but the integration isn't wired up yet, same
    honesty standard as the email stub: never silently pretend to charge
    someone's card."""

    if settings.STRIPE_SECRET_KEY:
        raise NotImplementedError("STRIPE_SECRET_KEY is set but the Stripe integration isn't wired up yet")

    logger.info("STUB STRIPE — would create a checkout session for invoice %s ($%.2f)", invoice_id, amount)
    return None
