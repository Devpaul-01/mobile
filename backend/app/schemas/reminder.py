"""
schemas/reminder.py — Pydantic schemas for the reminders domain (Milestone 3).

Scope note: these endpoints create/read/update/delete ``Reminder`` rows —
stored preferences. Nothing here fires a notification; there's no scheduler
yet (see app/db/models.py Reminder docstring).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, field_validator

_MAX_BULK_CONTACTS = 500


def _validate_channels(channels: list[str]) -> list[str]:
    allowed = {"push", "email", "whatsapp"}
    if not channels:
        raise ValueError("channels must include at least one of: push, email, whatsapp")
    deduped = list(dict.fromkeys(channels))  # preserve order, drop exact dupes
    invalid = [c for c in deduped if c not in allowed]
    if invalid:
        raise ValueError(f"invalid channel(s): {invalid} — allowed: push, email, whatsapp")
    return deduped


class ReminderCreate(BaseModel):
    contact_id: UUID
    lead_time_days: int = Field(default=0, ge=0, le=365)
    channels: list[Annotated[str, StringConstraints(max_length=20)]] = Field(
        default_factory=lambda: ["push"]
    )

    @field_validator("channels")
    @classmethod
    def _check_channels(cls, channels: list[str]) -> list[str]:
        return _validate_channels(channels)


class ReminderBulkCreate(BaseModel):
    """Apply one reminder definition to several selected contacts at once —
    the explicit feature request this milestone exists for. No persisted
    Group entity: ``contact_ids`` is a plain multi-select, scoped to this
    one call."""

    contact_ids: list[UUID] = Field(..., min_length=1, max_length=_MAX_BULK_CONTACTS)
    lead_time_days: int = Field(default=0, ge=0, le=365)
    channels: list[Annotated[str, StringConstraints(max_length=20)]] = Field(
        default_factory=lambda: ["push"]
    )

    @field_validator("channels")
    @classmethod
    def _check_channels(cls, channels: list[str]) -> list[str]:
        return _validate_channels(channels)

    @field_validator("contact_ids")
    @classmethod
    def _dedupe_contact_ids(cls, contact_ids: list[UUID]) -> list[UUID]:
        return list(dict.fromkeys(contact_ids))


class ReminderOut(BaseModel):
    id: UUID
    contact_id: UUID
    lead_time_days: int
    channels: list[str]
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ReminderBulkCreateResponse(BaseModel):
    created: list[ReminderOut]
    skipped_contact_ids: list[UUID] = Field(default_factory=list)


class ReminderUpdate(BaseModel):
    lead_time_days: int | None = Field(default=None, ge=0, le=365)
    channels: list[Annotated[str, StringConstraints(max_length=20)]] | None = None
    enabled: bool | None = None

    @field_validator("channels")
    @classmethod
    def _check_channels(cls, channels: list[str] | None) -> list[str] | None:
        if channels is None:
            return None
        return _validate_channels(channels)


class ReminderListResponse(BaseModel):
    items: list[ReminderOut]
    total: int
    limit: int
    offset: int
