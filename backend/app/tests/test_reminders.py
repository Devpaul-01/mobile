"""Tests for Reminder CRUD and the multi-select bulk-apply endpoint
(app/api/routers/reminders.py, app/services/reminders.py) — Milestone 3."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.models import Contact, ContactSource, User

pytestmark = pytest.mark.asyncio


async def _auth_headers(async_client: AsyncClient, db_session: AsyncSession) -> dict[str, str]:
    user = User(
        email="reminderuser@example.com",
        password_hash=hash_password("TestPass123!"),
        email_verified=True,
        failed_login_count=0,
    )
    db_session.add(user)
    await db_session.commit()

    login_resp = await async_client.post(
        "/mobile/auth/login",
        json={"email": "reminderuser@example.com", "password": "TestPass123!"},
    )
    assert login_resp.status_code == 200
    access_token = login_resp.json()["access_token"]
    return {"Authorization": f"Bearer {access_token}"}


async def _create_contact(db_session: AsyncSession, user_id, name: str = "Some Contact") -> Contact:
    contact = Contact(user_id=user_id, name=name, source=ContactSource.MANUAL)
    db_session.add(contact)
    await db_session.commit()
    await db_session.refresh(contact)
    return contact


async def _get_user_by_email(db_session: AsyncSession, email: str) -> User:
    result = await db_session.execute(select(User).where(User.email == email))
    return result.scalar_one()


# ---------------------------------------------------------------------------
# Single create / CRUD
# ---------------------------------------------------------------------------


async def test_create_reminder_requires_authentication(async_client: AsyncClient):
    resp = await async_client.post(
        "/mobile/reminders", json={"contact_id": "00000000-0000-0000-0000-000000000000"}
    )
    assert resp.status_code == 401


async def test_create_reminder_404s_for_contact_not_owned(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)

    other_user = User(
        email="otherreminderuser@example.com",
        password_hash=hash_password("TestPass123!"),
        email_verified=True,
        failed_login_count=0,
    )
    db_session.add(other_user)
    await db_session.flush()
    other_contact = await _create_contact(db_session, other_user.id, "Not Yours")

    resp = await async_client.post(
        "/mobile/reminders",
        json={"contact_id": str(other_contact.id)},
        headers=headers,
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "contact_not_found"


async def test_create_reminder_rejects_unknown_channel(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)
    resp = await async_client.post(
        "/mobile/reminders",
        json={"contact_id": "00000000-0000-0000-0000-000000000000", "channels": ["carrier_pigeon"]},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_create_list_get_update_delete_reminder_end_to_end(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)
    user = await _get_user_by_email(db_session, "reminderuser@example.com")
    contact = await _create_contact(db_session, user.id, "Reminder Target")

    create_resp = await async_client.post(
        "/mobile/reminders",
        json={"contact_id": str(contact.id), "lead_time_days": 3, "channels": ["push", "email"]},
        headers=headers,
    )
    assert create_resp.status_code == 201
    reminder_id = create_resp.json()["id"]
    assert create_resp.json()["lead_time_days"] == 3
    assert set(create_resp.json()["channels"]) == {"push", "email"}
    assert create_resp.json()["enabled"] is True

    list_resp = await async_client.get("/mobile/reminders", headers=headers)
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1

    get_resp = await async_client.get(f"/mobile/reminders/{reminder_id}", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == reminder_id

    update_resp = await async_client.patch(
        f"/mobile/reminders/{reminder_id}",
        json={"enabled": False, "lead_time_days": 7},
        headers=headers,
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["enabled"] is False
    assert update_resp.json()["lead_time_days"] == 7
    assert update_resp.json()["channels"] == ["push", "email"]  # untouched field preserved

    delete_resp = await async_client.delete(f"/mobile/reminders/{reminder_id}", headers=headers)
    assert delete_resp.status_code == 204

    after_delete = await async_client.get(f"/mobile/reminders/{reminder_id}", headers=headers)
    assert after_delete.status_code == 404
    assert after_delete.json()["code"] == "reminder_not_found"


# ---------------------------------------------------------------------------
# Bulk apply — the explicit feature request this milestone was built for
# ---------------------------------------------------------------------------


async def test_bulk_apply_creates_one_reminder_per_selected_contact(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)
    user = await _get_user_by_email(db_session, "reminderuser@example.com")

    contact_a = await _create_contact(db_session, user.id, "Contact A")
    contact_b = await _create_contact(db_session, user.id, "Contact B")
    contact_c = await _create_contact(db_session, user.id, "Contact C")

    resp = await async_client.post(
        "/mobile/reminders/bulk",
        json={
            "contact_ids": [str(contact_a.id), str(contact_b.id), str(contact_c.id)],
            "lead_time_days": 2,
            "channels": ["push"],
        },
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["created"]) == 3
    assert body["skipped_contact_ids"] == []
    created_contact_ids = {r["contact_id"] for r in body["created"]}
    assert created_contact_ids == {str(contact_a.id), str(contact_b.id), str(contact_c.id)}
    assert all(r["lead_time_days"] == 2 for r in body["created"])

    list_resp = await async_client.get("/mobile/reminders", headers=headers)
    assert list_resp.json()["total"] == 3


async def test_bulk_apply_skips_contacts_not_owned_without_failing_the_whole_call(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)
    user = await _get_user_by_email(db_session, "reminderuser@example.com")
    my_contact = await _create_contact(db_session, user.id, "Mine")

    other_user = User(
        email="otherbulkuser@example.com",
        password_hash=hash_password("TestPass123!"),
        email_verified=True,
        failed_login_count=0,
    )
    db_session.add(other_user)
    await db_session.flush()
    not_mine = await _create_contact(db_session, other_user.id, "Not Mine")

    resp = await async_client.post(
        "/mobile/reminders/bulk",
        json={"contact_ids": [str(my_contact.id), str(not_mine.id)], "channels": ["push"]},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["created"]) == 1
    assert body["created"][0]["contact_id"] == str(my_contact.id)
    assert body["skipped_contact_ids"] == [str(not_mine.id)]


async def test_bulk_apply_rejects_empty_contact_list(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)
    resp = await async_client.post(
        "/mobile/reminders/bulk",
        json={"contact_ids": [], "channels": ["push"]},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_bulk_apply_requires_authentication(async_client: AsyncClient):
    resp = await async_client.post(
        "/mobile/reminders/bulk",
        json={"contact_ids": ["00000000-0000-0000-0000-000000000000"]},
    )
    assert resp.status_code == 401
