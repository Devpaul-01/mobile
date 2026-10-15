"""
services/dedup.py — Contact import identity matching and merge logic.

Implements MOBILE_APP_ARCHITECTURE_SPEC.md §15.2 precisely:

- Identity matching key is normalized phone (E.164) or normalized email
  (lowercased) — **never name**. Two different people are frequently named
  the same thing; the same person is frequently saved under different name
  spellings by different people, so name carries no matching signal here.
- Fast path: match by ``device_contact_id`` first (§15.4) — repeat imports
  of the same device contact short-circuit straight to an update without
  needing to re-match on phone/email.
- No identifier match → create.
- Exactly one match → update (merge new identifiers, apply the
  never-downgrade-a-known-birthday rule).
- More than one distinct existing contact matched (e.g. an incoming row's
  phone matches contact A and its email matches contact B) → do **not**
  guess. Flag for manual review and change nothing.
- A phone number the client couldn't normalize to E.164
  (``normalization_failed: True``) is still stored, verbatim, on the
  contact — useful once corrected, and for WhatsApp — but is excluded from
  the matching query itself, since matching on an unnormalized string
  risks a false merge.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Contact, ContactIdentifier, ContactIdentifierType, ContactSource
from app.schemas.contact import (
    ContactImportItem,
    ContactImportResponse,
    ContactImportReviewItem,
)


async def import_contacts(
    user_id: UUID, items: list[ContactImportItem], db: AsyncSession
) -> ContactImportResponse:
    imported = 0
    updated = 0
    requires_review: list[ContactImportReviewItem] = []

    for item in items:
        matched_contact_id: UUID | None = None

        if item.device_contact_id:
            matched_contact_id = await _find_by_device_contact_id(
                user_id, item.device_contact_id, db
            )

        if matched_contact_id is not None:
            contact = await _load_contact(matched_contact_id, db)
            if contact is not None:
                await _update_contact(contact, item, db)
                updated += 1
                continue

        matched_ids = await _find_matching_contact_ids(user_id, item, db)

        if len(matched_ids) == 0:
            await _create_contact(user_id, item, db)
            imported += 1
        elif len(matched_ids) == 1:
            contact = await _load_contact(next(iter(matched_ids)), db)
            assert contact is not None  # matched_ids came from a live query
            await _update_contact(contact, item, db)
            updated += 1
        else:
            requires_review.append(
                ContactImportReviewItem(
                    name=item.name,
                    device_contact_id=item.device_contact_id,
                    matched_contact_ids=list(matched_ids),
                )
            )

    return ContactImportResponse(
        imported=imported, updated=updated, requires_review=requires_review
    )


async def _find_by_device_contact_id(
    user_id: UUID, device_contact_id: str, db: AsyncSession
) -> UUID | None:
    result = await db.execute(
        select(ContactIdentifier.contact_id)
        .join(Contact, Contact.id == ContactIdentifier.contact_id)
        .where(
            Contact.user_id == user_id,
            Contact.deleted_at.is_(None),
            ContactIdentifier.device_contact_id == device_contact_id,
        )
        .limit(1)
    )
    row = result.first()
    return row[0] if row else None


async def _find_matching_contact_ids(
    user_id: UUID, item: ContactImportItem, db: AsyncSession
) -> set[UUID]:
    normalized_values = [p.value for p in item.phones if not p.normalization_failed]
    normalized_values += list(item.emails)
    if not normalized_values:
        return set()

    result = await db.execute(
        select(ContactIdentifier.contact_id)
        .join(Contact, Contact.id == ContactIdentifier.contact_id)
        .where(
            Contact.user_id == user_id,
            Contact.deleted_at.is_(None),
            ContactIdentifier.normalization_failed.is_(False),
            ContactIdentifier.normalized_value.in_(normalized_values),
        )
        .distinct()
    )
    return {row[0] for row in result.all()}


async def _load_contact(contact_id: UUID, db: AsyncSession) -> Contact | None:
    result = await db.execute(select(Contact).where(Contact.id == contact_id))
    return result.scalar_one_or_none()


async def _create_contact(
    user_id: UUID, item: ContactImportItem, db: AsyncSession
) -> Contact:
    contact = Contact(
        user_id=user_id,
        name=item.name,
        birthday_month=item.birthday.month if item.birthday else None,
        birthday_day=item.birthday.day if item.birthday else None,
        birthday_year=item.birthday.year if item.birthday else None,
        source=ContactSource.DEVICE_IMPORT,
    )
    db.add(contact)
    await db.flush()

    for phone in item.phones:
        db.add(
            ContactIdentifier(
                contact_id=contact.id,
                type=ContactIdentifierType.PHONE,
                normalized_value=phone.value,
                raw_value=phone.value,
                device_contact_id=item.device_contact_id,
                normalization_failed=phone.normalization_failed,
            )
        )
    for email in item.emails:
        db.add(
            ContactIdentifier(
                contact_id=contact.id,
                type=ContactIdentifierType.EMAIL,
                normalized_value=email,
                raw_value=email,
                device_contact_id=item.device_contact_id,
                normalization_failed=False,
            )
        )
    return contact


async def _update_contact(
    contact: Contact, item: ContactImportItem, db: AsyncSession
) -> None:
    """Merge an incoming import row onto an already-matched contact.

    Name is deliberately never overwritten here — if the user has since
    edited the contact's name in-app, a re-import shouldn't silently
    clobber that edit. Identifiers are merged additively. Birthday follows
    the never-downgrade rule from §15.2/§15.3, applied only to
    device-sourced contacts.
    """
    existing_result = await db.execute(
        select(ContactIdentifier).where(ContactIdentifier.contact_id == contact.id)
    )
    existing_identifiers = {
        (ci.type, ci.normalized_value) for ci in existing_result.scalars().all()
    }

    for phone in item.phones:
        key = (ContactIdentifierType.PHONE, phone.value)
        if key not in existing_identifiers:
            db.add(
                ContactIdentifier(
                    contact_id=contact.id,
                    type=ContactIdentifierType.PHONE,
                    normalized_value=phone.value,
                    raw_value=phone.value,
                    device_contact_id=item.device_contact_id,
                    normalization_failed=phone.normalization_failed,
                )
            )
            existing_identifiers.add(key)

    for email in item.emails:
        key = (ContactIdentifierType.EMAIL, email)
        if key not in existing_identifiers:
            db.add(
                ContactIdentifier(
                    contact_id=contact.id,
                    type=ContactIdentifierType.EMAIL,
                    normalized_value=email,
                    raw_value=email,
                    device_contact_id=item.device_contact_id,
                    normalization_failed=False,
                )
            )
            existing_identifiers.add(key)

    # §15.3: a manually-entered contact's birthday is higher-trust than
    # device scrape data — never auto-overwrite it from an import.
    if contact.source != ContactSource.MANUAL and item.birthday is not None:
        existing_is_full = contact.birthday_month is not None and contact.birthday_year is not None
        incoming_is_full = item.birthday.year is not None

        # Only write if: contact currently has no birthday at all, or the
        # incoming data is an improvement (month-only -> full), or both are
        # full (a correction). Never downgrade a known full date to
        # month-only.
        contact_has_birthday = contact.birthday_month is not None
        if not contact_has_birthday or incoming_is_full or not existing_is_full:
            contact.birthday_month = item.birthday.month
            contact.birthday_day = item.birthday.day
            contact.birthday_year = item.birthday.year

    contact.updated_at = datetime.now(timezone.utc)
