"""Tests for contact-photo upload (§26) — app/services/storage.py and
app/api/routers/uploads.py.

Rather than mocking S3 over the network (moto, a real test bucket), these
tests monkeypatch app.services.storage._get_client to return a small fake
object implementing just the three boto3 methods this module calls
(generate_presigned_post, head_object, generate_presigned_url,
delete_object) — matching this codebase's existing style of monkeypatching
at the function/module boundary (see test_scheduler.py's email/push
monkeypatches) rather than pulling in a storage-emulation dependency."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.models import Contact, ContactSource, User

pytestmark = pytest.mark.asyncio


class _FakeStorageClient:
    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.deleted: list[str] = []

    def generate_presigned_post(self, *, Bucket, Key, Fields, Conditions, ExpiresIn):
        return {
            "url": f"https://fake-r2.example.com/{Bucket}",
            "fields": {**Fields, "key": Key},
        }

    def head_object(self, *, Bucket, Key):
        obj = self.objects.get(Key)
        if obj is None:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ContentType": obj["content_type"], "ContentLength": obj["size_bytes"]}

    def delete_object(self, *, Bucket, Key):
        self.objects.pop(Key, None)
        self.deleted.append(Key)

    def generate_presigned_url(self, operation_name, *, Params, ExpiresIn):
        return f"https://fake-r2.example.com/{Params['Bucket']}/{Params['Key']}?sig=fake"


@pytest.fixture
def fake_storage(monkeypatch):
    client = _FakeStorageClient()
    monkeypatch.setattr("app.services.storage._get_client", lambda: client)
    return client


async def _auth_headers(async_client: AsyncClient, db_session: AsyncSession) -> dict[str, str]:
    user = User(
        email="uploaduser@example.com",
        password_hash=hash_password("TestPass123!"),
        email_verified=True,
        failed_login_count=0,
    )
    db_session.add(user)
    await db_session.commit()

    login_resp = await async_client.post(
        "/mobile/auth/login",
        json={"email": "uploaduser@example.com", "password": "TestPass123!"},
    )
    assert login_resp.status_code == 200
    access_token = login_resp.json()["access_token"]
    return {"Authorization": f"Bearer {access_token}"}, user


async def _create_contact(db_session: AsyncSession, user_id, name: str = "Photo Target") -> Contact:
    contact = Contact(user_id=user_id, name=name, source=ContactSource.MANUAL)
    db_session.add(contact)
    await db_session.commit()
    await db_session.refresh(contact)
    return contact


# ---------------------------------------------------------------------------
# Presign
# ---------------------------------------------------------------------------


async def test_presign_requires_authentication(async_client: AsyncClient, fake_storage):
    resp = await async_client.post(
        "/mobile/uploads/presign", json={"content_type": "image/png"}
    )
    assert resp.status_code == 401


async def test_presign_rejects_unsupported_content_type(
    async_client: AsyncClient, db_session: AsyncSession, fake_storage
):
    headers, _ = await _auth_headers(async_client, db_session)
    resp = await async_client.post(
        "/mobile/uploads/presign", json={"content_type": "application/pdf"}, headers=headers
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_content_type"


async def test_presign_rejects_unsupported_purpose(
    async_client: AsyncClient, db_session: AsyncSession, fake_storage
):
    headers, _ = await _auth_headers(async_client, db_session)
    resp = await async_client.post(
        "/mobile/uploads/presign",
        json={"content_type": "image/png", "purpose": "banner"},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_presign_returns_upload_url_and_object_key(
    async_client: AsyncClient, db_session: AsyncSession, fake_storage
):
    headers, _ = await _auth_headers(async_client, db_session)
    resp = await async_client.post(
        "/mobile/uploads/presign", json={"content_type": "image/jpeg"}, headers=headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object_key"].endswith(".jpg")
    assert "fields" in body
    assert "expires_at" in body


# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------


async def test_confirm_404s_for_contact_not_owned(
    async_client: AsyncClient, db_session: AsyncSession, fake_storage
):
    headers, _ = await _auth_headers(async_client, db_session)

    other_user = User(
        email="otheruploaduser@example.com",
        password_hash=hash_password("TestPass123!"),
        email_verified=True,
        failed_login_count=0,
    )
    db_session.add(other_user)
    await db_session.flush()
    other_contact = await _create_contact(db_session, other_user.id, "Not Yours")

    resp = await async_client.post(
        "/mobile/uploads/confirm",
        json={"object_key": "whatever.jpg", "contact_id": str(other_contact.id)},
        headers=headers,
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "contact_not_found"


async def test_confirm_rejects_object_not_found_in_storage(
    async_client: AsyncClient, db_session: AsyncSession, fake_storage
):
    headers, user = await _auth_headers(async_client, db_session)
    contact = await _create_contact(db_session, user.id)

    resp = await async_client.post(
        "/mobile/uploads/confirm",
        json={"object_key": "never-uploaded.jpg", "contact_id": str(contact.id)},
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "upload_not_found"


async def test_confirm_rejects_oversized_object_and_deletes_it(
    async_client: AsyncClient, db_session: AsyncSession, fake_storage
):
    headers, user = await _auth_headers(async_client, db_session)
    contact = await _create_contact(db_session, user.id)

    key = "oversized.jpg"
    fake_storage.objects[key] = {"content_type": "image/jpeg", "size_bytes": 6 * 1024 * 1024}

    resp = await async_client.post(
        "/mobile/uploads/confirm",
        json={"object_key": key, "contact_id": str(contact.id)},
        headers=headers,
    )
    assert resp.status_code == 400
    assert key in fake_storage.deleted


async def test_confirm_creates_contact_photo_on_valid_object(
    async_client: AsyncClient, db_session: AsyncSession, fake_storage
):
    headers, user = await _auth_headers(async_client, db_session)
    contact = await _create_contact(db_session, user.id)

    key = "valid.png"
    fake_storage.objects[key] = {"content_type": "image/png", "size_bytes": 1024}

    resp = await async_client.post(
        "/mobile/uploads/confirm",
        json={"object_key": key, "contact_id": str(contact.id)},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["contact_id"] == str(contact.id)
    assert body["content_type"] == "image/png"
    assert body["size_bytes"] == 1024
    assert "view_url" in body and body["view_url"]


async def test_confirm_replaces_existing_photo_for_same_contact(
    async_client: AsyncClient, db_session: AsyncSession, fake_storage
):
    headers, user = await _auth_headers(async_client, db_session)
    contact = await _create_contact(db_session, user.id)

    first_key = "first.png"
    fake_storage.objects[first_key] = {"content_type": "image/png", "size_bytes": 1024}
    first_resp = await async_client.post(
        "/mobile/uploads/confirm",
        json={"object_key": first_key, "contact_id": str(contact.id)},
        headers=headers,
    )
    assert first_resp.status_code == 200

    second_key = "second.webp"
    fake_storage.objects[second_key] = {"content_type": "image/webp", "size_bytes": 2048}
    second_resp = await async_client.post(
        "/mobile/uploads/confirm",
        json={"object_key": second_key, "contact_id": str(contact.id)},
        headers=headers,
    )
    assert second_resp.status_code == 200
    assert second_resp.json()["content_type"] == "image/webp"

    # Old object deleted from storage, and only one ContactPhoto row remains.
    assert first_key in fake_storage.deleted
    get_resp = await async_client.get(
        f"/mobile/uploads/contact-photo/{contact.id}", headers=headers
    )
    assert get_resp.json()["content_type"] == "image/webp"


# ---------------------------------------------------------------------------
# Get / Delete
# ---------------------------------------------------------------------------


async def test_get_contact_photo_404s_when_none_exists(
    async_client: AsyncClient, db_session: AsyncSession, fake_storage
):
    headers, user = await _auth_headers(async_client, db_session)
    contact = await _create_contact(db_session, user.id)

    resp = await async_client.get(
        f"/mobile/uploads/contact-photo/{contact.id}", headers=headers
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "contact_photo_not_found"


async def test_delete_contact_photo_removes_row_and_object(
    async_client: AsyncClient, db_session: AsyncSession, fake_storage
):
    headers, user = await _auth_headers(async_client, db_session)
    contact = await _create_contact(db_session, user.id)

    key = "to-delete.jpg"
    fake_storage.objects[key] = {"content_type": "image/jpeg", "size_bytes": 500}
    confirm_resp = await async_client.post(
        "/mobile/uploads/confirm",
        json={"object_key": key, "contact_id": str(contact.id)},
        headers=headers,
    )
    assert confirm_resp.status_code == 200

    delete_resp = await async_client.delete(
        f"/mobile/uploads/contact-photo/{contact.id}", headers=headers
    )
    assert delete_resp.status_code == 204
    assert key in fake_storage.deleted

    get_resp = await async_client.get(
        f"/mobile/uploads/contact-photo/{contact.id}", headers=headers
    )
    assert get_resp.status_code == 404


# ---------------------------------------------------------------------------
# Not configured
# ---------------------------------------------------------------------------


async def test_presign_fails_loudly_when_storage_not_configured(
    async_client: AsyncClient, db_session: AsyncSession
):
    """No STORAGE_* env vars are set in the test environment (see
    conftest.py) and this test deliberately does NOT use the fake_storage
    fixture — confirming the real, unconfigured path fails with a clear
    500 rather than silently no-op'ing (object storage has no sensible
    degraded mode, unlike Redis)."""
    import app.services.storage as storage_module

    storage_module._client = None
    storage_module._client_init_attempted = False

    headers, _ = await _auth_headers(async_client, db_session)
    resp = await async_client.post(
        "/mobile/uploads/presign", json={"content_type": "image/png"}, headers=headers
    )
    assert resp.status_code == 500
    assert resp.json()["code"] == "storage_not_configured"

    storage_module._client = None
    storage_module._client_init_attempted = False
