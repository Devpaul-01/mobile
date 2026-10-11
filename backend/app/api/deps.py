"""
api/deps.py — FastAPI dependency functions shared across all routers.

Copied-pattern from bm-ts/backend/app/api/deps.py against the new ``User``/
``UserSession`` tables. The primary export is ``get_current_user``, used as
``Depends(get_current_user)`` on every authenticated route. It:
  1. Extracts the Bearer token from the ``Authorization`` header.
  2. Decodes and validates the JWT (signature + expiry + ``type == access``).
  3. Verifies the backing session is unrevoked and unexpired.
  4. Looks up the ``User`` row.
  5. Returns the ``User`` ORM object, or raises 401/403 if any step fails.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import Depends, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.errors import AppError, ErrorCodes
from app.core.jwt import JWTError, TokenType, decode_token, ensure_token_type
from app.db.models import User, UserSession
from app.db.session import get_db

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/mobile/auth/login")


def _as_utc(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime for DB values that may be naive in tests."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _raise_credentials_error() -> AppError:
    raise AppError(
        status_code=status.HTTP_401_UNAUTHORIZED,
        code=ErrorCodes.INVALID_CREDENTIALS,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_token_payload(
    token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Decode an access token and verify its backing session when present."""
    try:
        payload = decode_token(token)
        ensure_token_type(payload, TokenType.ACCESS)
        raw_user_id = payload.get("sub")
        if raw_user_id is None:
            raise _raise_credentials_error()
        user_id = UUID(str(raw_user_id))
    except JWTError:
        raise _raise_credentials_error()
    except ValueError as exc:
        raise _raise_credentials_error() from exc

    payload["sub"] = user_id

    session_id = payload.get("sid")
    if session_id is None:
        return payload

    try:
        session_uuid = UUID(str(session_id))
    except ValueError as exc:
        raise _raise_credentials_error() from exc

    result = await db.execute(
        select(UserSession).where(
            UserSession.id == session_uuid, UserSession.user_id == user_id
        )
    )
    session = result.scalar_one_or_none()
    if session is None or session.revoked_at is not None:
        raise _raise_credentials_error()

    if _as_utc(session.expires_at) <= datetime.now(timezone.utc):
        raise _raise_credentials_error()

    return payload


async def get_current_user(
    payload: dict[str, Any] = Depends(get_current_token_payload),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Resolve a Bearer access token to a ``User`` row.

    Raises 401 if the token is invalid/expired/wrong-type or the user no
    longer exists. Raises 403 if the user's email is not verified or the
    account is locked.
    """
    user = await _resolve_user(payload, db)

    if not user.email_verified:
        raise AppError(
            status_code=status.HTTP_403_FORBIDDEN,
            code=ErrorCodes.EMAIL_NOT_VERIFIED,
            detail="Email not verified",
        )
    if user.locked_until is not None and _as_utc(user.locked_until) > datetime.now(
        timezone.utc
    ):
        raise AppError(
            status_code=status.HTTP_403_FORBIDDEN,
            code=ErrorCodes.ACCOUNT_LOCKED,
            detail="Account is temporarily locked",
        )

    return user


async def get_current_user_allow_unverified(
    payload: dict[str, Any] = Depends(get_current_token_payload),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Like ``get_current_user`` but skips the email-verification check.

    Use on endpoints an authenticated-but-unverified user must still reach
    (``/mobile/auth/me``, ``/mobile/auth/logout``). Account lockout is still
    enforced.
    """
    user = await _resolve_user(payload, db)

    if user.locked_until is not None and _as_utc(user.locked_until) > datetime.now(
        timezone.utc
    ):
        raise AppError(
            status_code=status.HTTP_403_FORBIDDEN,
            code=ErrorCodes.ACCOUNT_LOCKED,
            detail="Account is temporarily locked",
        )

    return user


async def _resolve_user(payload: dict[str, Any], db: AsyncSession) -> User:
    user_id = payload.get("sub")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise _raise_credentials_error()
    return user
