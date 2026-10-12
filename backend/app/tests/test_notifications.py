"""Tests for push-token registration (app/api/routers/notifications.py)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.models import PushToken, User

pytestmark = pytest.mark.asyncio


async def _auth_headers(async_client: AsyncClient, db_session: AsyncSession) -> dict[str, str]:
    user = User(
        email="pushtokenuser@example.com",
        password_hash=hash_password("TestPass123!"),
        email_verified=True,
        failed_login_count=0,
    )
    db_session.add(user)
    await db_session.commit()

    login_resp = await async_client.post(
        "/mobile/auth/login",
        json={"email": "pushtokenuser@example.com", "password": "TestPass123!"},
    )
    assert login_resp.status_code == 200
    access_token = login_resp.json()["access_token"]
    return {"Authorization": f"Bearer {access_token}"}


async def test_register_push_token_requires_authentication(async_client: AsyncClient):
    resp = await async_client.post(
        "/mobile/notifications/register-token",
        json={"device_token": "abc123", "platform": "ios"},
    )
    assert resp.status_code == 401


async def test_register_push_token_creates_new_row(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)

    resp = await async_client.post(
        "/mobile/notifications/register-token",
        json={"device_token": "device-token-1", "platform": "ios"},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["device_token"] == "device-token-1"
    assert body["platform"] == "ios"

    result = await db_session.execute(
        select(PushToken).where(PushToken.device_token == "device-token-1")
    )
    rows = result.scalars().all()
    assert len(rows) == 1


async def test_register_push_token_upserts_on_repeat_registration(
    async_client: AsyncClient, db_session: AsyncSession
):
    """Re-registering the same (user, device_token) pair must update the
    existing row in place, not create a duplicate."""
    headers = await _auth_headers(async_client, db_session)

    first = await async_client.post(
        "/mobile/notifications/register-token",
        json={"device_token": "device-token-2", "platform": "android"},
        headers=headers,
    )
    assert first.status_code == 200
    first_id = first.json()["id"]

    second = await async_client.post(
        "/mobile/notifications/register-token",
        json={"device_token": "device-token-2", "platform": "android"},
        headers=headers,
    )
    assert second.status_code == 200
    assert second.json()["id"] == first_id

    result = await db_session.execute(
        select(PushToken).where(PushToken.device_token == "device-token-2")
    )
    rows = result.scalars().all()
    assert len(rows) == 1


async def test_register_push_token_rejects_unknown_platform(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)
    resp = await async_client.post(
        "/mobile/notifications/register-token",
        json={"device_token": "device-token-3", "platform": "blackberry"},
        headers=headers,
    )
    assert resp.status_code == 422
