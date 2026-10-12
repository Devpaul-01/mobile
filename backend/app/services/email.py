"""
services/email.py — Reminder email delivery.

Stubbed exactly like services/auth_tokens.py's ``_deliver`` — logs instead
of calling a real provider, since SendGrid integration (architecture doc
§19/Phase 8) isn't wired up yet. This keeps the scheduler's call site
(services/scheduler.py) identical to what a real implementation will look
like: swap this function's body for a real SendGrid call later, nothing
above it in the call chain needs to change.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def send_reminder_email(*, to_email: str, contact_name: str) -> bool:
    """Returns ``True`` on (stubbed) success, ``False`` on failure — the
    scheduler uses this to decide whether to bump ``email_attempts`` and
    retry on the next pass (§16.3). The stub always succeeds today; a real
    SendGrid integration would return ``False`` on a provider error."""
    logger.info(
        "[dev email stub] birthday reminder for %s, to be sent to %s",
        contact_name,
        to_email,
    )
    return True
