"""
services/auth_tokens.py — Email-verification and password-reset token
lifecycle: create, verify, and mark-used. Retry-with-backoff send pattern
copied from bm-ts/backend/app/services/password_reset.py.

``_deliver`` sends via app/services/smtp.py (plain SMTP — a team decision
in place of the architecture doc's §19 SendGrid framing). When SMTP isn't
configured (local dev, CI, the test suite), the sender logs and returns
success without opening a connection, so the auth lifecycle
(register → verify → login) still works end-to-end without a real mail
server configured.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EmailVerificationToken, PasswordResetToken, User
from app.services.smtp import send_email

logger = logging.getLogger(__name__)

_SUBJECTS = {
    "email verification": "Verify your email",
    "password reset": "Reset your password",
}

_VERIFICATION_TOKEN_TTL = timedelta(hours=24)
_RESET_TOKEN_TTL = timedelta(minutes=30)


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


async def _deliver(kind: str, email: str, token: str) -> None:
    """Send a verification/reset email via SMTP, retrying 3x with backoff
    on failure (only relevant once SMTP_HOST is actually configured —
    see app/services/smtp.py for the unconfigured-environment behavior).

    Deliberately never logs ``token`` itself, even at DEBUG level — a
    password-reset/verification token is credential-equivalent, and
    logging it would be a bypass of the email channel this function
    exists to send it over."""
    body = f"Your {kind} code:\n\n{token}\n\nThis code will expire soon."
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            await send_email(
                to_email=email,
                subject=_SUBJECTS.get(kind, kind.title()),
                body=body,
            )
            return
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                await asyncio.sleep(2**attempt)
    logger.warning("Failed to deliver %s to %s after 3 attempts: %s", kind, email, last_exc)


async def create_verification_token(user: User, db: AsyncSession) -> str:
    token = _generate_token()
    db.add(
        EmailVerificationToken(
            user_id=user.id,
            token=token,
            expires_at=datetime.now(timezone.utc) + _VERIFICATION_TOKEN_TTL,
        )
    )
    return token


async def send_verification_email(email: str, token: str) -> None:
    await _deliver("email verification", email, token)


async def verify_verification_token(
    token: str, db: AsyncSession
) -> EmailVerificationToken | None:
    result = await db.execute(
        select(EmailVerificationToken).where(EmailVerificationToken.token == token)
    )
    row = result.scalar_one_or_none()
    if row is None or row.used_at is not None:
        return None
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        return None
    return row


def mark_verification_token_used(row: EmailVerificationToken) -> None:
    row.used_at = datetime.now(timezone.utc)


async def create_reset_token(user: User, db: AsyncSession) -> str:
    token = _generate_token()
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token=token,
            expires_at=datetime.now(timezone.utc) + _RESET_TOKEN_TTL,
        )
    )
    return token


async def send_reset_email(email: str, token: str) -> None:
    await _deliver("password reset", email, token)


async def verify_reset_token(
    token: str, db: AsyncSession
) -> PasswordResetToken | None:
    result = await db.execute(
        select(PasswordResetToken).where(PasswordResetToken.token == token)
    )
    row = result.scalar_one_or_none()
    if row is None or row.used_at is not None:
        return None
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        return None
    return row


def mark_reset_token_used(row: PasswordResetToken) -> None:
    row.used_at = datetime.now(timezone.utc)
