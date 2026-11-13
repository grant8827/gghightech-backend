"""
Transactional email via Mailgun's HTTP API.

The public send_* functions below are the only call sites the rest of the
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


def _send(
    kind: str,
    to: str,
    subject: str,
    text: str,
    attachments: Optional[list[tuple[str, bytes, str]]] = None,
    reply_to: Optional[str] = None,
) -> bool:
    """Returns whether Mailgun accepted the message. `attachments` is a
    list of (filename, content, MIME type)."""
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
                # Lets staff answer a contact message with plain Reply.
                **({"h:Reply-To": _one_line(reply_to)} if reply_to else {}),
            },
            files=[("attachment", attachment) for attachment in attachments or []] or None,
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


def send_contact_notification(
    message_id: str,
    name: str,
    email: str,
    phone: Optional[str],
    subject: Optional[str],
    message: str,
) -> bool:
    """Tell the team (LEAD_NOTIFICATION_EMAIL) about a new contact-form
    message (app/api/routes/messages.py). The message is already saved and
    shown in the admin Messages tab; this email is a heads-up, and Reply
    goes straight to the sender."""
    text = (
        "A new message was sent from the website.\n\n"
        f"From: {name} <{email}>\n"
        f"Phone: {phone or '(none given)'}\n"
        f"Subject: {subject or '(none)'}\n\n"
        f"{message}\n\n"
        f"Reply to this email to answer {name} directly, or see it in the admin dashboard: "
        f"{settings.FRONTEND_URL.rstrip('/')}/admin\n"
    )
    return _send(
        "contact-notification",
        settings.LEAD_NOTIFICATION_EMAIL,
        f"New message from {name}" + (f": {subject}" if subject else ""),
        text,
        reply_to=email,
    )


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


def send_invoice_email(
    to: str,
    name: Optional[str],
    number: str,
    project_title: str,
    total: float,
    due_date,
    pay_url: Optional[str],
    portal_url: str,
    pdf: tuple[str, bytes],
) -> bool:
    """Sent by POST /invoices/{id}/send (app/api/routes/invoices.py)."""
    due = due_date.strftime("%B %d, %Y") if due_date else "on receipt"
    pay = f"Pay securely online by card:\n{pay_url}\n\n" if pay_url else ""
    text = (
        f"Hi {name or 'there'},\n\n"
        f"Please find attached invoice {number} for {project_title}.\n\n"
        f"Amount due: ${total:,.2f}\n"
        f"Due date: {due}\n\n"
        f"{pay}"
        f"You can also view, pay and download receipts from your client dashboard:\n{portal_url}\n\n"
        f"Thank you for your business,\n{settings.BUSINESS_NAME}\n"
    )
    filename, content = pdf
    return _send(
        "invoice",
        to,
        f"Invoice {number} from {settings.BUSINESS_NAME} - ${total:,.2f} due {due}",
        text,
        attachments=[(filename, content, "application/pdf")],
        reply_to=settings.BUSINESS_EMAIL or None,
    )


def send_contract_email(to: str, name: str, number: str, title: str, portal_url: str, pdf: tuple[str, bytes]) -> bool:
    """Sent by POST /contracts/{id}/send (app/api/routes/contracts.py)."""
    text = (
        f"Hi {name},\n\n"
        f"Your agreement {number}, \"{title}\", is ready for review and signature. A copy is attached.\n\n"
        f"To sign it, sign in to your client dashboard and open Contracts:\n{portal_url}\n\n"
        f"If anything needs changing, just reply to this email.\n\n"
        f"Thank you,\n{settings.BUSINESS_NAME}\n"
    )
    filename, content = pdf
    return _send(
        "contract",
        to,
        f"Please review and sign: {title} ({number})",
        text,
        attachments=[(filename, content, "application/pdf")],
        reply_to=settings.BUSINESS_EMAIL or None,
    )


def send_contract_signed_email(to: str, number: str, title: str, signer_name: str, pdf: tuple[str, bytes]) -> bool:
    """Signed copy, sent to the client and to the team when a contract is
    signed (POST /contracts/{id}/sign)."""
    text = (
        f"Agreement {number}, \"{title}\", was signed by {signer_name}.\n\n"
        "The fully signed copy is attached for your records.\n\n"
        f"{settings.BUSINESS_NAME}\n"
    )
    filename, content = pdf
    return _send("contract-signed", to, f"Signed: {title} ({number})", text, attachments=[(filename, content, "application/pdf")])


def send_password_reset_email(email: str, full_name: str, reset_link: str) -> bool:
    """Sent by POST /auth/forgot-password (app/api/routes/auth.py)."""
    text = (
        f"Hi {full_name},\n\n"
        "Someone asked to reset the password for your GG HighTech account. "
        "Use the link below to choose a new one:\n\n"
        f"{reset_link}\n\n"
        "This link works once and expires in 60 minutes. If you didn't ask for this, you can ignore "
        "this email — your password won't change.\n"
    )
    return _send("password-reset", email, "Reset your GG HighTech password", text)


def send_estimate_pdf_email(email: str, estimate_id: str, pdf_bytes: bytes) -> bool:
    """Sent to a visitor who left an email on the public estimator
    (POST /estimates, app/api/routes/estimates.py), with their estimate
    attached."""
    text = (
        "Hello,\n\n"
        "Thanks for using the GG HighTech project estimator. Your estimate is attached as a PDF.\n\n"
        "It's a starting point, not a quote — we'll follow up to talk through your project and firm up "
        "the details. If you have questions in the meantime, just reply to this email.\n\n"
        "— GG HighTech\n"
    )
    return _send(
        "estimate-pdf",
        email,
        "Your GG HighTech project estimate",
        text,
        attachments=[(f"gghightech-estimate-{estimate_id[:8]}.pdf", pdf_bytes, "application/pdf")],
    )
