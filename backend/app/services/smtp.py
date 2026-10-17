"""
services/smtp.py — Shared SMTP email sender.

Replaces the architecture doc's §19 SendGrid framing with plain SMTP, per
an explicit team decision. Used by both services/auth_tokens.py
(verification/reset emails) and services/email.py (birthday reminder
emails) so there is exactly one place that talks to a mail server.

Uses Python's standard-library smtplib/email, run off the event loop via
asyncio.to_thread — smtplib is blocking, and this codebase has no other
dependency on an async SMTP client. No new third-party dependency is
required.

Configuration (see app/core/config.py): SMTP_HOST/SMTP_PORT/SMTP_USERNAME/
SMTP_PASSWORD/SMTP_USE_TLS/SMTP_FROM_EMAIL/SMTP_FROM_NAME. When
SMTP_HOST is unset (local dev, CI, the test suite), send_email() logs
the send and returns True without opening a socket — this mirrors the
previous stub's "works end-to-end without a configured provider"
behavior instead of making local dev/tests depend on a mail server.
Once SMTP_HOST is set, a real connection is attempted and a failure is
raised to the caller rather than swallowed, so the existing
retry-with-backoff loops in auth_tokens._deliver / email.send_reminder_email
behave the way they already assume a real provider would.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from app.core.config import settings

logger = logging.getLogger(__name__)


class SMTPNotConfiguredError(Exception):
    """Raised internally when a caller explicitly requires a real send
    (not currently used by any caller — both existing call sites treat
    "not configured" as a soft no-op in non-production environments, per
    the behavior the stub they replace already had)."""


def _send_sync(*, host: str, to_email: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{settings.smtp_from_name} <{settings.smtp_from_email}>"
    message["To"] = to_email
    message.set_content(body)

    with smtplib.SMTP(host, settings.smtp_port, timeout=10) as client:
        client.ehlo()
        if settings.smtp_use_tls:
            client.starttls()
            client.ehlo()
        if settings.smtp_username and settings.smtp_password:
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(message)


async def send_email(*, to_email: str, subject: str, body: str) -> bool:
    """Send one plaintext email. Returns True on success.

    Raises on failure when SMTP is configured, so callers' existing
    retry/backoff loops see real errors. Returns True without sending
    when SMTP_HOST is unset, so local dev/CI/tests keep working without
    a mail server configured — matching the behavior of the stub this
    module replaces.
    """
    host = settings.smtp_host
    if not host:
        logger.info(
            "[smtp not configured] would send %r to %s", subject, to_email
        )
        return True

    await asyncio.to_thread(
        _send_sync, host=host, to_email=to_email, subject=subject, body=body
    )
    return True
