"""
schemas/auth.py — Pydantic schemas for authentication endpoints.

Shape copied from bm-ts/backend/app/schemas/auth.py, with ``name``/``timezone``
promoted to real columns on ``User`` (see db/models.py) rather than keys inside
a JSON metadata blob — the extension backend has no dedicated columns for
these (see MOBILE_APP_ARCHITECTURE_SPEC.md §36), but the scheduler needs
``User.timezone`` queryable/indexable later (§16.2), so this system gives it
a real column from the start.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Optional
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator

from app.core.validators import sanitize_text


class RegisterRequest(BaseModel):
    """Registration payload — email is validated as a proper address; password minimum 8 chars."""

    email: EmailStr
    password: Annotated[str, StringConstraints(min_length=8, max_length=128)] = Field(
        ..., description="8–128 characters"
    )


class LoginRequest(BaseModel):
    """Login credentials — email + plaintext password."""

    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    """Holds the opaque refresh token returned by register/login."""

    refresh_token: str


class TokenResponse(BaseModel):
    """JWT token pair returned on successful login or refresh."""

    access_token: str
    refresh_token: str
    token_type: str = Field(default="bearer")


class UserProfile(BaseModel):
    """Public profile returned by GET /mobile/auth/me. ORM-mapped from the ``User`` model."""

    id: UUID
    email: EmailStr
    created_at: datetime
    email_verified: bool = False
    name: str | None = None
    timezone: str | None = None

    model_config = ConfigDict(from_attributes=True)


class UserProfileUpdate(BaseModel):
    """Payload for PATCH /mobile/auth/me. Both fields optional/independent —
    sending only one leaves the other unchanged (see the router).

    ``timezone`` must be a real IANA zone name (e.g. "America/New_York"),
    not just any string under 100 chars — the scheduler does
    ``ZoneInfo(user.timezone)`` for every reminder on every pass
    (services/scheduler.py §16.2) with no try/except around that call, so
    an invalid value saved here would crash the scheduler pass for every
    user being evaluated in that run, not just this one. Validating at
    write time, where a client can see and correct the error, is far
    better than discovering it later inside a background job with no
    caller to report it to."""

    name: Optional[str] = Field(None, max_length=255)
    timezone: Optional[str] = Field(None, max_length=100)

    _sanitize_name = field_validator("name", mode="after")(sanitize_text)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"{v!r} is not a recognized IANA timezone name (e.g. 'America/New_York', 'UTC')"
            ) from exc
        return v
