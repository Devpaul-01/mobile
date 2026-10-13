"""
services/scheduler.py — Reminder evaluation + occurrence creation (Phase 6,
MOBILE_APP_ARCHITECTURE_SPEC.md §16.2).

Runs in-process via APScheduler, not a separate Celery/RQ worker — see
``init_scheduler`` below and §13.3's rationale (one simple periodic job
doesn't justify a broker). The actual periodic wiring lives in
``init_scheduler``; ``evaluate_reminders`` is the pure, directly-testable
core so tests don't need to wait on or mock a real APScheduler interval.

Trigger-time calculation, precisely
------------------------------------
For each enabled ``Reminder`` whose contact has a known birthday
(month + day — year is never required to fire, since mobile's birthday
data is natively year-less per §15.1):

1. For each of the 3 candidate years ``{today.year - 1, today.year,
   today.year + 1}`` (covers every case ``lead_time_days`` (0-365) can
   wrap into an adjacent calendar year):
   a. Build the birthday date in that candidate year. Feb 29 on a
      non-leap candidate year falls back to Feb 28 — a deliberate,
      documented choice (the architecture doc doesn't specify this edge
      case).
   b. Subtract ``lead_time_days`` to get the trigger date.
   c. Combine with a fixed local time of day (``_REMINDER_LOCAL_HOUR``,
      09:00) in the user's timezone (``User.timezone``, defaulting to UTC
      if unset) to get the trigger *moment*, then convert to UTC.
2. If that moment falls within ``(now - 15min, now]`` — a trailing
   window, matching how a periodic job run at time T should check "what
   became due since the last run," not "what's due starting now" — and no
   ``ReminderOccurrence`` exists yet for ``(reminder_id, candidate_year)``,
   create the occurrence and trigger its downstream actions synchronously.

At most one of the 3 candidate years can match in any single run, since
they're a year apart and the window is only 15 minutes wide.

Downstream actions on a newly-created occurrence
--------------------------------------------------
- ``push`` in ``channels`` → send to every one of the user's registered
  ``PushToken`` rows (services/push.py, stubbed/logged — best-effort,
  never retried, no status tracked per §16.3).
- ``email`` in ``channels`` → services/email.py (stubbed/logged), with
  ``email_status``/``email_attempts``/``email_sent_at`` tracked on the
  occurrence per §16.3's retry-up-to-3-times contract (see
  ``retry_failed_emails`` below for the next-pass retry half of that).
- ``whatsapp`` in ``channels`` → creates a ``PendingApproval`` row (§17,
  Phase 9's state machine — see app/services/approvals.py) rather than
  sending anything itself, and also sends a push notification pointing
  at the Approval Queue (§17.2 — this happens regardless of whether
  ``push`` is itself one of the reminder's own channels). WhatsApp can
  never be auto-sent; this is the hand-off point to the mobile approval
  queue, where the user taps "Send via WhatsApp" themselves.

This module also runs ``expire_stale_approvals`` each cycle (§17.2) —
any approval still ``PENDING_APPROVAL`` after 7 days is marked ``EXPIRED``,
not deleted.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Contact, EmailStatus, PushToken, Reminder, ReminderOccurrence, User
from app.db.session import AsyncSessionLocal
from app.services.approvals import create_pending_approval, expire_stale_approvals
from app.services.email import send_reminder_email
from app.services.push import send_approval_queue_push, send_push_notification

logger = logging.getLogger(__name__)

_WINDOW_MINUTES = 15
_REMINDER_LOCAL_HOUR = 9  # fixed local send time; not specified by the architecture doc
_MAX_EMAIL_ATTEMPTS = 3

# Distributed lock for the scheduler job, per an explicit team decision
# (2026-10-11, see docs/development/implementation-status.md) made before
# this scheduler existed — §13.3's accepted trade-off was "liveness tied
# to one instance," explicitly not a durable multi-instance guarantee.
# Now that the job is real, a Postgres session-level advisory lock closes
# that gap cheaply: no new infrastructure (Postgres is already the DB),
# and the lock auto-releases if the holding connection/process dies, so a
# crash mid-job can't wedge every future run. Arbitrary fixed key, unique
# to this one job.
_SCHEDULER_ADVISORY_LOCK_KEY = 823_746_501


async def _try_acquire_scheduler_lock(db: AsyncSession) -> bool:
    """Returns True if this call holds the lock (or isn't on Postgres, in
    which case there's only ever one test/dev process and no multi-instance
    concern to guard against — SQLite has no advisory-lock equivalent)."""
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return True
    result = await db.execute(
        text("SELECT pg_try_advisory_lock(:key)"), {"key": _SCHEDULER_ADVISORY_LOCK_KEY}
    )
    return bool(result.scalar())


async def _release_scheduler_lock(db: AsyncSession) -> None:
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return
    await db.execute(
        text("SELECT pg_advisory_unlock(:key)"), {"key": _SCHEDULER_ADVISORY_LOCK_KEY}
    )


def _birthday_in_year(month: int, day: int, year: int) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        if month == 2 and day == 29:
            return date(year, 2, 28)
        raise


def _trigger_moment_utc(
    *, month: int, day: int, candidate_year: int, lead_time_days: int, tz: ZoneInfo
) -> datetime:
    trigger_date = _birthday_in_year(month, day, candidate_year) - timedelta(days=lead_time_days)
    local_moment = datetime.combine(trigger_date, time(_REMINDER_LOCAL_HOUR, 0), tzinfo=tz)
    return local_moment.astimezone(timezone.utc)


async def evaluate_reminders(
    db: AsyncSession, *, now: datetime | None = None
) -> list[ReminderOccurrence]:
    """Check every enabled reminder for a trigger moment in the trailing
    15-minute window ending at ``now`` (defaults to the real current UTC
    time), creating and returning any newly-fired ``ReminderOccurrence``
    rows. Does not commit — the caller controls the transaction, so a test
    or the real scheduler job can inspect results before deciding."""
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=_WINDOW_MINUTES)

    result = await db.execute(
        select(Reminder)
        .join(Contact, Contact.id == Reminder.contact_id)
        .where(
            Reminder.enabled.is_(True),
            Contact.deleted_at.is_(None),
            Contact.birthday_month.is_not(None),
            Contact.birthday_day.is_not(None),
        )
        .options(selectinload(Reminder.contact))
    )
    reminders = result.scalars().all()

    created: list[ReminderOccurrence] = []
    for reminder in reminders:
        contact = reminder.contact
        # The query above filters birthday_month/birthday_day IS NOT NULL,
        # so these are never None at runtime — asserted for mypy, which
        # can't see that narrowing through the SQL WHERE clause.
        assert contact.birthday_month is not None
        assert contact.birthday_day is not None
        user_result = await db.execute(select(User).where(User.id == reminder.user_id))
        user = user_result.scalar_one_or_none()
        if user is None:
            continue
        tz = ZoneInfo(user.timezone) if user.timezone else timezone.utc

        for candidate_year in (now.year - 1, now.year, now.year + 1):
            trigger_moment = _trigger_moment_utc(
                month=contact.birthday_month,
                day=contact.birthday_day,
                candidate_year=candidate_year,
                lead_time_days=reminder.lead_time_days,
                tz=tz,  # type: ignore[arg-type]
            )
            if not (window_start < trigger_moment <= now):
                continue

            occurrence = await _create_occurrence_idempotent(
                db, reminder=reminder, occurrence_year=candidate_year
            )
            if occurrence is not None:
                await _trigger_downstream_actions(db, reminder=reminder, contact=contact, user=user, occurrence=occurrence)
                created.append(occurrence)
            break  # at most one candidate year can match per reminder per run

    return created


async def _create_occurrence_idempotent(
    db: AsyncSession, *, reminder: Reminder, occurrence_year: int
) -> ReminderOccurrence | None:
    """Insert the occurrence row, relying on the DB's unique constraint on
    ``(reminder_id, occurrence_year)`` as the actual idempotency guarantee
    (not a pre-check SELECT, which would race under concurrent scheduler
    runs) — a violation means another run already handled this occurrence,
    so it's treated as "nothing to do," not an error.

    Wrapped in a SAVEPOINT (``begin_nested``): a plain ``db.rollback()``
    here would roll back the *entire* transaction, destroying any
    occurrences this same ``evaluate_reminders`` call already created for
    other reminders earlier in the loop, before the caller ever gets to
    commit. A savepoint confines the rollback to just this one insert.
    """
    occurrence = ReminderOccurrence(reminder_id=reminder.id, occurrence_year=occurrence_year)
    try:
        async with db.begin_nested():
            db.add(occurrence)
            await db.flush()
    except IntegrityError:
        return None
    return occurrence


async def _trigger_downstream_actions(
    db: AsyncSession,
    *,
    reminder: Reminder,
    contact: Contact,
    user: User,
    occurrence: ReminderOccurrence,
) -> None:
    push_tokens: list[PushToken] | None = None

    async def _load_push_tokens() -> list[PushToken]:
        nonlocal push_tokens
        if push_tokens is None:
            result = await db.execute(select(PushToken).where(PushToken.user_id == user.id))
            push_tokens = list(result.scalars().all())
        return push_tokens

    if "push" in reminder.channels:
        for push_token in await _load_push_tokens():
            await send_push_notification(
                device_token=push_token.device_token, contact_name=contact.name
            )

    if "email" in reminder.channels:
        occurrence.email_status = EmailStatus.PENDING
        occurrence.email_attempts = 1
        success = await send_reminder_email(to_email=user.email, contact_name=contact.name)
        occurrence.email_status = EmailStatus.SENT if success else EmailStatus.FAILED
        if success:
            occurrence.email_sent_at = datetime.now(timezone.utc)
        await db.flush()

    if "whatsapp" in reminder.channels:
        await create_pending_approval(
            db,
            reminder_occurrence_id=occurrence.id,
            user_id=user.id,
            contact_id=contact.id,
        )
        # §17.2: creating a PendingApproval also triggers a push pointing
        # at the Approval Queue — regardless of whether "push" is itself
        # one of this reminder's own channels.
        for push_token in await _load_push_tokens():
            await send_approval_queue_push(
                device_token=push_token.device_token, contact_name=contact.name
            )


async def retry_failed_emails(db: AsyncSession) -> int:
    """Retry occurrences whose email send failed, up to
    ``_MAX_EMAIL_ATTEMPTS`` total attempts (§16.3) — run this on the same
    periodic cadence as ``evaluate_reminders``, separately, since it
    revisits old occurrences rather than scanning reminders. Returns the
    number of occurrences retried this pass."""
    result = await db.execute(
        select(ReminderOccurrence)
        .where(
            ReminderOccurrence.email_status == EmailStatus.FAILED,
            ReminderOccurrence.email_attempts < _MAX_EMAIL_ATTEMPTS,
        )
        .options(selectinload(ReminderOccurrence.reminder).selectinload(Reminder.contact))
    )
    occurrences = result.scalars().all()

    retried = 0
    for occurrence in occurrences:
        reminder = occurrence.reminder
        contact = reminder.contact
        user_result = await db.execute(select(User).where(User.id == reminder.user_id))
        user = user_result.scalar_one_or_none()
        if user is None:
            continue

        occurrence.email_attempts += 1
        success = await send_reminder_email(to_email=user.email, contact_name=contact.name)
        if success:
            occurrence.email_status = EmailStatus.SENT
            occurrence.email_sent_at = datetime.now(timezone.utc)
        elif occurrence.email_attempts >= _MAX_EMAIL_ATTEMPTS:
            occurrence.email_status = EmailStatus.FAILED_PERMANENT
        retried += 1

    await db.flush()
    return retried


async def run_scheduler_cycle() -> None:
    """The actual periodic job body — opens its own session (this runs
    outside any HTTP request), takes the distributed lock, evaluates
    reminders, retries failed emails, sweeps stale approvals, commits,
    and always releases the lock and closes the session. This is what
    ``init_scheduler`` below points APScheduler at; call it directly in
    a one-off script/shell if you ever need to trigger a cycle by hand.
    """
    async with AsyncSessionLocal() as db:
        acquired = await _try_acquire_scheduler_lock(db)
        if not acquired:
            logger.info(
                "Scheduler lock held by another instance — skipping this cycle."
            )
            return
        try:
            created = await evaluate_reminders(db)
            retried = await retry_failed_emails(db)
            expired = await expire_stale_approvals(db)
            await db.commit()
            if created or retried or expired:
                logger.info(
                    "Scheduler cycle: %d occurrence(s) created, %d email retry(ies), "
                    "%d approval(s) expired.",
                    len(created),
                    retried,
                    expired,
                )
        except Exception:
            await db.rollback()
            logger.exception("Scheduler cycle failed")
        finally:
            await _release_scheduler_lock(db)


def init_scheduler():
    """Create and start the APScheduler background job, called once from
    main.py's startup if ``settings.scheduler_enabled`` (off by default in
    tests — see app/tests/conftest.py). Returns the scheduler instance so
    the caller can shut it down cleanly.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_scheduler_cycle,
        "interval",
        minutes=_WINDOW_MINUTES,
        id="reminder_evaluation",
        max_instances=1,  # a slow run must finish before the next one starts
        coalesce=True,  # if runs were missed, run once on catch-up, not once per miss
    )
    scheduler.start()
    return scheduler
