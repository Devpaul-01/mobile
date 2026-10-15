"""
core/jwt.py — JWT creation and validation utilities.

Pattern copied from bm-ts/backend/app/core/jwt.py. Tokens are HS256-signed
JWTs with the following custom claims in addition to the standard
``sub`` / ``iat`` / ``exp``:

  ``type`` — ``"access"`` or ``"refresh"`` (validated by
             ``ensure_token_type`` before use).

Access tokens expire after ``settings.access_token_expire_minutes`` (default 15 min).
Refresh tokens expire after ``settings.refresh_token_expire_days`` (default 30 days).

``iss``/``aud`` are ``"birthdaymessaging-mobile"`` — distinct from the extension
backend's ``"birthdaymessaging"`` so a token from one system is never accidentally
accepted by the other (they also use different signing secrets, so this is
belt-and-suspenders, not the only thing preventing cross-acceptance).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt
from jwt.exceptions import InvalidTokenError as JWTError  # re-export for dependents

from app.core.config import settings

_ISSUER = "birthdaymessaging-mobile"
_AUDIENCE = "birthdaymessaging-mobile"


class TokenType:
    """String constants for the ``type`` claim in JWTs."""

    ACCESS = "access"
    REFRESH = "refresh"


def _build_payload(
    subject: UUID,
    token_type: str,
    expires_delta: timedelta,
    session_id: UUID | None = None,
) -> dict[str, Any]:
    """Construct a standard JWT claims dict with ``sub``, ``type``, ``iat``, and ``exp``."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(subject),
        "type": token_type,
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "iat": now,
        "exp": now + expires_delta,
    }
    if session_id is not None:
        payload["sid"] = str(session_id)
    # Add a JTI to refresh tokens so they can be revoked individually.
    if token_type == TokenType.REFRESH:
        payload["jti"] = str(uuid4())
    return payload


def create_access_token(subject: UUID, session_id: UUID | None = None) -> str:
    """Return a signed short-lived access token for ``subject``."""
    expires = timedelta(minutes=settings.access_token_expire_minutes)
    payload = _build_payload(subject, TokenType.ACCESS, expires, session_id=session_id)
    return jwt.encode(
        payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
    )


def create_refresh_token(subject: UUID, session_id: UUID | None = None) -> str:
    """Return a signed long-lived refresh token for ``subject``."""
    expires = timedelta(days=settings.refresh_token_expire_days)
    payload = _build_payload(subject, TokenType.REFRESH, expires, session_id=session_id)
    return jwt.encode(
        payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
    )


def decode_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT, raising ``JWTError`` on any validation failure."""
    try:
        return jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            audience=_AUDIENCE,
            issuer=_ISSUER,
        )
    except jwt.InvalidTokenError as exc:
        raise JWTError("Token validation failed") from exc


def ensure_token_type(payload: dict[str, Any], expected_type: str) -> None:
    """Raise ``JWTError`` if the payload's ``type`` claim doesn't match ``expected_type``."""
    token_type = payload.get("type")
    if token_type != expected_type:
        raise JWTError("unexpected token type")
