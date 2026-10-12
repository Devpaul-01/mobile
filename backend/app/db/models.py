"""
db/models.py — SQLAlchemy ORM models.

Milestone 1 added users, sessions, and the auth-token tables. Milestone 2
added Contact/ContactIdentifier per MOBILE_APP_ARCHITECTURE_SPEC.md §36.
Milestone 3 added Reminder — scoped specifically to "apply a reminder to
several selected contacts at once" per an explicit team scope answer
(multi-select contacts at creation time, no persisted Group entity), not
the full scheduler/occurrence/push/email/approval machinery.
Milestone 4 (this revision) adds ReminderOccurrence (the scheduler's
idempotency anchor, §16.1) and PushToken (device push-token registration,
§12.2) — i.e. Phase 6 plus a minimal Phase 7 slice. PendingApproval /
ApprovalHistory (the WhatsApp state machine, Phase 9) are still deferred —
a large, separate state machine of their own, not bundled in here.
Subscriptions/payments are still deferred to later milestones too — not
stubbed here, since an empty table nobody writes to yet is dead weight, not
scaffolding.

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

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
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


class ReminderChannel(str, enum.Enum):
    """Delivery channels a reminder can request. Stored as a JSON list on
    ``Reminder.channels`` rather than the architecture doc's suggested
    array/bitmask column (§36) — a JSON list of these string values is
    portable across Postgres and the SQLite test database, needs no
    bit-arithmetic to read, and is small enough (3 possible values) that
    bitmask compactness buys nothing here.

    Only ``push`` has anything that could act on it yet (no scheduler, no
    email service, no WhatsApp/approval flow exist in this codebase as of
    Milestone 3) — all three are accepted and stored now so the reminder
    *preference* survives once those phases are built, per the architecture
    doc's Phase 6-9 order.
    """

    PUSH = "push"
    EMAIL = "email"
    WHATSAPP = "whatsapp"


class Reminder(Base):
    """A user's request to be reminded about a contact.

    Milestone 3 scope, precisely: this table and its CRUD/bulk-create
    endpoints only. No ``ReminderOccurrence`` (the scheduler's idempotency
    anchor, MOBILE_APP_ARCHITECTURE_SPEC.md §16.1) and no scheduler exist
    yet — a ``Reminder`` row here is a stored preference, not yet something
    that fires. That's intentionally a separate, larger milestone (§41
    Phase 6), not bundled into this one.

    One contact can have more than one ``Reminder`` row (e.g. a "3 days
    before" push reminder and a separate "on the day" email reminder) — the
    schema doesn't force exactly one per contact, matching the doc's §36
    one-to-many ``CONTACT ||--o{ REMINDER`` relationship rather than adding
    a uniqueness constraint the doc never specifies.

    ``user_id`` is denormalized here (also reachable via
    ``contact.user_id``) per the doc's explicit call-out in §36 — "for
    query convenience" — so listing a user's reminders never needs a join
    through ``contacts``.
    """

    __tablename__ = "reminders"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    channels: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    contact: Mapped["Contact"] = relationship("Contact")

    __table_args__ = (
        Index("idx_reminders_user_id", "user_id"),
        Index("idx_reminders_contact_id", "contact_id"),
    )


class EmailStatus(str, enum.Enum):
    """Tracks the (stubbed, see services/email.py) email-send attempt for
    one ReminderOccurrence. ``None``/absent on the row itself means "email
    wasn't a requested channel for this reminder" — not a 5th enum value,
    since that's a different thing from "we tried and it's pending"."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    FAILED_PERMANENT = "failed_permanent"


class ReminderOccurrence(Base):
    """One row per actual calendar-year firing of a ``Reminder`` — the
    scheduler's idempotency anchor (MOBILE_APP_ARCHITECTURE_SPEC.md §16.1).

    Why a separate table rather than mutating ``Reminder`` in place: the
    scheduler runs every 15 minutes (§16.2); without a durable "did we
    already fire for 2026" record, a scheduler restart mid-cycle could
    double-fire. The unique constraint on ``(reminder_id, occurrence_year)``
    makes "create occurrence" naturally idempotent — a constraint violation
    means "already handled, skip," not an error (see
    services/scheduler.py).
    """

    __tablename__ = "reminder_occurrences"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    reminder_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("reminders.id", ondelete="CASCADE"),
        nullable=False,
    )
    occurrence_year: Mapped[int] = mapped_column(Integer, nullable=False)
    fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    email_status: Mapped[EmailStatus | None] = mapped_column(
        Enum(EmailStatus, name="email_status", native_enum=False, length=20),
        nullable=True,
    )
    email_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    email_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    reminder: Mapped["Reminder"] = relationship("Reminder")

    __table_args__ = (
        UniqueConstraint(
            "reminder_id", "occurrence_year", name="uq_reminder_occurrence_year"
        ),
        Index("idx_reminder_occurrences_reminder_id", "reminder_id"),
    )


class PushPlatform(str, enum.Enum):
    IOS = "ios"
    ANDROID = "android"


class PushToken(Base):
    """A device's push-notification token, registered by the mobile client
    (MOBILE_APP_ARCHITECTURE_SPEC.md §12.2 — upsert on ``(user_id,
    device_token)``, safe to call on every app foreground).

    Sending is stubbed for now (services/push.py logs instead of calling
    Expo's push API — no mobile client exists yet to actually receive one).
    Push failures are never retried (§16.3 — "push is inherently
    best-effort/fire-and-forget"), which is why, unlike
    ``ReminderOccurrence.email_status``, there's no status/attempts tracking
    here at all.
    """

    __tablename__ = "push_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    device_token: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[PushPlatform] = mapped_column(
        Enum(PushPlatform, name="push_platform", native_enum=False, length=10),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("user_id", "device_token", name="uq_push_token_user_device"),
        Index("idx_push_tokens_user_id", "user_id"),
    )
