"""Tests for GDPR export / account deletion (§27) —
app/services/account.py, app/api/routers/account.py.

Matches this codebase's established mocking style: storage deletion is
monkeypatched at app.services.storage._get_client with a small fake
client (see test_uploads.py), extended here with list_objects_v2/
delete_objects support since account deletion does a prefix delete
rather than single-object operations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.models import (
    ApprovalChannel,
    ApprovalState,
    Contact,
    ContactIdentifier,
    ContactIdentifierType,
    ContactPhoto,
    ContactSource,
    PendingApproval,
    Payment,
    PaymentProvider,
    PaymentStatus,
    PushPlatform,
    PushToken,
    Reminder,
    ReminderOccurrence,
    Subscription,
    SubscriptionStatus,
    User,
)

pytestmark = pytest.mark.asyncio


class _FakeStorageClient:
    """Supports only what account deletion needs: listing and bulk-
    deleting objects under a prefix."""

    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.deleted: list[str] = []

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        matching = [key for key in self.objects if key.startswith(Prefix)]
        return {"Contents": [{"Key": key} for key in matching], "IsTruncated": False}

    def delete_objects(self, *, Bucket, Delete):
        for obj in Delete["Objects"]:
            self.objects.pop(obj["Key"], None)
            self.deleted.append(obj["Key"])


@pytest.fixture
def fake_storage(monkeypatch):
    client = _FakeStorageClient()
    monkeypatch.setattr("app.services.storage._get_client", lambda: client)
    from app.core.config import settings

    monkeypatch.setattr(settings, "storage_endpoint_url", "https://fake-r2.example.com")
    monkeypatch.setattr(settings, "storage_bucket_name", "fake-bucket")
    monkeypatch.setattr(settings, "storage_access_key_id", "fake-key-id")
    monkeypatch.setattr(settings, "storage_secret_access_key", "fake-secret")
    return client


async def _register_user(
    async_client: AsyncClient, db_session: AsyncSession, email: str, password: str = "TestPass123!"
) -> tuple[dict, User]:
    user = User(
        email=email,
        password_hash=hash_password(password),
        email_verified=True,
        failed_login_count=0,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    login_resp = await async_client.post(
        "/mobile/auth/login", json={"email": email, "password": password}
    )
    assert login_resp.status_code == 200
    headers = {"Authorization": f"Bearer {login_resp.json()['access_token']}"}
    return headers, user


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


async def test_export_requires_authentication(async_client: AsyncClient):
    resp = await async_client.get("/mobile/account/export")
    assert resp.status_code == 401


async def test_export_covers_every_owned_entity_type(
    async_client: AsyncClient, db_session: AsyncSession, fake_storage
):
    headers, user = await _register_user(async_client, db_session, "exportme@example.com")

    contact = Contact(user_id=user.id, name="Ada", source=ContactSource.MANUAL, notes="friend")
    db_session.add(contact)
    await db_session.flush()
    db_session.add(
        ContactIdentifier(
            contact_id=contact.id,
            type=ContactIdentifierType.EMAIL,
            normalized_value="ada@example.com",
            raw_value="Ada@Example.com",
        )
    )
    db_session.add(
        ContactPhoto(contact_id=contact.id, object_key=f"{user.id}/contacts/ada.jpg",
                     content_type="image/jpeg", size_bytes=1024)
    )
    await db_session.flush()

    reminder = Reminder(
        contact_id=contact.id, user_id=user.id, lead_time_days=3, channels=["push"]
    )
    db_session.add(reminder)
    await db_session.flush()
    occurrence = ReminderOccurrence(reminder_id=reminder.id, occurrence_year=2026)
    db_session.add(occurrence)
    await db_session.flush()

    approval = PendingApproval(
        reminder_occurrence_id=occurrence.id,
        user_id=user.id,
        contact_id=contact.id,
        channel=ApprovalChannel.WHATSAPP,
        state=ApprovalState.PENDING_APPROVAL,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db_session.add(approval)
    await db_session.flush()

    subscription = Subscription(
        user_id=user.id,
        payment_provider=PaymentProvider.PAYPAL,
        plan="monthly",
        status=SubscriptionStatus.ACTIVE,
        current_period_end=datetime.now(timezone.utc) + timedelta(days=30),
    )
    db_session.add(subscription)
    await db_session.flush()
    db_session.add(
        Payment(
            subscription_id=subscription.id,
            provider_order_id="ORDER-EXPORT-1",
            amount=4.99,
            currency="USD",
            status=PaymentStatus.COMPLETED,
        )
    )
    await db_session.commit()

    resp = await async_client.get("/mobile/account/export", headers=headers)
    assert resp.status_code == 200
    body = resp.json()

    assert body["profile"]["email"] == "exportme@example.com"

    assert len(body["contacts"]) == 1
    exported_contact = body["contacts"][0]
    assert exported_contact["name"] == "Ada"
    assert len(exported_contact["identifiers"]) == 1
    assert exported_contact["identifiers"][0]["normalized_value"] == "ada@example.com"
    assert exported_contact["photo"]["content_type"] == "image/jpeg"

    assert len(body["reminders"]) == 1
    assert body["reminders"][0]["occurrences"][0]["occurrence_year"] == 2026

    assert len(body["approvals"]) == 1
    assert body["approvals"][0]["state"] == "pending_approval"

    assert body["subscription"]["plan"] == "monthly"
    assert body["subscription"]["status"] == "active"
    assert len(body["subscription"]["payments"]) == 1
    assert body["subscription"]["payments"][0]["status"] == "completed"
    # §27.4 — no raw PayPal identifiers in the export.
    assert "provider_order_id" not in body["subscription"]["payments"][0]
    assert "provider_subscription_id" not in body["subscription"]


async def test_export_subscription_is_null_when_never_subscribed(
    async_client: AsyncClient, db_session: AsyncSession, fake_storage
):
    headers, _user = await _register_user(async_client, db_session, "nosub@example.com")
    resp = await async_client.get("/mobile/account/export", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["subscription"] is None
    assert resp.json()["contacts"] == []


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


async def test_delete_requires_authentication(async_client: AsyncClient):
    resp = await async_client.request(
        "DELETE", "/mobile/account", json={"password": "whatever"}
    )
    assert resp.status_code == 401


async def test_delete_rejects_wrong_password(
    async_client: AsyncClient, db_session: AsyncSession, fake_storage
):
    headers, _user = await _register_user(async_client, db_session, "wrongpass@example.com")
    resp = await async_client.request(
        "DELETE",
        "/mobile/account",
        json={"password": "DefinitelyNotIt1!"},
        headers=headers,
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "wrong_password"


async def test_delete_cascades_everything_and_removes_storage_objects(
    async_client: AsyncClient, db_session: AsyncSession, fake_storage
):
    headers, user = await _register_user(
        async_client, db_session, "deleteme@example.com", "CorrectPass1!"
    )

    contact = Contact(user_id=user.id, name="Bo", source=ContactSource.MANUAL)
    db_session.add(contact)
    await db_session.flush()
    photo_key = f"{user.id}/contacts/bo.jpg"
    db_session.add(
        ContactPhoto(contact_id=contact.id, object_key=photo_key, content_type="image/jpeg", size_bytes=10)
    )
    db_session.add(
        PushToken(user_id=user.id, device_token="device-1", platform=PushPlatform.IOS)
    )
    await db_session.commit()

    # Put a matching object in fake storage so the prefix-delete has
    # something real to remove, plus an unrelated object that must survive.
    fake_storage.objects[photo_key] = {}
    fake_storage.objects["someone-else/contacts/other.jpg"] = {}

    resp = await async_client.request(
        "DELETE",
        "/mobile/account",
        json={"password": "CorrectPass1!"},
        headers=headers,
    )
    assert resp.status_code == 200

    # The user row (and everything FK-cascaded from it) is gone.
    result = await db_session.execute(select(User).where(User.id == user.id))
    assert result.scalar_one_or_none() is None

    result = await db_session.execute(select(Contact).where(Contact.user_id == user.id))
    assert result.scalar_one_or_none() is None

    result = await db_session.execute(
        select(ContactPhoto).where(ContactPhoto.contact_id == contact.id)
    )
    assert result.scalar_one_or_none() is None

    # Storage cleanup: this user's object gone, someone else's untouched.
    assert photo_key not in fake_storage.objects
    assert "someone-else/contacts/other.jpg" in fake_storage.objects

    # The deleted account can no longer authenticate.
    login_resp = await async_client.post(
        "/mobile/auth/login",
        json={"email": "deleteme@example.com", "password": "CorrectPass1!"},
    )
    assert login_resp.status_code == 401


async def test_delete_works_when_storage_not_configured(
    async_client: AsyncClient, db_session: AsyncSession
):
    """No fake_storage fixture here — deletion must still succeed for a
    user with no photos even when object storage isn't configured at all
    (app/services/storage.py delete_objects_by_prefix no-ops rather than
    raising in that case, unlike the rest of that module's fail-loud
    stance — see its docstring)."""
    headers, _user = await _register_user(
        async_client, db_session, "nostorage@example.com", "CorrectPass1!"
    )
    resp = await async_client.request(
        "DELETE",
        "/mobile/account",
        json={"password": "CorrectPass1!"},
        headers=headers,
    )
    assert resp.status_code == 200
