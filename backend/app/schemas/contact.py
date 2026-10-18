"""
schemas/contact.py — Pydantic schemas for the contacts domain.

Import request shape follows MOBILE_APP_ARCHITECTURE_SPEC.md §15.1/§35: the
mobile client normalizes phone/email client-side (E.164, lowercased/trimmed)
before sending, so the server trusts the normalized values it's given but
still re-validates shape (not content — a phone string that merely looks
like E.164 format is accepted; actually re-parsing phone numbers server-side
would duplicate client-side libphonenumber work for no real benefit, since
a malformed number only costs a missed dedup match, not a security issue).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.core.validators import sanitize_text
from app.db.models import ContactSource


class ContactImportPhone(BaseModel):
    """One phone number for an incoming contact.

    ``value`` is the E.164-normalized number when the client succeeded at
    normalizing it, or the original raw string when it didn't (in which
    case ``normalization_failed`` must be true) — see
    MOBILE_APP_ARCHITECTURE_SPEC.md §15.1 step 4.
    """

    value: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    normalization_failed: bool = False


class ContactImportBirthday(BaseModel):
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)
    year: int | None = Field(default=None, ge=1900, le=2100)


class ContactImportItem(BaseModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=255, strip_whitespace=True)]
    phones: list[ContactImportPhone] = Field(default_factory=list)
    emails: list[Annotated[str, StringConstraints(max_length=320)]] = Field(
        default_factory=list
    )
    birthday: ContactImportBirthday | None = None
    device_contact_id: str | None = Field(default=None, max_length=255)

    _sanitize_name = field_validator("name", mode="after")(sanitize_text)

    @field_validator("emails")
    @classmethod
    def _lowercase_emails(cls, emails: list[str]) -> list[str]:
        # Defense in depth — the client is expected to send these already
        # normalized, but a case-sensitive dedup miss is a silent failure
        # mode not worth risking over one extra .lower() call.
        return [e.strip().lower() for e in emails]


class ContactImportRequest(BaseModel):
    contacts: list[ContactImportItem] = Field(..., min_length=1, max_length=2000)


class ContactImportReviewItem(BaseModel):
    """One incoming row that matched more than one existing contact and was
    not auto-merged — see MOBILE_APP_ARCHITECTURE_SPEC.md §15.2."""

    name: str
    device_contact_id: str | None = None
    matched_contact_ids: list[UUID]


class ContactImportResponse(BaseModel):
    imported: int
    updated: int
    requires_review: list[ContactImportReviewItem] = Field(default_factory=list)


class ContactOut(BaseModel):
    id: UUID
    name: str
    birthday_month: int | None = None
    birthday_day: int | None = None
    birthday_year: int | None = None
    source: ContactSource
    notes: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ContactUpdate(BaseModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=255, strip_whitespace=True)] | None = None
    birthday_month: int | None = Field(default=None, ge=1, le=12)
    birthday_day: int | None = Field(default=None, ge=1, le=31)
    birthday_year: int | None = Field(default=None, ge=1900, le=2100)
    notes: Annotated[str, StringConstraints(max_length=10_000)] | None = None

    _sanitize_name = field_validator("name", mode="after")(sanitize_text)
    _sanitize_notes = field_validator("notes", mode="after")(sanitize_text)


class ContactListResponse(BaseModel):
    items: list[ContactOut]
    total: int
    limit: int
    offset: int
