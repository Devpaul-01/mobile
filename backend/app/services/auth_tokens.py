"""
services/auth_tokens.py — Email-verification and password-reset token
lifecycle: create, verify, and mark-used. Retry-with-backoff send pattern
copied from bm-ts/backend/app/services/password_reset.py.

SendGrid integration is MOBILE_APP_ARCHITECTURE_SPEC.md §19 (Phase 8) —
not implemented yet. ``_deliver`` logs the link instead of sending a real
email, so the auth lifecycle (register → verify → login) works end-to-end in
local dev and in tests without a configured email provider. Swap this
function's body for a real SendGrid call in Phase 8; nothing above it in the
call chain needs to change.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EmailVerificationToken, PasswordResetToken, User

logger = logging.getLogger(__name__)

_VERIFICATION_TOKEN_TTL = timedelta(hours=24)
_RESET_TOKEN_TTL = timedelta(minutes=30)


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


async def _deliver(kind: str, email: str, token: str) -> None:
    """Stand-in for a real email send. Retries 3x with backoff like the
    pattern it's copied from, even though right now the 'send' never fails —
    this keeps the call site identical to what Phase 8 will look like.

    Deliberately never logs ``token`` itself, even at DEBUG level — a
    password-reset/verification token is credential-equivalent, and this
    stub's whole point is standing in for a real send, not creating a
    log-based bypass of the email channel it's stubbing out."""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            logger.info("[dev email stub] %s requested for %s", kind, email)
            return
        except Exception as exc:  # pragma: no cover — stub never raises today
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
