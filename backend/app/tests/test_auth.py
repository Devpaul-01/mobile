"""
test_auth.py — Auth lifecycle tests.

Pattern adapted from bm-ts/backend/app/tests/test_auth.py, scoped to this
milestone's endpoints: register, login (+ lockout), refresh (+ rotation
reuse-detection), logout, me, verify-email, password reset.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EmailVerificationToken, PasswordResetToken, User

pytestmark = pytest.mark.asyncio


async def _register_and_verify(client: AsyncClient, db_session: AsyncSession, email: str, password: str) -> User:
    resp = await client.post("/mobile/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text

    result = await db_session.execute(select(User).where(User.email == email))
    user = result.scalar_one()

    token_result = await db_session.execute(
        select(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id)
    )
    token_row = token_result.scalar_one()

    verify_resp = await client.post("/mobile/auth/verify-email", json={"token": token_row.token})
    assert verify_resp.status_code == 200, verify_resp.text

    await db_session.refresh(user)
    return user


async def test_register_creates_unverified_user(async_client: AsyncClient, db_session: AsyncSession):
    resp = await async_client.post(
        "/mobile/auth/register", json={"email": "new@example.com", "password": "TestPass123!"}
    )
    assert resp.status_code == 201

    result = await db_session.execute(select(User).where(User.email == "new@example.com"))
    user = result.scalar_one()
    assert user.email_verified is False


async def test_register_duplicate_email_rejected(async_client: AsyncClient, db_session: AsyncSession):
    await _register_and_verify(async_client, db_session, "dup@example.com", "TestPass123!")
    resp = await async_client.post(
        "/mobile/auth/register", json={"email": "dup@example.com", "password": "AnotherPass123!"}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "email_already_registered"


async def test_register_email_is_case_insensitive_for_dedup(
    async_client: AsyncClient, db_session: AsyncSession
):
    await _register_and_verify(async_client, db_session, "case@example.com", "TestPass123!")
    resp = await async_client.post(
        "/mobile/auth/register", json={"email": "Case@Example.com", "password": "AnotherPass123!"}
    )
    assert resp.status_code == 400


async def test_login_rejects_unverified_user(async_client: AsyncClient, db_session: AsyncSession):
    await async_client.post(
        "/mobile/auth/register", json={"email": "unverified@example.com", "password": "TestPass123!"}
    )
    resp = await async_client.post(
        "/mobile/auth/login", json={"email": "unverified@example.com", "password": "TestPass123!"}
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "email_not_verified"


async def test_login_success_returns_token_pair(async_client: AsyncClient, db_session: AsyncSession):
    await _register_and_verify(async_client, db_session, "verified@example.com", "TestPass123!")
    resp = await async_client.post(
        "/mobile/auth/login", json={"email": "verified@example.com", "password": "TestPass123!"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["token_type"] == "bearer"


async def test_login_wrong_password_rejected(async_client: AsyncClient, db_session: AsyncSession):
    await _register_and_verify(async_client, db_session, "wrongpw@example.com", "TestPass123!")
    resp = await async_client.post(
        "/mobile/auth/login", json={"email": "wrongpw@example.com", "password": "WrongPass123!"}
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"


async def test_login_unknown_email_rejected(async_client: AsyncClient):
    resp = await async_client.post(
        "/mobile/auth/login", json={"email": "nobody@example.com", "password": "whatever123"}
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"


async def test_account_locks_after_five_failed_attempts(
    async_client: AsyncClient, db_session: AsyncSession
):
    """Checks the lockout flag directly in the DB rather than via a 6th HTTP
    call — login is itself rate-limited at 5/minute (same IP in tests), so a
    6th call in the same test would 429 before ever reaching the lockout
    check. That's expected production behavior (both guards are IP/account
    keyed and both trip around the same attempt count), just not something
    to also assert through the rate limiter in this test."""
    await _register_and_verify(async_client, db_session, "lockout@example.com", "TestPass123!")

    for _ in range(5):
        resp = await async_client.post(
            "/mobile/auth/login", json={"email": "lockout@example.com", "password": "WrongPass!"}
        )
        assert resp.status_code == 401

    result = await db_session.execute(select(User).where(User.email == "lockout@example.com"))
    user = result.scalar_one()
    await db_session.refresh(user)
    assert user.failed_login_count == 5
    assert user.locked_until is not None


async def test_refresh_returns_new_token_pair(async_client: AsyncClient, db_session: AsyncSession):
    await _register_and_verify(async_client, db_session, "refresh@example.com", "TestPass123!")
    login_resp = await async_client.post(
        "/mobile/auth/login", json={"email": "refresh@example.com", "password": "TestPass123!"}
    )
    refresh_token = login_resp.json()["refresh_token"]

    resp = await async_client.post("/mobile/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 200
    new_tokens = resp.json()
    assert new_tokens["refresh_token"] != refresh_token


async def test_refresh_token_reuse_is_rejected(async_client: AsyncClient, db_session: AsyncSession):
    """A refresh token can only be used once — using it again (e.g. after it
    was already rotated) must fail, not silently issue another pair."""
    await _register_and_verify(async_client, db_session, "reuse@example.com", "TestPass123!")
    login_resp = await async_client.post(
        "/mobile/auth/login", json={"email": "reuse@example.com", "password": "TestPass123!"}
    )
    refresh_token = login_resp.json()["refresh_token"]

    first = await async_client.post("/mobile/auth/refresh", json={"refresh_token": refresh_token})
    assert first.status_code == 200

    second = await async_client.post("/mobile/auth/refresh", json={"refresh_token": refresh_token})
    assert second.status_code == 401
    assert second.json()["code"] == "invalid_refresh_token"


async def test_me_requires_authentication(async_client: AsyncClient):
    resp = await async_client.get("/mobile/auth/me")
    assert resp.status_code == 401


async def test_me_returns_profile_for_valid_token(async_client: AsyncClient, db_session: AsyncSession):
    await _register_and_verify(async_client, db_session, "profile@example.com", "TestPass123!")
    login_resp = await async_client.post(
        "/mobile/auth/login", json={"email": "profile@example.com", "password": "TestPass123!"}
    )
    access_token = login_resp.json()["access_token"]

    resp = await async_client.get(
        "/mobile/auth/me", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["email"] == "profile@example.com"
    assert resp.json()["email_verified"] is True


async def test_logout_revokes_refresh_token(async_client: AsyncClient, db_session: AsyncSession):
    await _register_and_verify(async_client, db_session, "logout@example.com", "TestPass123!")
    login_resp = await async_client.post(
        "/mobile/auth/login", json={"email": "logout@example.com", "password": "TestPass123!"}
    )
    refresh_token = login_resp.json()["refresh_token"]

    logout_resp = await async_client.post("/mobile/auth/logout", json={"refresh_token": refresh_token})
    assert logout_resp.status_code == 200

    refresh_resp = await async_client.post("/mobile/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh_resp.status_code == 401


async def test_password_reset_flow(async_client: AsyncClient, db_session: AsyncSession):
    await _register_and_verify(async_client, db_session, "reset@example.com", "OldPass123!")

    req_resp = await async_client.post(
        "/mobile/auth/request-password-reset", json={"email": "reset@example.com"}
    )
    assert req_resp.status_code == 200

    result = await db_session.execute(select(User).where(User.email == "reset@example.com"))
    user = result.scalar_one()
    token_result = await db_session.execute(
        select(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
    )
    token_row = token_result.scalar_one()

    reset_resp = await async_client.post(
        "/mobile/auth/reset-password",
        json={"token": token_row.token, "new_password": "NewPass456!"},
    )
    assert reset_resp.status_code == 200

    old_login = await async_client.post(
        "/mobile/auth/login", json={"email": "reset@example.com", "password": "OldPass123!"}
    )
    assert old_login.status_code == 401

    new_login = await async_client.post(
        "/mobile/auth/login", json={"email": "reset@example.com", "password": "NewPass456!"}
    )
    assert new_login.status_code == 200


async def test_password_reset_invalidates_existing_sessions(
    async_client: AsyncClient, db_session: AsyncSession
):
    await _register_and_verify(async_client, db_session, "sessions@example.com", "OldPass123!")
    login_resp = await async_client.post(
        "/mobile/auth/login", json={"email": "sessions@example.com", "password": "OldPass123!"}
    )
    refresh_token = login_resp.json()["refresh_token"]

    result = await db_session.execute(select(User).where(User.email == "sessions@example.com"))
    user = result.scalar_one()
    await db_session.execute(
        select(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
    )

    from app.services.auth_tokens import create_reset_token

    reset_token = await create_reset_token(user, db_session)
    await db_session.commit()

    reset_resp = await async_client.post(
        "/mobile/auth/reset-password",
        json={"token": reset_token, "new_password": "NewPass456!"},
    )
    assert reset_resp.status_code == 200

    refresh_resp = await async_client.post("/mobile/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh_resp.status_code == 401


async def test_reset_password_with_invalid_token_rejected(async_client: AsyncClient):
    resp = await async_client.post(
        "/mobile/auth/reset-password",
        json={"token": "not-a-real-token", "new_password": "NewPass456!"},
    )
    assert resp.status_code == 400


async def test_request_email_verification_resends_token_for_unverified_user(
    async_client: AsyncClient, db_session: AsyncSession
):
    await async_client.post(
        "/mobile/auth/register", json={"email": "resend@example.com", "password": "TestPass123!"}
    )
    resp = await async_client.post(
        "/mobile/auth/request-email-verification", json={"email": "resend@example.com"}
    )
    assert resp.status_code == 200

    result = await db_session.execute(select(User).where(User.email == "resend@example.com"))
    user = result.scalar_one()
    tokens_result = await db_session.execute(
        select(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id)
    )
    # register() already created one token; request-email-verification should add a second.
    assert len(tokens_result.scalars().all()) == 2


async def test_request_email_verification_does_not_reveal_unknown_email(async_client: AsyncClient):
    resp = await async_client.post(
        "/mobile/auth/request-email-verification", json={"email": "doesnotexist@example.com"}
    )
    assert resp.status_code == 200


async def test_request_password_reset_does_not_reveal_unknown_email(async_client: AsyncClient):
    """Anti-enumeration: same response whether or not the email exists."""
    resp = await async_client.post(
        "/mobile/auth/request-password-reset", json={"email": "doesnotexist@example.com"}
    )
    assert resp.status_code == 200
