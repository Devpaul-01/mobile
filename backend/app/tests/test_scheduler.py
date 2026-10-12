"""Tests for the reminder scheduler core (app/services/scheduler.py) —
Phase 6. These call ``evaluate_reminders``/``retry_failed_emails`` directly
against ``db_session`` rather than going through HTTP, since there's no
endpoint here — the scheduler is a background job, not an API surface."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.models import (
    Contact,
    ContactSource,
    EmailStatus,
    PushPlatform,
    PushToken,
    Reminder,
    ReminderOccurrence,
    User,
)
from app.services.scheduler import (
    _try_acquire_scheduler_lock,
    evaluate_reminders,
    retry_failed_emails,
)

pytestmark = pytest.mark.asyncio


async def _make_user(db_session: AsyncSession, email: str, timezone_name: str | None = None) -> User:
    user = User(
        email=email,
        password_hash=hash_password("TestPass123!"),
        email_verified=True,
        failed_login_count=0,
        timezone=timezone_name,
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def _make_contact(
    db_session: AsyncSession, user_id, *, month: int, day: int, name: str = "Birthday Person"
) -> Contact:
    contact = Contact(
        user_id=user_id,
        name=name,
        source=ContactSource.MANUAL,
        birthday_month=month,
        birthday_day=day,
    )
    db_session.add(contact)
    await db_session.flush()
    return contact


async def _make_reminder(
    db_session: AsyncSession, *, user_id, contact_id, lead_time_days: int = 0, channels=None
) -> Reminder:
    reminder = Reminder(
        user_id=user_id,
        contact_id=contact_id,
        lead_time_days=lead_time_days,
        channels=channels or ["push"],
    )
    db_session.add(reminder)
    await db_session.flush()
    return reminder


# ---------------------------------------------------------------------------
# Trigger-moment correctness + idempotency
# ---------------------------------------------------------------------------


async def test_evaluate_reminders_fires_on_exact_trigger_moment_utc(db_session: AsyncSession):
    """A UTC-timezone user with a birthday today and no lead time should
    fire exactly at 09:00 UTC today."""
    user = await _make_user(db_session, "utc-user@example.com", "UTC")
    contact = await _make_contact(db_session, user.id, month=6, day=15)
    await _make_reminder(db_session, user_id=user.id, contact_id=contact.id, channels=["push"])

    now = datetime(now_year := datetime.now(timezone.utc).year, 6, 15, 9, 0, tzinfo=timezone.utc)
    created = await evaluate_reminders(db_session, now=now)

    assert len(created) == 1
    assert created[0].occurrence_year == now_year
    await db_session.commit()


async def test_evaluate_reminders_is_idempotent_across_repeated_runs(db_session: AsyncSession):
    """Running evaluate_reminders twice for the same trigger moment must
    not create a second occurrence — the unique constraint on
    (reminder_id, occurrence_year) is the actual guarantee, this just
    verifies the service surfaces that correctly as 'nothing created'."""
    user = await _make_user(db_session, "idempotent-user@example.com", "UTC")
    contact = await _make_contact(db_session, user.id, month=3, day=10)
    reminder = await _make_reminder(db_session, user_id=user.id, contact_id=contact.id, channels=["push"])

    now = datetime(datetime.now(timezone.utc).year, 3, 10, 9, 0, tzinfo=timezone.utc)

    first_run = await evaluate_reminders(db_session, now=now)
    await db_session.commit()
    assert len(first_run) == 1

    second_run = await evaluate_reminders(db_session, now=now)
    await db_session.commit()
    assert len(second_run) == 0

    result = await db_session.execute(
        select(ReminderOccurrence).where(ReminderOccurrence.reminder_id == reminder.id)
    )
    assert len(result.scalars().all()) == 1


async def test_evaluate_reminders_respects_timezone_offset(db_session: AsyncSession):
    """A user in a timezone ahead of UTC (e.g. Africa/Lagos, UTC+1) should
    fire at 08:00 UTC (09:00 local), not 09:00 UTC."""
    user = await _make_user(db_session, "lagos-user@example.com", "Africa/Lagos")
    contact = await _make_contact(db_session, user.id, month=7, day=1)
    await _make_reminder(db_session, user_id=user.id, contact_id=contact.id, channels=["push"])

    year = datetime.now(timezone.utc).year

    not_yet = datetime(year, 7, 1, 7, 55, tzinfo=timezone.utc)  # before 08:00 UTC trigger
    assert len(await evaluate_reminders(db_session, now=not_yet)) == 0

    at_trigger = datetime(year, 7, 1, 8, 0, tzinfo=timezone.utc)
    created = await evaluate_reminders(db_session, now=at_trigger)
    await db_session.commit()
    assert len(created) == 1


async def test_evaluate_reminders_handles_lead_time_crossing_year_boundary(db_session: AsyncSession):
    """A Jan 2 birthday with a 5-day lead time must trigger around Dec 28
    of the *previous* year — the reason evaluate_reminders checks 3
    candidate years instead of just ``now.year``."""
    user = await _make_user(db_session, "wraparound-user@example.com", "UTC")
    contact = await _make_contact(db_session, user.id, month=1, day=2)
    await _make_reminder(
        db_session, user_id=user.id, contact_id=contact.id, lead_time_days=5, channels=["push"]
    )

    # Dec 28 of the year *before* the birthday's candidate year.
    now = datetime(2025, 12, 28, 9, 0, tzinfo=timezone.utc)
    created = await evaluate_reminders(db_session, now=now)
    await db_session.commit()

    assert len(created) == 1
    assert created[0].occurrence_year == 2026  # the birthday being reminded about


async def test_evaluate_reminders_falls_back_feb29_to_feb28_on_non_leap_year(db_session: AsyncSession):
    user = await _make_user(db_session, "leap-user@example.com", "UTC")
    contact = await _make_contact(db_session, user.id, month=2, day=29)
    await _make_reminder(db_session, user_id=user.id, contact_id=contact.id, channels=["push"])

    # 2026 is not a leap year — the birthday should resolve to Feb 28, 2026.
    now = datetime(2026, 2, 28, 9, 0, tzinfo=timezone.utc)
    created = await evaluate_reminders(db_session, now=now)
    await db_session.commit()

    assert len(created) == 1
    assert created[0].occurrence_year == 2026


async def test_evaluate_reminders_skips_disabled_reminders(db_session: AsyncSession):
    user = await _make_user(db_session, "disabled-user@example.com", "UTC")
    contact = await _make_contact(db_session, user.id, month=4, day=4)
    reminder = await _make_reminder(db_session, user_id=user.id, contact_id=contact.id, channels=["push"])
    reminder.enabled = False
    await db_session.flush()

    now = datetime(datetime.now(timezone.utc).year, 4, 4, 9, 0, tzinfo=timezone.utc)
    created = await evaluate_reminders(db_session, now=now)
    assert created == []


async def test_evaluate_reminders_skips_soft_deleted_contacts(db_session: AsyncSession):
    user = await _make_user(db_session, "deletedcontact-user@example.com", "UTC")
    contact = await _make_contact(db_session, user.id, month=5, day=5)
    contact.deleted_at = datetime.now(timezone.utc)
    await db_session.flush()
    await _make_reminder(db_session, user_id=user.id, contact_id=contact.id, channels=["push"])

    now = datetime(datetime.now(timezone.utc).year, 5, 5, 9, 0, tzinfo=timezone.utc)
    created = await evaluate_reminders(db_session, now=now)
    assert created == []


# ---------------------------------------------------------------------------
# Downstream actions
# ---------------------------------------------------------------------------


async def test_evaluate_reminders_sends_push_to_every_registered_token(db_session: AsyncSession):
    user = await _make_user(db_session, "pushuser@example.com", "UTC")
    contact = await _make_contact(db_session, user.id, month=8, day=20)
    await _make_reminder(db_session, user_id=user.id, contact_id=contact.id, channels=["push"])

    db_session.add_all(
        [
            PushToken(user_id=user.id, device_token="token-a", platform=PushPlatform.IOS),
            PushToken(user_id=user.id, device_token="token-b", platform=PushPlatform.ANDROID),
        ]
    )
    await db_session.flush()

    now = datetime(datetime.now(timezone.utc).year, 8, 20, 9, 0, tzinfo=timezone.utc)
    created = await evaluate_reminders(db_session, now=now)
    await db_session.commit()
    assert len(created) == 1  # push has no persisted status to assert on; just confirm it didn't error


async def test_evaluate_reminders_marks_email_sent_on_success(db_session: AsyncSession):
    user = await _make_user(db_session, "emailuser@example.com", "UTC")
    contact = await _make_contact(db_session, user.id, month=9, day=9)
    await _make_reminder(db_session, user_id=user.id, contact_id=contact.id, channels=["email"])

    now = datetime(datetime.now(timezone.utc).year, 9, 9, 9, 0, tzinfo=timezone.utc)
    created = await evaluate_reminders(db_session, now=now)
    await db_session.commit()

    assert len(created) == 1
    occurrence = created[0]
    assert occurrence.email_status == EmailStatus.SENT
    assert occurrence.email_attempts == 1
    assert occurrence.email_sent_at is not None


async def test_evaluate_reminders_creates_occurrence_for_whatsapp_without_sending(
    db_session: AsyncSession,
):
    """whatsapp is flagged as unimplemented (Phase 9) — the occurrence
    bookkeeping must still happen so the yearly-fire record stays correct
    once the pending-approval state machine lands."""
    user = await _make_user(db_session, "whatsappuser@example.com", "UTC")
    contact = await _make_contact(db_session, user.id, month=10, day=10)
    await _make_reminder(db_session, user_id=user.id, contact_id=contact.id, channels=["whatsapp"])

    now = datetime(datetime.now(timezone.utc).year, 10, 10, 9, 0, tzinfo=timezone.utc)
    created = await evaluate_reminders(db_session, now=now)
    await db_session.commit()

    assert len(created) == 1
    assert created[0].email_status is None


# ---------------------------------------------------------------------------
# Email retry progression
# ---------------------------------------------------------------------------


async def test_retry_failed_emails_retries_and_can_eventually_succeed(
    db_session: AsyncSession, monkeypatch
):
    user = await _make_user(db_session, "retryuser@example.com", "UTC")
    contact = await _make_contact(db_session, user.id, month=11, day=11)
    reminder = await _make_reminder(db_session, user_id=user.id, contact_id=contact.id, channels=["email"])

    occurrence = ReminderOccurrence(
        reminder_id=reminder.id,
        occurrence_year=2026,
        email_status=EmailStatus.FAILED,
        email_attempts=1,
    )
    db_session.add(occurrence)
    await db_session.flush()

    async def _succeed(*, to_email: str, contact_name: str) -> bool:
        return True

    monkeypatch.setattr("app.services.scheduler.send_reminder_email", _succeed)

    retried = await retry_failed_emails(db_session)
    await db_session.commit()

    assert retried == 1
    await db_session.refresh(occurrence)
    assert occurrence.email_status == EmailStatus.SENT
    assert occurrence.email_attempts == 2
    assert occurrence.email_sent_at is not None


async def test_retry_failed_emails_marks_permanently_failed_after_max_attempts(
    db_session: AsyncSession, monkeypatch
):
    user = await _make_user(db_session, "permafailuser@example.com", "UTC")
    contact = await _make_contact(db_session, user.id, month=12, day=12)
    reminder = await _make_reminder(db_session, user_id=user.id, contact_id=contact.id, channels=["email"])

    occurrence = ReminderOccurrence(
        reminder_id=reminder.id,
        occurrence_year=2026,
        email_status=EmailStatus.FAILED,
        email_attempts=2,  # one more failure hits _MAX_EMAIL_ATTEMPTS == 3
    )
    db_session.add(occurrence)
    await db_session.flush()

    async def _fail(*, to_email: str, contact_name: str) -> bool:
        return False

    monkeypatch.setattr("app.services.scheduler.send_reminder_email", _fail)

    retried = await retry_failed_emails(db_session)
    await db_session.commit()

    assert retried == 1
    await db_session.refresh(occurrence)
    assert occurrence.email_status == EmailStatus.FAILED_PERMANENT
    assert occurrence.email_attempts == 3


async def test_retry_failed_emails_ignores_occurrences_already_at_max_attempts(
    db_session: AsyncSession,
):
    user = await _make_user(db_session, "maxedoutuser@example.com", "UTC")
    contact = await _make_contact(db_session, user.id, month=12, day=13)
    reminder = await _make_reminder(db_session, user_id=user.id, contact_id=contact.id, channels=["email"])

    occurrence = ReminderOccurrence(
        reminder_id=reminder.id,
        occurrence_year=2026,
        email_status=EmailStatus.FAILED,
        email_attempts=3,
    )
    db_session.add(occurrence)
    await db_session.flush()

    retried = await retry_failed_emails(db_session)
    assert retried == 0


# ---------------------------------------------------------------------------
# Distributed lock
# ---------------------------------------------------------------------------


async def test_scheduler_lock_always_true_on_non_postgres(db_session: AsyncSession):
    """The test suite runs on SQLite — there's no advisory-lock equivalent
    and no multi-instance concern, so acquiring must always succeed."""
    assert await _try_acquire_scheduler_lock(db_session) is True
