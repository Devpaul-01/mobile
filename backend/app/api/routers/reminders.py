"""
api/routers/reminders.py — Reminder CRUD + multi-select bulk-apply.

All routes are mounted under ``/mobile/reminders`` (set in main.py) and
require a verified, unlocked user.

Endpoints
---------
POST    /mobile/reminders         Create one reminder for one contact.
POST    /mobile/reminders/bulk    Apply one reminder definition to several
                                   selected contacts at once (§ scope note
                                   below) — the explicit feature this
                                   milestone was built for.
GET     /mobile/reminders         List the user's reminders (paginated,
                                   optionally filtered by contact_id).
GET     /mobile/reminders/{id}    Fetch one reminder.
PATCH   /mobile/reminders/{id}    Edit lead_time_days/channels/enabled.
DELETE  /mobile/reminders/{id}    Delete a reminder (hard delete — see
                                   app/db/models.py Reminder docstring on
                                   why these aren't soft-deleted).

Scope note: "apply a reminder to a group of contacts at once" was answered
explicitly as multi-select, not a persisted Group entity — ``/bulk`` takes
a plain list of ``contact_ids`` scoped to that one call, nothing is saved
as a reusable group.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.errors import AppError, ErrorCodes
from app.core.rate_limiter import limiter
from app.db.models import Contact, Reminder, User
from app.db.session import get_db
from app.schemas.common import ErrorResponse
from app.schemas.reminder import (
    ReminderBulkCreate,
    ReminderBulkCreateResponse,
    ReminderCreate,
    ReminderListResponse,
    ReminderOut,
    ReminderUpdate,
)
from app.services.reminders import bulk_create_reminders

_ERROR_RESPONSES: dict[int | str, dict] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}

router = APIRouter(responses=_ERROR_RESPONSES)


async def _get_owned_contact_id(
    contact_id: UUID, current_user: User, db: AsyncSession
) -> None:
    """Raise 404 (not 403 — matches the rest of this API's ownership-check
    convention) if ``contact_id`` doesn't exist, isn't owned by this user,
    or has been soft-deleted. Kept local to this router rather than
    importing the contacts router's private helper, to avoid coupling two
    routers' internals together."""
    result = await db.execute(
        select(Contact.id).where(
            Contact.id == contact_id,
            Contact.user_id == current_user.id,
            Contact.deleted_at.is_(None),
        )
    )
    if result.scalar_one_or_none() is None:
        raise AppError(
            status.HTTP_404_NOT_FOUND, ErrorCodes.CONTACT_NOT_FOUND, "Contact not found"
        )


async def _get_owned_reminder(
    reminder_id: UUID, current_user: User, db: AsyncSession
) -> Reminder:
    result = await db.execute(
        select(Reminder).where(
            Reminder.id == reminder_id, Reminder.user_id == current_user.id
        )
    )
    reminder = result.scalar_one_or_none()
    if reminder is None:
        raise AppError(
            status.HTTP_404_NOT_FOUND, ErrorCodes.REMINDER_NOT_FOUND, "Reminder not found"
        )
    return reminder


@router.post("", response_model=ReminderOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("100/day")
async def create_reminder(
    request: Request,
    data: ReminderCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReminderOut:
    await _get_owned_contact_id(data.contact_id, current_user, db)

    reminder = Reminder(
        contact_id=data.contact_id,
        user_id=current_user.id,
        lead_time_days=data.lead_time_days,
        channels=data.channels,
    )
    db.add(reminder)
    await db.commit()
    await db.refresh(reminder)
    return ReminderOut.model_validate(reminder)


@router.post("/bulk", response_model=ReminderBulkCreateResponse)
@limiter.limit("20/day")
async def bulk_create_reminders_endpoint(
    request: Request,
    data: ReminderBulkCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReminderBulkCreateResponse:
    response = await bulk_create_reminders(current_user.id, data, db)
    await db.commit()
    return response


@router.get("", response_model=ReminderListResponse)
async def list_reminders(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    contact_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ReminderListResponse:
    conditions = [Reminder.user_id == current_user.id]
    if contact_id is not None:
        conditions.append(Reminder.contact_id == contact_id)

    count_result = await db.execute(select(func.count(Reminder.id)).where(*conditions))
    total = count_result.scalar_one()

    result = await db.execute(
        select(Reminder)
        .where(*conditions)
        .order_by(Reminder.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    reminders = result.scalars().all()

    return ReminderListResponse(
        items=[ReminderOut.model_validate(r) for r in reminders],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{reminder_id}", response_model=ReminderOut)
async def get_reminder(
    reminder_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReminderOut:
    reminder = await _get_owned_reminder(reminder_id, current_user, db)
    return ReminderOut.model_validate(reminder)


@router.patch("/{reminder_id}", response_model=ReminderOut)
async def update_reminder(
    reminder_id: UUID,
    data: ReminderUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReminderOut:
    reminder = await _get_owned_reminder(reminder_id, current_user, db)
    fields = data.model_dump(exclude_unset=True)

    if "lead_time_days" in fields:
        reminder.lead_time_days = fields["lead_time_days"]
    if "channels" in fields:
        reminder.channels = fields["channels"]
    if "enabled" in fields:
        reminder.enabled = fields["enabled"]

    reminder.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(reminder)
    return ReminderOut.model_validate(reminder)


@router.delete("/{reminder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_reminder(
    reminder_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    reminder = await _get_owned_reminder(reminder_id, current_user, db)
    await db.delete(reminder)
    await db.commit()
