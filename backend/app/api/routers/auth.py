"""
api/routers/auth.py — Authentication and profile endpoints.

All routes are mounted under ``/mobile/auth`` (set in main.py).

Endpoints
---------
POST  /mobile/auth/register                     Register a new account, send verification email.
POST  /mobile/auth/login                        Authenticate, return token pair.
POST  /mobile/auth/refresh                      Exchange a refresh token for a new token pair.
POST  /mobile/auth/logout                       Revoke the current session.
GET   /mobile/auth/me                           Return the current user's profile.
POST  /mobile/auth/request-email-verification   Resend the verification email.
POST  /mobile/auth/verify-email                 Consume a verification token.
POST  /mobile/auth/request-password-reset       Send a password-reset email.
POST  /mobile/auth/reset-password               Consume a reset token and set a new password.

Pattern copied from bm-ts/backend/app/api/routers/auth.py (session/rotation
logic, lockout, enumeration-safe password-reset responses). Deliberately
narrower than the extension backend's auth router for this milestone: no
session-listing/revoke-by-id, change-password, profile update, account
deletion, or data export yet — see the Milestone 1 handoff doc for what's
staged for the next batch.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, status, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_allow_unverified
from app.core.errors import AppError, ErrorCodes
from app.core.jwt import (
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    ensure_token_type,
)
from app.core.rate_limiter import limiter
from app.core.redis_client import invalidate_session_cache
from app.core.security import hash_password, verify_password
from app.db.models import RevokedToken, User, UserSession
from app.db.session import get_db
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    RefreshRequest,
    TokenResponse,
    UserProfile,
    UserProfileUpdate,
)
from app.schemas.common import ErrorResponse, MessageResponse
from app.schemas.email_verification import (
    RequestEmailVerificationRequest,
    VerifyEmailRequest,
)
from app.schemas.password_reset import (
    RequestPasswordResetRequest,
    ResetPasswordRequest,
)
from app.services.auth_tokens import (
    create_reset_token,
    create_verification_token,
    mark_reset_token_used,
    mark_verification_token_used,
    send_reset_email,
    send_verification_email,
    verify_reset_token,
    verify_verification_token,
)

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}

logger = logging.getLogger(__name__)

router = APIRouter(responses=_ERROR_RESPONSES)

_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_MINUTES = 15


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _coerce_expiry(expiry_value: Any) -> datetime:
    if isinstance(expiry_value, datetime):
        return (
            expiry_value if expiry_value.tzinfo else expiry_value.replace(tzinfo=timezone.utc)
        )
    return datetime.fromtimestamp(int(expiry_value), timezone.utc)


def _request_ip(request: Request | None) -> str | None:
    if request is None or request.client is None:
        return None
    return request.client.host


def _request_user_agent(request: Request | None) -> str | None:
    if request is None:
        return None
    user_agent = request.headers.get("user-agent")
    return user_agent[:500] if user_agent else None


async def _ensure_revoked_token(
    db: AsyncSession, *, jti: str | None, user_id: UUID, expires_at: datetime
) -> None:
    if not jti:
        return
    result = await db.execute(
        select(RevokedToken).where(RevokedToken.refresh_token_jti == jti)
    )
    if result.scalar_one_or_none() is None:
        db.add(
            RevokedToken(
                refresh_token_jti=jti,
                user_id=user_id,
                expires_at=expires_at,
                revoked_at=datetime.now(timezone.utc),
            )
        )


async def _issue_session_tokens(
    user: User,
    db: AsyncSession,
    request: Request | None,
    session: UserSession | None = None,
) -> tuple[TokenResponse, UserSession]:
    now = datetime.now(timezone.utc)
    if session is None:
        session = UserSession(
            user_id=user.id,
            current_refresh_token_jti="pending",
            ip_address=_request_ip(request),
            user_agent=_request_user_agent(request),
            created_at=now,
            last_seen_at=now,
            expires_at=now,
        )
        db.add(session)
        await db.flush()
    else:
        session.ip_address = _request_ip(request) or session.ip_address
        session.user_agent = _request_user_agent(request) or session.user_agent
        session.last_seen_at = now

    access_token = create_access_token(user.id, session.id)
    refresh_token = create_refresh_token(user.id, session.id)
    refresh_payload = decode_token(refresh_token)
    session.current_refresh_token_jti = refresh_payload["jti"]
    session.expires_at = _coerce_expiry(refresh_payload["exp"])

    return (
        TokenResponse(access_token=access_token, refresh_token=refresh_token),
        session,
    )


async def _revoke_session_record(
    session: UserSession, db: AsyncSession, *, reason: str
) -> None:
    if session.revoked_at is None:
        session.revoked_at = datetime.now(timezone.utc)
        session.revoked_reason = reason
    await _ensure_revoked_token(
        db,
        jti=session.current_refresh_token_jti,
        user_id=session.user_id,
        expires_at=session.expires_at,
    )
    # Bound how long a just-revoked session could still read as valid via
    # the Redis session-validity cache (core/redis_client.py) — best-effort,
    # the cache's own short TTL is the backstop if this call itself fails.
    await invalidate_session_cache(str(session.id))


@router.post(
    "/register", response_model=MessageResponse, status_code=status.HTTP_201_CREATED
)
@limiter.limit("5/minute")
async def register(
    request: Request, data: RegisterRequest, db: AsyncSession = Depends(get_db)
) -> MessageResponse:
    """Register a new account. Sends a verification email; the user must verify before logging in."""
    email = _normalize_email(data.email)
    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none():
        raise AppError(
            status.HTTP_400_BAD_REQUEST,
            ErrorCodes.EMAIL_ALREADY_REGISTERED,
            "Email already registered",
        )

    user = User(
        email=email,
        password_hash=hash_password(data.password),
        email_verified=False,
        failed_login_count=0,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    try:
        token = await create_verification_token(user, db)
        await db.commit()
        await send_verification_email(user.email, token)
    except Exception as exc:  # do not fail registration if email delivery fails
        logger.warning("Failed to send verification email to %s: %s", user.email, exc)

    return MessageResponse(
        message="Account created. Please check your email to verify your account before logging in."
    )


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
@limiter.limit("20/hour")
async def login(
    request: Request, data: LoginRequest, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    """Authenticate with email + password. Returns a token pair; 401 on invalid credentials.

    Accounts are locked for 15 minutes after 5 consecutive failed attempts.
    """
    email = _normalize_email(data.email)
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user and user.locked_until and user.locked_until > datetime.now(timezone.utc):
        raise AppError(
            status.HTTP_403_FORBIDDEN,
            ErrorCodes.ACCOUNT_LOCKED,
            "Account temporarily locked due to too many failed login attempts. Try again later.",
        )

    if not user or not verify_password(data.password, user.password_hash if user else ""):
        if not user:
            # Dummy verify to avoid a timing oracle that reveals whether the email exists.
            verify_password(
                data.password,
                "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$dummyhashdummyhashdummyhashdummy",
            )
        if user:
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if user.failed_login_count >= _MAX_FAILED_ATTEMPTS:
                user.locked_until = datetime.now(timezone.utc) + timedelta(
                    minutes=_LOCKOUT_MINUTES
                )
            await db.commit()
        raise AppError(
            status.HTTP_401_UNAUTHORIZED, ErrorCodes.INVALID_CREDENTIALS, "Invalid credentials"
        )

    if not user.email_verified:
        raise AppError(
            status.HTTP_403_FORBIDDEN,
            ErrorCodes.EMAIL_NOT_VERIFIED,
            "Please verify your email before logging in. Check your inbox for the verification link.",
        )

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = datetime.now(timezone.utc)
    response, _ = await _issue_session_tokens(user, db, request)
    await db.commit()
    return response


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("30/minute")
async def refresh(
    request: Request, data: RefreshRequest, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    """Exchange a valid refresh token for a fresh token pair; 401 on invalid/expired/reused token."""
    try:
        payload = decode_token(data.refresh_token)
        ensure_token_type(payload, TokenType.REFRESH)
        user_id = UUID(payload.get("sub"))
        jti = payload.get("jti")
        session_id = payload.get("sid")
        exp = payload.get("exp")
    except Exception as exc:
        raise AppError(
            status.HTTP_401_UNAUTHORIZED, ErrorCodes.INVALID_REFRESH_TOKEN, "Invalid refresh token"
        ) from exc

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise AppError(
            status.HTTP_401_UNAUTHORIZED, ErrorCodes.INVALID_REFRESH_TOKEN, "User no longer exists"
        )

    if session_id is None:
        raise AppError(
            status.HTTP_401_UNAUTHORIZED, ErrorCodes.INVALID_REFRESH_TOKEN, "Invalid refresh token"
        )

    try:
        session_uuid = UUID(str(session_id))
    except ValueError as exc:
        raise AppError(
            status.HTTP_401_UNAUTHORIZED, ErrorCodes.INVALID_REFRESH_TOKEN, "Invalid refresh token"
        ) from exc

    session_result = await db.execute(
        select(UserSession).where(UserSession.id == session_uuid, UserSession.user_id == user_id)
    )
    session = session_result.scalar_one_or_none()
    if session is None or session.revoked_at is not None:
        raise AppError(
            status.HTTP_401_UNAUTHORIZED, ErrorCodes.INVALID_REFRESH_TOKEN, "Invalid refresh token"
        )
    if session.current_refresh_token_jti != jti:
        # The jti on this session has already moved on — this refresh token was
        # already used once (or belongs to a session that's since rotated).
        raise AppError(
            status.HTTP_401_UNAUTHORIZED, ErrorCodes.INVALID_REFRESH_TOKEN, "Invalid refresh token"
        )

    if jti:
        revoked_result = await db.execute(
            select(RevokedToken).where(RevokedToken.refresh_token_jti == jti)
        )
        if revoked_result.scalar_one_or_none():
            raise AppError(
                status.HTTP_401_UNAUTHORIZED,
                ErrorCodes.INVALID_REFRESH_TOKEN,
                "Invalid refresh token",
            )

    expires_at = _coerce_expiry(exp)
    await _ensure_revoked_token(db, jti=jti, user_id=user_id, expires_at=expires_at)
    response, _ = await _issue_session_tokens(user, db, request, session=session)
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise AppError(
            status.HTTP_401_UNAUTHORIZED, ErrorCodes.INVALID_REFRESH_TOKEN, "Invalid refresh token"
        ) from exc

    return response


@router.post("/logout", response_model=MessageResponse)
async def logout(
    data: RefreshRequest, db: AsyncSession = Depends(get_db)
) -> MessageResponse:
    """Revoke (logout) a session by its refresh token.

    Mirrors the extension backend's behavior: the client clears its local
    tokens regardless of whether this call succeeds, so this endpoint always
    returns a clean 200/structured-error rather than anything the client
    needs to retry before it's safe to drop local state.
    """
    try:
        payload = decode_token(data.refresh_token)
        ensure_token_type(payload, TokenType.REFRESH)
        jti = payload.get("jti")
        user_id = UUID(payload.get("sub"))
        session_id = payload.get("sid")
        exp = payload.get("exp")
    except Exception:
        raise AppError(
            status.HTTP_401_UNAUTHORIZED, ErrorCodes.INVALID_REFRESH_TOKEN, "Invalid refresh token"
        )

    expires_at = _coerce_expiry(exp)

    # _revoke_session_record() already records this jti as revoked (it's the
    # session's current_refresh_token_jti, which is the same jti this request
    # decoded). Calling _ensure_revoked_token a second time for that same jti
    # here, unconditionally, inserts two RevokedToken rows with the same
    # unique refresh_token_jti in one transaction and fails at commit — so
    # the explicit fallback call below only runs when no session row was
    # found/revoked (jti wouldn't otherwise get recorded at all).
    session_revoked = False
    if session_id is not None:
        try:
            session_uuid = UUID(str(session_id))
        except ValueError:
            session_uuid = None
        if session_uuid is not None:
            session_result = await db.execute(
                select(UserSession).where(
                    UserSession.id == session_uuid, UserSession.user_id == user_id
                )
            )
            session = session_result.scalar_one_or_none()
            if session is not None:
                await _revoke_session_record(session, db, reason="logout")
                session_revoked = True

    if not session_revoked:
        await _ensure_revoked_token(db, jti=jti, user_id=user_id, expires_at=expires_at)
    await db.commit()
    return MessageResponse(message="Logged out")


@router.get("/me", response_model=UserProfile)
async def me(
    current_user: User = Depends(get_current_user_allow_unverified),
) -> UserProfile:
    """Return the profile of the currently authenticated user."""
    return UserProfile.model_validate(current_user)


@router.patch("/me", response_model=UserProfile)
async def update_me(
    data: UserProfileUpdate,
    current_user: User = Depends(get_current_user_allow_unverified),
    db: AsyncSession = Depends(get_db),
) -> UserProfile:
    """Partial update of the current user's name, timezone, and/or
    auto_send_email — a field left out of the payload is left unchanged.
    See UserProfileUpdate for why ``timezone`` is validated as a real
    IANA zone name, and what ``auto_send_email`` controls."""
    fields = data.model_dump(exclude_unset=True)
    if "name" in fields:
        current_user.name = fields["name"]
    if "timezone" in fields:
        current_user.timezone = fields["timezone"]
    if "auto_send_email" in fields:
        current_user.auto_send_email = fields["auto_send_email"]
    await db.commit()
    await db.refresh(current_user)
    return UserProfile.model_validate(current_user)


@router.post("/request-email-verification", response_model=MessageResponse)
@limiter.limit("5/hour")
async def request_email_verification(
    request: Request, data: RequestEmailVerificationRequest, db: AsyncSession = Depends(get_db)
) -> MessageResponse:
    """Resend a verification email. Always returns success (anti-enumeration)."""
    email = _normalize_email(data.email)
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user and not user.email_verified:
        try:
            token = await create_verification_token(user, db)
            await db.commit()
            await send_verification_email(user.email, token)
        except Exception as exc:
            logger.warning("Failed to resend verification email to %s: %s", email, exc)
    return MessageResponse(message="If that account exists, a verification email has been sent.")


@router.post("/verify-email", response_model=MessageResponse)
async def verify_email(
    data: VerifyEmailRequest, db: AsyncSession = Depends(get_db)
) -> MessageResponse:
    """Consume a verification token and mark the account's email verified."""
    row = await verify_verification_token(data.token, db)
    if row is None:
        raise AppError(
            status.HTTP_400_BAD_REQUEST,
            ErrorCodes.NOT_FOUND,
            "Invalid or expired verification token",
        )
    result = await db.execute(select(User).where(User.id == row.user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise AppError(
            status.HTTP_400_BAD_REQUEST,
            ErrorCodes.NOT_FOUND,
            "Invalid or expired verification token",
        )
    user.email_verified = True
    mark_verification_token_used(row)
    await db.commit()
    return MessageResponse(message="Email verified. You can now log in.")


@router.post("/request-password-reset", response_model=MessageResponse)
@limiter.limit("5/hour")
async def request_password_reset(
    request: Request, data: RequestPasswordResetRequest, db: AsyncSession = Depends(get_db)
) -> MessageResponse:
    """Send a password-reset email. Always returns success (anti-enumeration)."""
    email = _normalize_email(data.email)
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user:
        try:
            token = await create_reset_token(user, db)
            await db.commit()
            await send_reset_email(user.email, token)
        except Exception as exc:
            logger.warning("Failed to send reset email to %s: %s", email, exc)
    return MessageResponse(message="If that account exists, a password reset email has been sent.")


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(
    data: ResetPasswordRequest, db: AsyncSession = Depends(get_db)
) -> MessageResponse:
    """Consume a reset token, set a new password, and revoke all active sessions."""
    row = await verify_reset_token(data.token, db)
    if row is None:
        raise AppError(
            status.HTTP_400_BAD_REQUEST, ErrorCodes.NOT_FOUND, "Invalid or expired reset token"
        )
    result = await db.execute(select(User).where(User.id == row.user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise AppError(
            status.HTTP_400_BAD_REQUEST, ErrorCodes.NOT_FOUND, "Invalid or expired reset token"
        )

    user.password_hash = hash_password(data.new_password)
    user.failed_login_count = 0
    user.locked_until = None
    mark_reset_token_used(row)

    # Invalidate all active sessions — a reset means any existing tokens
    # were issued under the old password and should not keep working.
    sessions_result = await db.execute(
        select(UserSession).where(
            UserSession.user_id == user.id, UserSession.revoked_at.is_(None)
        )
    )
    for session in sessions_result.scalars().all():
        await _revoke_session_record(session, db, reason="password_reset")

    await db.commit()
    return MessageResponse(message="Password reset. Please log in with your new password.")
