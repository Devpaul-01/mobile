"""
services/reminders.py — Reminder creation, including the bulk/multi-select
apply-to-several-contacts operation this milestone exists for.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Contact, Reminder
from app.schemas.reminder import ReminderBulkCreate, ReminderBulkCreateResponse, ReminderOut


async def bulk_create_reminders(
    user_id: UUID, data: ReminderBulkCreate, db: AsyncSession
) -> ReminderBulkCreateResponse:
    """Create one ``Reminder`` row per contact in ``data.contact_ids``, all
    sharing the same ``lead_time_days``/``channels``.

    A ``contact_id`` that doesn't exist or isn't owned by this user is
    skipped (reported in ``skipped_contact_ids``), not an error for the
    whole call — a multi-select of 50 contacts shouldn't fail entirely
    because one was deleted a moment earlier.
    """
    result = await db.execute(
        select(Contact.id).where(
            Contact.id.in_(data.contact_ids),
            Contact.user_id == user_id,
            Contact.deleted_at.is_(None),
        )
    )
    owned_contact_ids = {row[0] for row in result.all()}

    created: list[Reminder] = []
    skipped: list[UUID] = []
    for contact_id in data.contact_ids:
        if contact_id not in owned_contact_ids:
            skipped.append(contact_id)
            continue
        reminder = Reminder(
            contact_id=contact_id,
            user_id=user_id,
            lead_time_days=data.lead_time_days,
            channels=data.channels,
        )
        db.add(reminder)
        created.append(reminder)

    await db.flush()
    for reminder in created:
        await db.refresh(reminder)

    return ReminderBulkCreateResponse(
        created=[ReminderOut.model_validate(r) for r in created],
        skipped_contact_ids=skipped,
    )
