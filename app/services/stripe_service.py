"""Stripe-hosted Checkout integration."""

import logging
import uuid
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger("gghightech.billing")
STRIPE_API_URL = "https://api.stripe.com/v1"


class StripeIntegrationError(RuntimeError):
    pass


def _post(path: str, data: dict) -> dict:
    if not settings.STRIPE_SECRET_KEY:
        raise StripeIntegrationError("Stripe is not configured")
    try:
        response = httpx.post(
            f"{STRIPE_API_URL}{path}",
            auth=(settings.STRIPE_SECRET_KEY, ""),
            data=data,
            timeout=20,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("error", {}).get("message", "Stripe rejected the request")
        except ValueError:
            detail = "Stripe rejected the request"
        logger.error("Stripe API error: %s", detail)
        raise StripeIntegrationError(detail) from exc
    except httpx.HTTPError as exc:
        logger.exception("Stripe API request failed")
        raise StripeIntegrationError("Could not reach Stripe") from exc


def create_checkout_session(
    invoice_id: uuid.UUID,
    amount: float,
    customer_email: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    data = {
        "mode": "payment",
        "success_url": settings.STRIPE_SUCCESS_URL,
        "cancel_url": settings.STRIPE_CANCEL_URL,
        "client_reference_id": str(invoice_id),
        "metadata[invoice_id]": str(invoice_id),
        "payment_intent_data[metadata][invoice_id]": str(invoice_id),
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(round(amount * 100)),
        "line_items[0][price_data][product_data][name]": f"GG HighTech Invoice {str(invoice_id)[:8]}",
        "line_items[0][price_data][product_data][description]": description or "Software services",
        "line_items[0][quantity]": "1",
    }
    if customer_email:
        data["customer_email"] = customer_email
    session = _post("/checkout/sessions", data)
    checkout_url = session.get("url")
    if not checkout_url:
        raise StripeIntegrationError("Stripe did not return a Checkout URL")
    return checkout_url


def create_subscription_checkout_session(plan_id: uuid.UUID, amount: float, name: str) -> str:
    data = {
        "mode": "subscription",
        "success_url": settings.STRIPE_SUCCESS_URL,
        "cancel_url": settings.STRIPE_CANCEL_URL,
        "client_reference_id": str(plan_id),
        "metadata[plan_id]": str(plan_id),
        "subscription_data[metadata][plan_id]": str(plan_id),
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(round(amount * 100)),
        "line_items[0][price_data][recurring][interval]": "month",
        "line_items[0][price_data][product_data][name]": name,
        "line_items[0][quantity]": "1",
    }
    session = _post("/checkout/sessions", data)
    checkout_url = session.get("url")
    if not checkout_url:
        raise StripeIntegrationError("Stripe did not return a subscription Checkout URL")
    return checkout_url
