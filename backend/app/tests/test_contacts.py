"""Tests for contact import/dedup (app/services/dedup.py) and the
/mobile/contacts CRUD endpoints (app/api/routers/contacts.py)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.models import Contact, ContactIdentifier, ContactSource, User
from app.schemas.contact import ContactImportBirthday, ContactImportItem, ContactImportPhone
from app.services.dedup import import_contacts

pytestmark = pytest.mark.asyncio


async def _get_identifiers(db_session: AsyncSession, contact_id) -> list[ContactIdentifier]:
    result = await db_session.execute(
        select(ContactIdentifier).where(ContactIdentifier.contact_id == contact_id)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Dedup service — unit level
# ---------------------------------------------------------------------------


async def test_import_creates_new_contact_with_identifiers(
    db_session: AsyncSession, test_user: User
):
    item = ContactImportItem(
        name="Ada Lovelace",
        phones=[ContactImportPhone(value="+15550001111")],
        emails=["ada@example.com"],
        birthday=ContactImportBirthday(month=12, day=10, year=1920),
        device_contact_id="device-1",
    )

    response = await import_contacts(test_user.id, [item], db_session)
    await db_session.commit()

    assert response.imported == 1
    assert response.updated == 0
    assert response.requires_review == []

    result = await db_session.execute(select(Contact).where(Contact.user_id == test_user.id))
    contact = result.scalar_one()
    assert contact.name == "Ada Lovelace"
    assert contact.birthday_month == 12
    assert contact.birthday_year == 1920
    assert contact.source == ContactSource.DEVICE_IMPORT

    identifiers = await _get_identifiers(db_session, contact.id)
    assert {i.normalized_value for i in identifiers} == {"+15550001111", "ada@example.com"}


async def test_import_dedupes_by_phone_on_second_import(
    db_session: AsyncSession, test_user: User
):
    item = ContactImportItem(
        name="Grace Hopper",
        phones=[ContactImportPhone(value="+15550002222")],
    )
    await import_contacts(test_user.id, [item], db_session)
    await db_session.commit()

    second_response = await import_contacts(test_user.id, [item], db_session)
    await db_session.commit()

    assert second_response.imported == 0
    assert second_response.updated == 1

    result = await db_session.execute(select(Contact).where(Contact.user_id == test_user.id))
    contacts = result.scalars().all()
    assert len(contacts) == 1


async def test_import_flags_multiple_distinct_matches_for_review(
    db_session: AsyncSession, test_user: User
):
    await import_contacts(
        test_user.id,
        [
            ContactImportItem(name="Contact A", phones=[ContactImportPhone(value="+15550003333")]),
            ContactImportItem(name="Contact B", emails=["contactb@example.com"]),
        ],
        db_session,
    )
    await db_session.commit()

    ambiguous_item = ContactImportItem(
        name="Merged Person",
        phones=[ContactImportPhone(value="+15550003333")],
        emails=["contactb@example.com"],
    )
    response = await import_contacts(test_user.id, [ambiguous_item], db_session)
    await db_session.commit()

    assert response.imported == 0
    assert response.updated == 0
    assert len(response.requires_review) == 1
    assert len(response.requires_review[0].matched_contact_ids) == 2

    result = await db_session.execute(select(Contact).where(Contact.user_id == test_user.id))
    assert len(result.scalars().all()) == 2  # nothing created or merged


async def test_import_never_downgrades_full_birthday_to_month_only(
    db_session: AsyncSession, test_user: User
):
    full_birthday_item = ContactImportItem(
        name="Rosalind Franklin",
        phones=[ContactImportPhone(value="+15550004444")],
        birthday=ContactImportBirthday(month=7, day=25, year=1920),
    )
    await import_contacts(test_user.id, [full_birthday_item], db_session)
    await db_session.commit()

    month_only_reimport = ContactImportItem(
        name="Rosalind Franklin",
        phones=[ContactImportPhone(value="+15550004444")],
        birthday=ContactImportBirthday(month=7, day=25, year=None),
    )
    await import_contacts(test_user.id, [month_only_reimport], db_session)
    await db_session.commit()

    result = await db_session.execute(select(Contact).where(Contact.user_id == test_user.id))
    contact = result.scalar_one()
    assert contact.birthday_year == 1920  # not downgraded to None


async def test_import_never_overwrites_manual_contact_birthday(
    db_session: AsyncSession, test_user: User
):
    manual_contact = Contact(
        user_id=test_user.id,
        name="Manually Entered",
        birthday_month=3,
        birthday_day=14,
        birthday_year=1990,
        source=ContactSource.MANUAL,
    )
    db_session.add(manual_contact)
    await db_session.flush()
    db_session.add(
        ContactIdentifier(
            contact_id=manual_contact.id,
            type="phone",
            normalized_value="+15550005555",
            raw_value="+15550005555",
        )
    )
    await db_session.commit()

    reimport_item = ContactImportItem(
        name="Manually Entered",
        phones=[ContactImportPhone(value="+15550005555")],
        birthday=ContactImportBirthday(month=1, day=1, year=2000),
    )
    await import_contacts(test_user.id, [reimport_item], db_session)
    await db_session.commit()

    await db_session.refresh(manual_contact)
    assert manual_contact.birthday_month == 3
    assert manual_contact.birthday_day == 14
    assert manual_contact.birthday_year == 1990


async def test_device_contact_id_matches_even_when_phone_changes(
    db_session: AsyncSession, test_user: User
):
    first_item = ContactImportItem(
        name="Changed Number",
        phones=[ContactImportPhone(value="+15550006666")],
        device_contact_id="device-abc",
    )
    await import_contacts(test_user.id, [first_item], db_session)
    await db_session.commit()

    reimport_with_new_number = ContactImportItem(
        name="Changed Number",
        phones=[ContactImportPhone(value="+15550007777")],
        device_contact_id="device-abc",
    )
    response = await import_contacts(test_user.id, [reimport_with_new_number], db_session)
    await db_session.commit()

    assert response.imported == 0
    assert response.updated == 1

    result = await db_session.execute(select(Contact).where(Contact.user_id == test_user.id))
    contacts = result.scalars().all()
    assert len(contacts) == 1

    identifiers = await _get_identifiers(db_session, contacts[0].id)
    assert {i.normalized_value for i in identifiers} == {"+15550006666", "+15550007777"}


async def test_malformed_phone_is_stored_but_excluded_from_matching(
    db_session: AsyncSession, test_user: User
):
    item_a = ContactImportItem(
        name="Contact A",
        phones=[ContactImportPhone(value="not-a-real-number", normalization_failed=True)],
    )
    item_b = ContactImportItem(
        name="Contact B",
        phones=[ContactImportPhone(value="not-a-real-number", normalization_failed=True)],
    )
    response = await import_contacts(test_user.id, [item_a, item_b], db_session)
    await db_session.commit()

    # Both stored as separate contacts — the shared malformed string never
    # matched because normalization_failed rows are excluded from lookup.
    assert response.imported == 2
    result = await db_session.execute(select(Contact).where(Contact.user_id == test_user.id))
    assert len(result.scalars().all()) == 2


# ---------------------------------------------------------------------------
# Router — /mobile/contacts
# ---------------------------------------------------------------------------


async def _auth_headers(async_client: AsyncClient, db_session: AsyncSession) -> dict[str, str]:
    user = User(
        email="contactsuser@example.com",
        password_hash=hash_password("TestPass123!"),
        email_verified=True,
        failed_login_count=0,
    )
    db_session.add(user)
    await db_session.commit()

    login_resp = await async_client.post(
        "/mobile/auth/login",
        json={"email": "contactsuser@example.com", "password": "TestPass123!"},
    )
    assert login_resp.status_code == 200
    access_token = login_resp.json()["access_token"]
    return {"Authorization": f"Bearer {access_token}"}


async def test_import_endpoint_requires_authentication(async_client: AsyncClient):
    resp = await async_client.post(
        "/mobile/contacts/import", json={"contacts": [{"name": "No Auth"}]}
    )
    assert resp.status_code == 401


async def test_import_and_list_contacts_end_to_end(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)

    import_resp = await async_client.post(
        "/mobile/contacts/import",
        json={
            "contacts": [
                {
                    "name": "Imported Contact",
                    "phones": [{"value": "+15559990000"}],
                    "emails": ["imported@example.com"],
                }
            ]
        },
        headers=headers,
    )
    assert import_resp.status_code == 200
    assert import_resp.json()["imported"] == 1

    list_resp = await async_client.get("/mobile/contacts", headers=headers)
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Imported Contact"


async def test_get_contact_not_found_for_other_users_contact(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)

    other_user = User(
        email="otheruser@example.com",
        password_hash=hash_password("TestPass123!"),
        email_verified=True,
        failed_login_count=0,
    )
    db_session.add(other_user)
    await db_session.flush()
    other_contact = Contact(user_id=other_user.id, name="Not Yours", source=ContactSource.MANUAL)
    db_session.add(other_contact)
    await db_session.commit()

    resp = await async_client.get(f"/mobile/contacts/{other_contact.id}", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["code"] == "contact_not_found"


async def test_update_contact_birthday_flips_source_to_manual(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)
    await async_client.post(
        "/mobile/contacts/import",
        json={"contacts": [{"name": "To Edit", "phones": [{"value": "+15558880000"}]}]},
        headers=headers,
    )
    list_resp = await async_client.get("/mobile/contacts", headers=headers)
    contact_id = list_resp.json()["items"][0]["id"]

    update_resp = await async_client.patch(
        f"/mobile/contacts/{contact_id}",
        json={"birthday_month": 5, "birthday_day": 20, "birthday_year": 1999},
        headers=headers,
    )
    assert update_resp.status_code == 200
    body = update_resp.json()
    assert body["birthday_month"] == 5
    assert body["source"] == "manual"


async def test_delete_contact_soft_deletes_and_excludes_from_list(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)
    await async_client.post(
        "/mobile/contacts/import",
        json={"contacts": [{"name": "To Delete", "phones": [{"value": "+15557770000"}]}]},
        headers=headers,
    )
    list_resp = await async_client.get("/mobile/contacts", headers=headers)
    contact_id = list_resp.json()["items"][0]["id"]

    delete_resp = await async_client.delete(f"/mobile/contacts/{contact_id}", headers=headers)
    assert delete_resp.status_code == 204

    after_delete_list = await async_client.get("/mobile/contacts", headers=headers)
    assert after_delete_list.json()["total"] == 0

    get_resp = await async_client.get(f"/mobile/contacts/{contact_id}", headers=headers)
    assert get_resp.status_code == 404
