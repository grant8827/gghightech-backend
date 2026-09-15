"""
Email delivery — stubbed for Phase 0 (per project decision: wire up a real
provider like Resend once we're closer to launch).

send_lead_notification() and send_invite_email() are the two call sites the
rest of the app uses, so swapping the stub for a real provider later is a
one-file change.
"""

from typing import Optional

import logging

from app.core.config import settings

logger = logging.getLogger("gghightech.email")


def send_lead_notification(estimate_id: str, client_email: Optional[str], summary: str) -> bool:
    """Notify the internal team about a new estimate submission, and (when a
    client email was given) send them a confirmation. Returns whether a real
    send happened (always False until RESEND_API_KEY is set)."""

    if settings.RESEND_API_KEY:
        # TODO(Phase 2): call Resend here once RESEND_API_KEY is provisioned.
        raise NotImplementedError("RESEND_API_KEY is set but the Resend integration isn't wired up yet")

    logger.info(
        "STUB EMAIL — would notify %s of new estimate %s from %s: %s",
        settings.LEAD_NOTIFICATION_EMAIL,
        estimate_id,
        client_email or "(no email provided)",
        summary,
    )
    return False


def send_invite_email(email: str, full_name: str, invite_link: str) -> bool:
    """Sent when a staff member invites a new user (app/api/routes/users.py).
    Returns whether a real send happened (always False until RESEND_API_KEY
    is set) — in dev, the link is only visible in this log line."""

    if settings.RESEND_API_KEY:
        raise NotImplementedError("RESEND_API_KEY is set but the Resend integration isn't wired up yet")

    logger.info("STUB EMAIL — would invite %s (%s) to set a password: %s", full_name, email, invite_link)
    return False
