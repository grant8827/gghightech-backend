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
    return _request("POST", path, data)


def _request(method: str, path: str, data: Optional[dict] = None) -> dict:
    if not settings.STRIPE_SECRET_KEY:
        raise StripeIntegrationError("Stripe is not configured")
    try:
        response = httpx.request(
            method,
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


def create_instant_checkout_session(
    payment_id: uuid.UUID,
    amount: float,
    customer_email: str,
    description: Optional[str] = None,
) -> str:
    """For an InstantPayment — the webhook tells it apart from an invoice
    by the instant_payment_id metadata key."""
    data = {
        "mode": "payment",
        "success_url": settings.STRIPE_SUCCESS_URL,
        "cancel_url": settings.STRIPE_CANCEL_URL,
        "client_reference_id": str(payment_id),
        "customer_email": customer_email,
        "metadata[instant_payment_id]": str(payment_id),
        "payment_intent_data[metadata][instant_payment_id]": str(payment_id),
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(round(amount * 100)),
        "line_items[0][price_data][product_data][name]": description or "GG HighTech payment",
        "line_items[0][quantity]": "1",
    }
    session = _post("/checkout/sessions", data)
    checkout_url = session.get("url")
    if not checkout_url:
        raise StripeIntegrationError("Stripe did not return a Checkout URL")
    return checkout_url


def create_subscription_checkout_session(
    plan_id: uuid.UUID,
    amount: float,
    name: str,
    interval: str = "month",
    customer_email: Optional[str] = None,
) -> str:
    """interval is Stripe's recurring-interval string — "month" for a
    Monthly plan a client opted to auto-renew, "year" for an Annual plan
    (always recurring, see SubscriptionPlan's docstring)."""
    data = {
        "mode": "subscription",
        "success_url": settings.STRIPE_SUCCESS_URL,
        "cancel_url": settings.STRIPE_CANCEL_URL,
        "client_reference_id": str(plan_id),
        "metadata[plan_id]": str(plan_id),
        "subscription_data[metadata][plan_id]": str(plan_id),
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(round(amount * 100)),
        "line_items[0][price_data][recurring][interval]": interval,
        "line_items[0][price_data][product_data][name]": name,
        "line_items[0][quantity]": "1",
    }
    if customer_email:
        data["customer_email"] = customer_email
    session = _post("/checkout/sessions", data)
    checkout_url = session.get("url")
    if not checkout_url:
        raise StripeIntegrationError("Stripe did not return a subscription Checkout URL")
    return checkout_url


def end_subscription_after_current_period(subscription_id: str) -> None:
    """For a fixed-length plan whose last installment just got paid: no
    further renewal invoices, access runs to the end of the paid period."""
    _post(f"/subscriptions/{subscription_id}", {"cancel_at_period_end": "true"})


def cancel_subscription_now(subscription_id: str) -> None:
    """Staff canceled the plan: stop billing immediately."""
    _request("DELETE", f"/subscriptions/{subscription_id}")
