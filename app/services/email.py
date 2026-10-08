"""
Transactional email via Mailgun's HTTP API.

The three public functions below are the only call sites the rest of the
app uses, so changing provider stays a one-file change.

Two rules this module exists to keep:

1. Nothing sensitive goes to the log. The links these emails carry are
   credentials — an invite link sets an account's password, a payment link
   opens a Checkout session — and a lead notification carries a prospect's
   contact details and brief. Log lines name the kind of email, a masked
   recipient, and whether delivery worked; never the body, subject or link.
   The one exception is the development-only fallback in _send, which exists
   so the app is usable locally with no mail account at all.

2. Sending never breaks the request. Every caller has already committed
   its database change by the time it gets here, so a mail failure is
   logged and reported as False, not raised.
"""

from typing import Optional

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger("gghightech.email")

_TIMEOUT_SECONDS = 10.0


def email_configured() -> bool:
    return bool(settings.MAILGUN_API_KEY and settings.MAILGUN_DOMAIN)


def mask_email(address: Optional[str]) -> str:
    """j***@example.com — enough to recognise a recipient when reading
    logs, without the log becoming a list of customer addresses."""
    local, _, domain = (address or "").partition("@")
    if not local or not domain:
        return "***"
    return f"{local[0]}***@{domain}"


def _one_line(value: str) -> str:
    """Subjects include free text (invoice descriptions, plan names); keep
    them to a single line so they can't smuggle in extra headers."""
    return " ".join(value.split())[:200]


def _send(kind: str, to: str, subject: str, text: str) -> bool:
    """Returns whether Mailgun accepted the message."""
    if not email_configured():
        if settings.is_development:
            # Development only: with no mail account configured, this is
            # how a developer gets at an invite or payment link. Never
            # reached in any other environment.
            logger.info(
                "DEV ONLY — email is not configured, so this %s email was not sent.\nTo: %s\nSubject: %s\n\n%s",
                kind,
                to,
                subject,
                text,
            )
        else:
            logger.warning(
                "Email is not configured (MAILGUN_API_KEY / MAILGUN_DOMAIN); %s email to %s was NOT sent",
                kind,
                mask_email(to),
            )
        return False

    sender = settings.MAILGUN_FROM_EMAIL or f"GG HighTech <no-reply@{settings.MAILGUN_DOMAIN}>"
    try:
        response = httpx.post(
            f"{settings.MAILGUN_API_BASE.rstrip('/')}/v3/{settings.MAILGUN_DOMAIN}/messages",
            auth=("api", settings.MAILGUN_API_KEY),
            data={
                "from": sender,
                "to": to,
                "subject": _one_line(subject),
                "text": text,
                # Click/open tracking rewrites every link to pass through
                # Mailgun and records it there — not something to do to a
                # link that is itself a credential.
                "o:tracking": "no",
            },
            timeout=_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — see rule 2 in the module docstring
        # Type name only: an httpx error's message can embed the request URL.
        logger.error("%s email to %s failed: %s", kind, mask_email(to), type(exc).__name__)
        return False

    if response.status_code >= 300:
        logger.error("%s email to %s was rejected by Mailgun (HTTP %s)", kind, mask_email(to), response.status_code)
        return False

    logger.info("%s email sent to %s", kind, mask_email(to))
    return True


def send_lead_notification(estimate_id: str, client_email: Optional[str], summary: str) -> bool:
    """Tell the internal team (LEAD_NOTIFICATION_EMAIL) about a new estimate
    submission. `client_email` is the contact line the visitor left — email
    and/or phone — and `summary` the scope and brief; both belong in the
    email and nowhere else."""
    text = (
        "A new estimate was submitted on the website.\n\n"
        f"Reference: {estimate_id}\n"
        f"Contact: {client_email or '(none given)'}\n\n"
        f"{summary}\n\n"
        f"Review it in the admin dashboard: {settings.FRONTEND_URL.rstrip('/')}/admin\n"
    )
    return _send("lead-notification", settings.LEAD_NOTIFICATION_EMAIL, f"New estimate request {estimate_id[:8]}", text)


def send_invite_email(email: str, full_name: str, invite_link: str) -> bool:
    """Sent when a staff member invites a new user (app/api/routes/users.py)."""
    text = (
        f"Hi {full_name},\n\n"
        "You've been invited to GG HighTech. Use the link below to set your password and sign in:\n\n"
        f"{invite_link}\n\n"
        "This link works once and expires in 7 days. If you weren't expecting this, you can ignore this email.\n"
    )
    return _send("invite", email, "Your GG HighTech account invitation", text)


def send_payment_link_email(customer_email: str, checkout_url: str, description: str) -> bool:
    """Sent when staff create a Stripe Checkout link for a one-time invoice
    (app/api/routes/invoices.py) or a subscription plan
    (app/api/routes/subscriptions.py)."""
    text = (
        "Hello,\n\n"
        f"A secure payment link is ready for: {description}\n\n"
        f"{checkout_url}\n\n"
        "Payment is handled by Stripe. If you have any questions, just reply to this email.\n"
    )
    return _send("payment-link", customer_email, f"Payment link: {description}", text)
