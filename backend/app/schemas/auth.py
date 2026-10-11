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

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints


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
    name: Optional[str] = Field(None, max_length=255)
    timezone: Optional[str] = Field(None, max_length=100)
