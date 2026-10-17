"""
services/email.py — Reminder email delivery.

Sends via app/services/smtp.py (plain SMTP — a team decision in place of
the architecture doc's §19 SendGrid framing). When SMTP isn't configured
(local dev, CI, tests), the sender logs and returns success without
opening a connection, matching the previous stub's behavior for those
environments.
"""

from __future__ import annotations

import logging

from app.services.smtp import send_email

logger = logging.getLogger(__name__)


async def send_reminder_email(*, to_email: str, contact_name: str) -> bool:
    """Returns ``True`` on success, ``False`` on failure — the scheduler
    uses this to decide whether to bump ``email_attempts`` and retry on
    the next pass (§16.3)."""
    try:
        await send_email(
            to_email=to_email,
            subject=f"Don't forget: {contact_name}'s birthday",
            body=f"This is a reminder that it's {contact_name}'s birthday.",
        )
        return True
    except Exception:
        logger.exception(
            "Failed to send birthday reminder email for %s to %s",
            contact_name,
            to_email,
        )
        return False
