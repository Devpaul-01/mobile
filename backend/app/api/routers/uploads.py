"""
api/routers/uploads.py — Contact-photo upload flow (§26).

Mounted under ``/mobile/uploads`` (set in main.py).

Endpoints
---------
POST   /mobile/uploads/presign                  Get a presigned upload
                                                 (client does a multipart
                                                 form POST directly to
                                                 object storage with the
                                                 returned url+fields).
POST   /mobile/uploads/confirm                  Confirm the upload
                                                 actually landed; creates
                                                 the ContactPhoto row.
GET    /mobile/uploads/contact-photo/{contact_id}
                                                 Fetch the contact's photo
                                                 metadata + a fresh
                                                 short-lived view URL.
DELETE /mobile/uploads/contact-photo/{contact_id}
                                                 Remove a contact's photo.

The last two endpoints aren't in the architecture doc's §35 literal
inventory — added because §26.3 requires photos be readable only via
"short-lived presigned GET URLs issued on demand," which needs a GET
endpoint to issue them, and a photo with no delete path would be a
one-way door.

Deviation from §26.2's literal diagram, worth flagging: the presign
request carries no ``contact_id`` (matches §26.2's payload exactly:
``{content_type, purpose}``), so the "does this contact belong to the
caller" ownership check described in §32's security table happens
entirely at **confirm** time, where ``contact_id`` is actually known —
there's nothing to check yet at presign time.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.errors import AppError, ErrorCodes
from app.db.models import Contact, ContactPhoto, User
from app.db.session import get_db
from app.schemas.common import ErrorResponse
from app.schemas.upload import (
    ContactPhotoOut,
    UploadConfirmRequest,
    UploadPresignRequest,
    UploadPresignResponse,
)
from app.services.storage import (
    MAX_UPLOAD_BYTES,
    build_object_key,
    create_presigned_get_url,
    create_presigned_upload,
    delete_object,
    head_object,
    validate_content_type,
)

_ERROR_RESPONSES: dict[int | str, dict] = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}

router = APIRouter(responses=_ERROR_RESPONSES)


async def _get_owned_contact(contact_id: UUID, current_user: User, db: AsyncSession) -> Contact:
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


def _to_photo_out(photo: ContactPhoto) -> ContactPhotoOut:
    return ContactPhotoOut(
        id=photo.id,
        contact_id=photo.contact_id,
        content_type=photo.content_type,
        size_bytes=photo.size_bytes,
        uploaded_at=photo.uploaded_at,
        view_url=create_presigned_get_url(photo.object_key),
    )


@router.post("/presign", response_model=UploadPresignResponse)
async def presign_upload(
    data: UploadPresignRequest,
    current_user: User = Depends(get_current_user),
) -> UploadPresignResponse:
    validate_content_type(data.content_type)
    object_key = build_object_key(user_id=current_user.id, content_type=data.content_type)
    presigned = create_presigned_upload(object_key=object_key, content_type=data.content_type)
    return UploadPresignResponse(
        upload_url=presigned["url"],
        fields=presigned["fields"],
        object_key=presigned["object_key"],
        expires_at=presigned["expires_at"],
    )


@router.post("/confirm", response_model=ContactPhotoOut)
async def confirm_upload(
    data: UploadConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ContactPhotoOut:
    contact = await _get_owned_contact(data.contact_id, current_user, db)

    # §26.2: never trust the client's claim that the upload succeeded —
    # verify the object actually exists before writing any metadata row.
    head = head_object(data.object_key)
    if head is None:
        raise AppError(
            status.HTTP_400_BAD_REQUEST,
            ErrorCodes.UPLOAD_NOT_FOUND,
            "No object found at the given object_key — upload may not have completed.",
        )
    validate_content_type(head["content_type"] or "")
    if (head["size_bytes"] or 0) > MAX_UPLOAD_BYTES:
        delete_object(data.object_key)
        raise AppError(
            status.HTTP_400_BAD_REQUEST,
            ErrorCodes.INVALID_CONTENT_TYPE,
            f"Uploaded object exceeds the {MAX_UPLOAD_BYTES} byte limit.",
        )

    # One photo per contact (§26 scope discipline) — a new confirmed
    # upload replaces any existing photo, deleting the old object too.
    existing_result = await db.execute(
        select(ContactPhoto).where(ContactPhoto.contact_id == contact.id)
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        delete_object(existing.object_key)
        await db.delete(existing)
        await db.flush()

    photo = ContactPhoto(
        contact_id=contact.id,
        object_key=data.object_key,
        content_type=head["content_type"],
        size_bytes=head["size_bytes"],
    )
    db.add(photo)
    await db.commit()
    await db.refresh(photo)
    return _to_photo_out(photo)


@router.get("/contact-photo/{contact_id}", response_model=ContactPhotoOut)
async def get_contact_photo(
    contact_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ContactPhotoOut:
    await _get_owned_contact(contact_id, current_user, db)
    result = await db.execute(select(ContactPhoto).where(ContactPhoto.contact_id == contact_id))
    photo = result.scalar_one_or_none()
    if photo is None:
        raise AppError(
            status.HTTP_404_NOT_FOUND,
            ErrorCodes.CONTACT_PHOTO_NOT_FOUND,
            "This contact has no photo",
        )
    return _to_photo_out(photo)


@router.delete("/contact-photo/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contact_photo(
    contact_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _get_owned_contact(contact_id, current_user, db)
    result = await db.execute(select(ContactPhoto).where(ContactPhoto.contact_id == contact_id))
    photo = result.scalar_one_or_none()
    if photo is None:
        raise AppError(
            status.HTTP_404_NOT_FOUND,
            ErrorCodes.CONTACT_PHOTO_NOT_FOUND,
            "This contact has no photo",
        )
    delete_object(photo.object_key)
    await db.delete(photo)
    await db.commit()
