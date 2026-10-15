"""
api/routers/contacts.py — Contact import, listing, and management.

All routes are mounted under ``/mobile/contacts`` (set in main.py) and
require a verified, unlocked user (``Depends(get_current_user)``).

Endpoints
---------
POST    /mobile/contacts/import   Bulk-import device contacts (dedup, see app/services/dedup.py).
GET     /mobile/contacts          List the user's contacts (paginated).
GET     /mobile/contacts/{id}     Fetch one contact.
PATCH   /mobile/contacts/{id}     Edit a contact's name/birthday/notes.
DELETE  /mobile/contacts/{id}     Soft-delete a contact.

Soft-delete only (§14) — ``deleted_at`` is set, the row stays. Every query
here filters ``deleted_at IS NULL`` so a deleted contact behaves as 404 to
the API consumer.

A ``PATCH`` that touches the birthday flips ``source`` to ``manual``: a
user correcting a birthday by hand is a higher-trust signal than a device
contacts scrape, and §15.3's never-downgrade rule only protects a contact
once it's marked ``manual`` — so the flip has to happen somewhere, and the
moment of manual edit is the right one.
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
from app.db.models import Contact, ContactSource, User
from app.db.session import get_db
from app.schemas.common import ErrorResponse
from app.schemas.contact import (
    ContactImportRequest,
    ContactImportResponse,
    ContactListResponse,
    ContactOut,
    ContactUpdate,
)
from app.services.dedup import import_contacts

_ERROR_RESPONSES: dict[int | str, dict] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}

router = APIRouter(responses=_ERROR_RESPONSES)


async def _get_owned_contact(
    contact_id: UUID, current_user: User, db: AsyncSession
) -> Contact:
    result = await db.execute(
        select(Contact).where(
            Contact.id == contact_id,
            Contact.user_id == current_user.id,
            Contact.deleted_at.is_(None),
        )
    )
    contact = result.scalar_one_or_none()
    if contact is None:
        raise AppError(
            status.HTTP_404_NOT_FOUND, ErrorCodes.CONTACT_NOT_FOUND, "Contact not found"
        )
    return contact


@router.post("/import", response_model=ContactImportResponse)
@limiter.limit("10/minute")
async def import_contacts_endpoint(
    request: Request,
    data: ContactImportRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ContactImportResponse:
    """Import a batch of device contacts, deduping against existing ones.

    See ``app/services/dedup.py`` for the full matching rules (§15.2). This
    endpoint is a thin transactional wrapper around that service — all the
    create/update/flag decisions happen there.
    """
    response = await import_contacts(current_user.id, data.contacts, db)
    await db.commit()
    return response


@router.get("", response_model=ContactListResponse)
async def list_contacts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ContactListResponse:
    base_filter = (Contact.user_id == current_user.id, Contact.deleted_at.is_(None))

    count_result = await db.execute(select(func.count(Contact.id)).where(*base_filter))
    total = count_result.scalar_one()

    result = await db.execute(
        select(Contact)
        .where(*base_filter)
        .order_by(Contact.name.asc())
        .limit(limit)
        .offset(offset)
    )
    contacts = result.scalars().all()

    return ContactListResponse(
        items=[ContactOut.model_validate(c) for c in contacts],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{contact_id}", response_model=ContactOut)
async def get_contact(
    contact_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ContactOut:
    contact = await _get_owned_contact(contact_id, current_user, db)
    return ContactOut.model_validate(contact)


@router.patch("/{contact_id}", response_model=ContactOut)
async def update_contact(
    contact_id: UUID,
    data: ContactUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ContactOut:
    contact = await _get_owned_contact(contact_id, current_user, db)

    fields = data.model_dump(exclude_unset=True)

    if "name" in fields:
        contact.name = fields["name"]
    if "notes" in fields:
        contact.notes = fields["notes"]

    birthday_fields = {"birthday_month", "birthday_day", "birthday_year"} & fields.keys()
    if birthday_fields:
        if "birthday_month" in fields:
            contact.birthday_month = fields["birthday_month"]
        if "birthday_day" in fields:
            contact.birthday_day = fields["birthday_day"]
        if "birthday_year" in fields:
            contact.birthday_year = fields["birthday_year"]
        contact.source = ContactSource.MANUAL

    contact.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(contact)
    return ContactOut.model_validate(contact)


@router.delete("/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contact(
    contact_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    contact = await _get_owned_contact(contact_id, current_user, db)
    contact.deleted_at = datetime.now(timezone.utc)
    await db.commit()
