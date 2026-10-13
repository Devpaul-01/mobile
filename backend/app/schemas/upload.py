"""
schemas/upload.py — Pydantic schemas for contact-photo upload (§26).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

_ALLOWED_PURPOSES = {"contact_photo"}


class UploadPresignRequest(BaseModel):
    content_type: str
    purpose: str = Field(default="contact_photo")

    @field_validator("purpose")
    @classmethod
    def _check_purpose(cls, value: str) -> str:
        if value not in _ALLOWED_PURPOSES:
            raise ValueError(f"unsupported purpose '{value}' — allowed: contact_photo")
        return value


class UploadPresignResponse(BaseModel):
    upload_url: str
    fields: dict[str, str]
    object_key: str
    expires_at: datetime


class UploadConfirmRequest(BaseModel):
    object_key: str
    contact_id: UUID


class ContactPhotoOut(BaseModel):
    id: UUID
    contact_id: UUID
    content_type: str
    size_bytes: int
    uploaded_at: datetime
    view_url: str

    model_config = {"from_attributes": True}
