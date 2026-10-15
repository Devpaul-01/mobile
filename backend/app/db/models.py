"""
db/models.py — SQLAlchemy ORM models.

Milestone 1 added users, sessions, and the auth-token tables. Milestone 2
(this file) adds Contact/ContactIdentifier per MOBILE_APP_ARCHITECTURE_SPEC.md
§36. Reminders, approvals, push tokens, subscriptions and payments are still
deferred to later milestones — not stubbed here, since an empty table nobody
writes to yet is dead weight, not scaffolding.

Patterns copied from bm-ts/backend/app/db/models.py: UUID primary keys,
``server_default=now()`` timestamps, ``ondelete="CASCADE"`` FKs so account
deletion (a later milestone) reduces to deleting the User row for anything
that's a plain FK child (see MOBILE_APP_ARCHITECTURE_SPEC.md §27.1).

``contacts.group_id`` from the architecture doc's §36 table is deliberately
omitted here: it would FK to a ``message_groups`` table that doesn't exist
yet (no milestone before Phase 6 needs groups, and the doc itself says a
dedicated Groups UI isn't MVP-required — §44). Adding it as a nullable
column in a later migration, once ``message_groups`` exists, is a one-line
additive change — not worth a FK-to-nothing now.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ContactSource(str, enum.Enum):
    """Where a contact's data came from — affects how import merges treat it
    (see MOBILE_APP_ARCHITECTURE_SPEC.md §15.3): a ``manual`` contact's
    birthday is never silently overwritten by a later device re-import.

    ``future_calendar_import`` and ``extension_sync`` from the architecture
    doc's enum are not included as values here yet — nothing in this
    codebase can produce either of them, and an unused enum value is a
    speculative API surface, not a harmless placeholder. Adding one is a
    single-line migration when the feature that produces it actually ships.
    """

    DEVICE_IMPORT = "device_import"
    MANUAL = "manual"


class ContactIdentifierType(str, enum.Enum):
    PHONE = "phone"
    EMAIL = "email"


class User(Base):
    """Core user account record."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text(), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    email_verified: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, server_default=text("false")
    )
    failed_login_count: Mapped[int] = mapped_column(
        "failed_login_count", nullable=False, server_default=text("0"), default=0
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    sessions: Mapped[list["UserSession"]] = relationship(
        "UserSession", back_populates="user", cascade="all, delete-orphan"
    )
    password_reset_tokens: Mapped[list["PasswordResetToken"]] = relationship(
        "PasswordResetToken", back_populates="user", cascade="all, delete-orphan"
    )
    email_verification_tokens: Mapped[list["EmailVerificationToken"]] = relationship(
        "EmailVerificationToken", back_populates="user", cascade="all, delete-orphan"
    )


class UserSession(Base):
    """Persistent authenticated session anchored to the latest refresh-token JTI."""

    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    current_refresh_token_jti: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)

    user: Mapped[User] = relationship("User", back_populates="sessions")

    __table_args__ = (
        Index("idx_user_sessions_user_last_seen", "user_id", "last_seen_at"),
        Index("idx_user_sessions_user_revoked", "user_id", "revoked_at"),
        Index("idx_user_sessions_expires", "expires_at"),
    )


class RevokedToken(Base):
    """Records revoked refresh-token JTIs so refresh tokens can be individually invalidated."""

    __tablename__ = "revoked_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    refresh_token_jti: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("idx_revoked_jti", "refresh_token_jti", unique=True),
        Index("idx_revoked_expires", "expires_at"),
    )


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    user: Mapped[User] = relationship(
        "User", back_populates="email_verification_tokens"
    )

    __table_args__ = (Index("idx_email_verif_token", "token", unique=True),)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    user: Mapped[User] = relationship("User", back_populates="password_reset_tokens")

    __table_args__ = (Index("idx_pwreset_token", "token", unique=True),)


class Contact(Base):
    """A person the user wants to stay in touch with.

    Deliberately has no ``facebook_profile_url`` column at all — the
    extension backend's equivalent table makes that column ``NOT NULL``
    (see MOBILE_APP_ARCHITECTURE_SPEC.md §3, §14), which is precisely why
    this schema cannot reuse that table's design unchanged. Mobile has no
    mechanism to populate a Facebook ID (§21), so the column isn't included
    even as a nullable placeholder — it can be added later, additively, if
    a cross-system sync feature is ever built.

    Birthday is stored as three nullable int columns rather than a single
    date, because device contacts frequently have no year, and sometimes
    no day either isn't true — but a year-less birthday is the common case
    this must represent natively (§15.1 step 4), unlike the extension
    backend's boolean ``birthday_month_only`` flag against a full date.

    Soft-deleted via ``deleted_at`` (§14) so a reminder's history stays
    meaningful even after the contact itself is removed.
    """

    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    birthday_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    birthday_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    birthday_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[ContactSource] = mapped_column(
        Enum(ContactSource, name="contact_source", native_enum=False, length=20),
        nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(Text(), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    identifiers: Mapped[list["ContactIdentifier"]] = relationship(
        "ContactIdentifier", back_populates="contact", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_contacts_user_id", "user_id"),
        Index("idx_contacts_user_deleted", "user_id", "deleted_at"),
    )


class ContactIdentifier(Base):
    """A normalized phone number or email address used to detect whether an
    incoming imported contact is the same person as an existing one.

    This is the dedup-matching table (MOBILE_APP_ARCHITECTURE_SPEC.md §15.2)
    — kept separate from ``Contact`` rather than flat columns on it, on
    purpose, so a contact can hold more than one phone/email and so a future
    explicit-merge feature (§15.2's "user-visible merging") reduces to
    reassigning ``ContactIdentifier.contact_id`` rows rather than a schema
    change.

    ``normalized_value`` is what matching queries filter on.
    ``normalization_failed`` rows (a phone the client couldn't parse to
    E.164) are stored — so the raw value survives for later correction and
    for WhatsApp once fixed — but are excluded from the dedup lookup query
    itself (§15.1 step 4 / §15.2), since matching on an unnormalized,
    possibly-ambiguous string risks false matches.
    """

    __tablename__ = "contact_identifiers"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    type: Mapped[ContactIdentifierType] = mapped_column(
        Enum(
            ContactIdentifierType,
            name="contact_identifier_type",
            native_enum=False,
            length=10,
        ),
        nullable=False,
    )
    normalized_value: Mapped[str] = mapped_column(String(320), nullable=False)
    raw_value: Mapped[str] = mapped_column(String(320), nullable=False)
    device_contact_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    normalization_failed: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    contact: Mapped[Contact] = relationship("Contact", back_populates="identifiers")

    __table_args__ = (
        Index(
            "uq_contact_identifier_contact_type_value",
            "contact_id",
            "type",
            "normalized_value",
            unique=True,
        ),
        Index("idx_contact_identifier_normalized_value", "normalized_value"),
        Index("idx_contact_identifier_device_contact_id", "device_contact_id"),
    )
