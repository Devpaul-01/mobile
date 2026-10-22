"""
db/models.py — SQLAlchemy ORM models.

Milestone 1 added users, sessions, and the auth-token tables. Milestone 2
added Contact/ContactIdentifier per MOBILE_APP_ARCHITECTURE_SPEC.md §36.
Milestone 3 added Reminder — scoped specifically to "apply a reminder to
several selected contacts at once" per an explicit team scope answer
(multi-select contacts at creation time, no persisted Group entity), not
the full scheduler/occurrence/push/email/approval machinery.
Milestone 4 added ReminderOccurrence (the scheduler's idempotency anchor,
§16.1) and PushToken (device push-token registration, §12.2) — i.e. Phase
6 plus a minimal Phase 7 slice.
Milestone 5 added PendingApproval / ApprovalHistory — the WhatsApp
pending-approval state machine, §17, Phase 9. This is the one remaining
channel from Milestone 4's scheduler that previously only logged a
warning and did nothing; it's now wired to the real state machine.
Milestone 6 added ContactPhoto — object storage metadata, §26, Phase 11.
One optional photo per contact (enforced via a unique constraint on
``contact_id``, matching §26's explicit MVP scope note), backed by
Cloudflare R2 rather than AWS S3 per an explicit team decision (R2's API
is S3-compatible, so this needed no deviation from §26's design — only
the client's ``endpoint_url`` differs; see app/services/storage.py).

Milestone 7 (this revision) adds Subscription/Payment — PayPal payments,
§23/§24, Phase 12. ``Subscription.payment_provider`` is an enum
(``paypal | apple_iap | google_iap``) per §24's explicit provider-
agnostic hedge against the unresolved App/Play Store IAP-compliance
question — only ``paypal`` has a real service behind it in this
milestone. ``SubscriptionStatus``/``PaymentStatus`` concretize what §36
leaves as a generic ``status`` string column; the architecture doc
doesn't enumerate the values, so these are this milestone's own
necessary concretization, not a literal doc transcription.

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
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
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
    # Opt-in (default False — nothing changes for an existing user until
    # they turn it on): if True, a reminder's birthday-message email
    # (channel="email") is sent automatically once the scheduler
    # considers it "close enough" to the contact's actual birthday,
    # rather than waiting in the Approval Queue for a manual tap.
    # Per an explicit team decision — see services/scheduler.py's
    # birthday-day email check for exactly what "close enough" means.
    # WhatsApp is unaffected either way; it can never be auto-sent.
    auto_send_email: Mapped[bool] = mapped_column(
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


class ApprovalState(str, enum.Enum):
    """States for the WhatsApp pending-approval state machine
    (MOBILE_APP_ARCHITECTURE_SPEC.md §17.1). WhatsApp can never be
    auto-sent, so the backend's job stops at handing the user a
    pre-filled deep link — these states track how far that handoff got,
    not whether the message was actually delivered inside WhatsApp
    (unobservable from here; see ``COMPLETED``'s docstring note on the
    approval service)."""

    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    COMPLETED = "completed"
    LINK_FAILED = "link_failed"
    EXPIRED = "expired"
    DISMISSED = "dismissed"


class ApprovalChannel(str, enum.Enum):
    """A second approval-gated channel, exactly as this docstring once
    anticipated: ``email`` now also creates a ``PendingApproval`` when a
    reminder's email isn't eligible for (or isn't configured for)
    auto-send — see ``User.auto_send_email`` and
    ``services/scheduler.py``'s birthday-day email check."""

    WHATSAPP = "whatsapp"
    EMAIL = "email"


class ApprovalActor(str, enum.Enum):
    """Who caused an ``ApprovalHistory`` transition — §17.4."""

    USER = "user"
    SCHEDULER = "scheduler"
    SYSTEM = "system"


class PendingApproval(Base):
    """One row per WhatsApp or email birthday-message send awaiting the
    user's manual tap-to-send (MOBILE_APP_ARCHITECTURE_SPEC.md §17;
    email extended per an explicit team decision — see
    ``User.auto_send_email`` and ``services/scheduler.py``). Created by
    the scheduler (``app/services/scheduler.py``) — never created
    directly by an API call, since the whole point of this table is that
    the *scheduler* decided a send is due; the API only ever moves an
    existing row through its states.

    ``channel``-specific behavior:
      - ``whatsapp``: unchanged — ``message_subject``/``message_body``
        are left ``NULL``; the message is still composed client-side at
        tap time (§20.3), and the full
        pending_approval → approved → report-opened → completed/
        link_failed state machine applies.
      - ``email``: ``message_subject``/``message_body`` are populated at
        creation time from ``services/message_templates.py`` and are the
        user's editable draft (``PATCH /mobile/approvals/{id}``) up
        until they approve. There's no "did the link open" step to
        report for email — the backend can directly confirm an SMTP
        send succeeded or failed, so ``approve`` for this channel goes
        straight to ``completed``/``link_failed`` (see
        ``services/approvals.py``), skipping ``report_opened``
        entirely. This table is also how a successful *auto*-send
        (``User.auto_send_email=True``, close enough to the birthday —
        see the scheduler) is recorded: created already in the
        ``completed``/``link_failed`` state, actor ``system``, so both
        send paths share one place to look for "was this contact's
        birthday message actually sent."

    The unique constraint on ``(reminder_occurrence_id, channel)`` is
    belt-and-suspenders alongside the occurrence table's own
    ``(reminder_id, occurrence_year)`` uniqueness (§17.3) — even if the
    scheduler somehow re-evaluated the same occurrence, it couldn't
    create a second approval row for the same channel. Widened from a
    plain unique-on-``reminder_occurrence_id`` now that a single
    occurrence can have both a whatsapp and an email approval.

    Not soft-deleted (§14's convention for this table): an approval is
    either actioned, expired, or dismissed — there's no "undo" concept
    that soft-delete would need to preserve, unlike ``Contact``.
    """

    __tablename__ = "pending_approvals"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    reminder_occurrence_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("reminder_occurrences.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel: Mapped[ApprovalChannel] = mapped_column(
        Enum(ApprovalChannel, name="approval_channel", native_enum=False, length=20),
        nullable=False,
        server_default=text("'whatsapp'"),
    )
    state: Mapped[ApprovalState] = mapped_column(
        Enum(ApprovalState, name="approval_state", native_enum=False, length=20),
        nullable=False,
        server_default=text("'pending_approval'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    message_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message_body: Mapped[str | None] = mapped_column(Text(), nullable=True)

    contact: Mapped["Contact"] = relationship("Contact")

    __table_args__ = (
        UniqueConstraint(
            "reminder_occurrence_id",
            "channel",
            name="uq_pending_approval_occurrence_channel",
        ),
        Index("idx_pending_approvals_user_id", "user_id"),
    )


class ApprovalHistory(Base):
    """Audit trail of every state transition a ``PendingApproval`` goes
    through (§17.4) — kept as an append-only log rather than only
    tracking current state, so "did the user see this and dismiss it, or
    never see it at all" (DISMISSED vs EXPIRED) stays answerable later.

    ``from_state`` is nullable only for the very first row (creation —
    there is no prior state to record); every subsequent transition has
    one.
    """

    __tablename__ = "approval_history"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    approval_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("pending_approvals.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_state: Mapped[ApprovalState | None] = mapped_column(
        Enum(ApprovalState, name="approval_history_from_state", native_enum=False, length=20),
        nullable=True,
    )
    to_state: Mapped[ApprovalState] = mapped_column(
        Enum(ApprovalState, name="approval_history_to_state", native_enum=False, length=20),
        nullable=False,
    )
    actor: Mapped[ApprovalActor] = mapped_column(
        Enum(ApprovalActor, name="approval_actor", native_enum=False, length=20),
        nullable=False,
    )
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (Index("idx_approval_history_approval_id", "approval_id"),)


class ContactPhoto(Base):
    """Metadata for one contact's uploaded photo (§26) — the actual image
    bytes live in Cloudflare R2 (an S3-compatible object store), never in
    this database; this row only ever points at an object by its key.

    Enforces "one optional photo per contact" (§26's explicit MVP scope
    note — no photo *library*/multiple-images-per-contact) via the unique
    constraint on ``contact_id``: uploading a new photo for a contact that
    already has one replaces it (see app/services/storage.py), rather
    than this table ever holding two rows for the same contact.

    Not soft-deleted: a photo is either present or it isn't, there's no
    history value in keeping a row around after its object is deleted.
    """

    __tablename__ = "contact_photos"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    object_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("contact_id", name="uq_contact_photo_contact_id"),
    )


class PaymentProvider(str, enum.Enum):
    """§24's provider-agnostic hedge: only ``PAYPAL`` has a service behind
    it in this milestone. ``APPLE_IAP``/``GOOGLE_IAP`` exist as schema
    values only, so that adding native IAP later (contingent on the still-
    unresolved App/Play Store compliance question, §24) is a service
    addition, not a schema rework."""

    PAYPAL = "paypal"
    APPLE_IAP = "apple_iap"
    GOOGLE_IAP = "google_iap"


class SubscriptionStatus(str, enum.Enum):
    """Concretizes §36's generic ``status`` column. ``PENDING`` covers the
    window between "order created" and "order captured" — the architecture
    doc's §23 flow has the client round-trip through PayPal's hosted
    approval UI before capture, so a subscription can exist in a
    not-yet-entitled state for real, observable time."""

    PENDING = "pending"
    ACTIVE = "active"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class Subscription(Base):
    """One row per user (§36: ``user_id`` unique — "one active at a time,
    mirrors existing partial-unique-index pattern"). Represents the
    user's current entitlement window, not a literal PayPal recurring-
    billing agreement: this milestone implements §23's diagrammed flow
    literally (PayPal **Orders** create-order + capture, not the separate
    PayPal *Subscriptions* API), so "renewal" here means the user
    initiating a new create-order/capture round trip before
    ``current_period_end`` lapses, not a payment PayPal itself triggers
    automatically. ``provider_subscription_id`` holds the most recently
    captured PayPal order id, kept for traceability against PayPal's own
    dashboard — not a true recurring-agreement id, since none exists in
    this flow.

    This is a deliberate, explicitly-flagged interpretation: §23's own
    diagram shows ``create-order``/``capture`` (Orders API), and §35's API
    inventory lists exactly those two payment endpoints plus a webhook —
    nothing in either section names the Subscriptions API's
    ``create-subscription`` endpoint. The webhook endpoint still exists
    per §23's "webhooks handle ongoing lifecycle state" framing, so a
    future move to true recurring billing can land without a schema
    change beyond what's already here.
    """

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    payment_provider: Mapped[PaymentProvider] = mapped_column(
        Enum(PaymentProvider, name="payment_provider", native_enum=False, length=20),
        nullable=False,
        server_default=text("'paypal'"),
    )
    provider_subscription_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    plan: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, name="subscription_status", native_enum=False, length=20),
        nullable=False,
        server_default=text("'pending'"),
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    payments: Mapped[list["Payment"]] = relationship(
        "Payment", back_populates="subscription", cascade="all, delete-orphan"
    )


class Payment(Base):
    """One row per PayPal order (§12.2: "capturing a PayPal order must be
    idempotent on ``paypal_order_id`` — a unique constraint on that column
    prevents double-processing if the client retries after a timeout").
    """

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider_order_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status", native_enum=False, length=20),
        nullable=False,
        server_default=text("'pending'"),
    )
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    subscription: Mapped["Subscription"] = relationship("Subscription", back_populates="payments")

    __table_args__ = (Index("idx_payments_subscription_id", "subscription_id"),)
