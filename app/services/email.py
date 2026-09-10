"""
Email delivery — stubbed for Phase 0 (per project decision: wire up a real
provider like Resend once we're closer to launch).

send_lead_notification() is the single call site the rest of the app uses,
so swapping the stub for a real provider later is a one-file change.
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
