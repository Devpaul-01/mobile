"""
services/approvals.py — Pending-approval state machine for birthday
message sends (MOBILE_APP_ARCHITECTURE_SPEC.md §17; widened from
whatsapp-only to also cover email, per an explicit team decision).

Every transition here is recorded to ``ApprovalHistory`` (§17.4) — the
single ``_transition`` helper is the only place a ``PendingApproval.state``
is ever written, specifically so no transition can happen without also
being logged.

None of these functions commit — callers (the router, or the scheduler's
expiry sweep) control the transaction, matching the convention used
throughout app/services/scheduler.py and app/services/reminders.py.

Channel differences, in one place
-----------------------------------
``whatsapp``: unchanged. ``approve()`` only records the user's tap;
``report_opened()`` is still required afterward, since the backend has
no way to confirm a ``wa.me`` deep link actually opened.

``email``: ``approve()`` sends immediately via ``services/smtp.py`` and
lands directly on ``completed``/``link_failed`` — there is no "did the
link open" uncertainty to track for an SMTP send the backend itself
performs, so ``report_opened`` is never used for this channel (calling
it on an email-channel approval is a 409, same as calling it on an
already-settled one).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import status

from app.core.errors import AppError, ErrorCodes
from app.db.models import (
    ApprovalActor,
    ApprovalChannel,
    ApprovalHistory,
    ApprovalState,
    ContactIdentifier,
    ContactIdentifierType,
    PendingApproval,
)
from app.services.smtp import send_email

_EXPIRY_DAYS = 7  # §17.2 — "a week-old birthday reminder has diminishing value"


async def _transition(
    db: AsyncSession,
    approval: PendingApproval,
    *,
    to_state: ApprovalState,
    actor: ApprovalActor,
) -> None:
    db.add(
        ApprovalHistory(
            approval_id=approval.id,
            from_state=approval.state,
            to_state=to_state,
            actor=actor,
        )
    )
    approval.state = to_state
    approval.updated_at = datetime.now(timezone.utc)
    await db.flush()


def _expiry_floor(birthday_this_occurrence: date | None) -> datetime:
    """Expiry must never land before the contact's birthday itself — an
    approval created with a long lead time (e.g. 14 days ahead) would
    otherwise expire *before* the day it's actually for. Returns
    whichever is later: the usual 7-day window, or end-of-day (UTC) on
    the birthday. ``birthday_this_occurrence`` is None only for
    call sites that predate this floor existing (none in this codebase
    today) — falls back to the plain 7-day window in that case."""
    now = datetime.now(timezone.utc)
    default_expiry = now + timedelta(days=_EXPIRY_DAYS)
    if birthday_this_occurrence is None:
        return default_expiry
    end_of_birthday = datetime.combine(
        birthday_this_occurrence, datetime.max.time(), tzinfo=timezone.utc
    )
    return max(default_expiry, end_of_birthday)


async def create_pending_approval(
    db: AsyncSession,
    *,
    reminder_occurrence_id,
    user_id,
    contact_id,
    channel: ApprovalChannel = ApprovalChannel.WHATSAPP,
    message_subject: str | None = None,
    message_body: str | None = None,
    birthday_this_occurrence: date | None = None,
) -> PendingApproval:
    """Called only by the scheduler (app/services/scheduler.py) when a
    firing reminder requests the whatsapp channel, or when its email
    channel isn't eligible for auto-send — never from an API router.
    Records the creation itself as the first ``ApprovalHistory`` row
    (``from_state=None``) so the audit trail's first entry explains how
    the approval came to exist at all.

    ``message_subject``/``message_body`` are only meaningful for
    ``channel=EMAIL`` (see services/message_templates.py) — left ``None``
    for whatsapp, which still composes its message client-side (§20.3).
    ``birthday_this_occurrence`` bounds how late this can expire — see
    ``_expiry_floor``."""
    approval = PendingApproval(
        reminder_occurrence_id=reminder_occurrence_id,
        user_id=user_id,
        contact_id=contact_id,
        channel=channel,
        state=ApprovalState.PENDING_APPROVAL,
        expires_at=_expiry_floor(birthday_this_occurrence),
        message_subject=message_subject,
        message_body=message_body,
    )
    db.add(approval)
    await db.flush()
    db.add(
        ApprovalHistory(
            approval_id=approval.id,
            from_state=None,
            to_state=ApprovalState.PENDING_APPROVAL,
            actor=ApprovalActor.SCHEDULER,
        )
    )
    await db.flush()
    return approval


async def create_completed_approval(
    db: AsyncSession,
    *,
    reminder_occurrence_id,
    user_id,
    contact_id,
    message_subject: str,
    message_body: str,
    success: bool,
    birthday_this_occurrence: date | None = None,
) -> PendingApproval:
    """Record an auto-send (``User.auto_send_email=True``, see
    services/scheduler.py) — created already settled
    (``completed``/``link_failed``), actor ``system``, so both the
    manual (approve) and automatic send paths for email land in the same
    table and the same audit trail. Only ever used for ``channel=EMAIL``;
    whatsapp can never be auto-sent."""
    final_state = ApprovalState.COMPLETED if success else ApprovalState.LINK_FAILED
    approval = PendingApproval(
        reminder_occurrence_id=reminder_occurrence_id,
        user_id=user_id,
        contact_id=contact_id,
        channel=ApprovalChannel.EMAIL,
        state=final_state,
        expires_at=_expiry_floor(birthday_this_occurrence),
        message_subject=message_subject,
        message_body=message_body,
    )
    db.add(approval)
    await db.flush()
    db.add(
        ApprovalHistory(
            approval_id=approval.id,
            from_state=None,
            to_state=final_state,
            actor=ApprovalActor.SYSTEM,
        )
    )
    await db.flush()
    return approval


async def approve(db: AsyncSession, approval: PendingApproval) -> PendingApproval:
    """User tapped "Send".

    ``whatsapp``: unchanged — records the tap *before* the client opens
    the deep link (§17.2), so the transition survives even if the user
    backs out of WhatsApp afterward. Settles later via
    ``report_opened()``.

    ``email``: sends immediately via SMTP, using the approval's own
    (possibly user-edited) ``message_subject``/``message_body`` and the
    contact's current email — there is no intermediate "awaiting the
    link to open" state to pass through, so this lands directly on
    ``completed``/``link_failed``. A ``link_failed`` result is
    retry-able: the user can tap Send again from the same (still
    ``link_failed``) card.
    """
    if approval.channel == ApprovalChannel.EMAIL:
        # Retry-able: a previous send attempt landing on LINK_FAILED can
        # be re-approved (the user tapping Send again on the same card),
        # unlike whatsapp below, which only ever starts from
        # PENDING_APPROVAL.
        if approval.state not in (ApprovalState.PENDING_APPROVAL, ApprovalState.LINK_FAILED):
            raise AppError(
                status.HTTP_409_CONFLICT,
                ErrorCodes.INVALID_APPROVAL_TRANSITION,
                f"Cannot approve an approval in state '{approval.state.value}'",
            )
        return await _send_email_approval(db, approval)

    if approval.state != ApprovalState.PENDING_APPROVAL:
        raise AppError(
            status.HTTP_409_CONFLICT,
            ErrorCodes.INVALID_APPROVAL_TRANSITION,
            f"Cannot approve an approval in state '{approval.state.value}'",
        )
    await _transition(db, approval, to_state=ApprovalState.APPROVED, actor=ApprovalActor.USER)
    return approval


async def _send_email_approval(db: AsyncSession, approval: PendingApproval) -> PendingApproval:
    """The actual SMTP send for an email-channel approval — see
    ``approve()``'s docstring for why this skips the
    approved/report-opened steps whatsapp needs."""
    contact_email = await _contact_email(db, approval)
    if not contact_email:
        await _transition(
            db, approval, to_state=ApprovalState.LINK_FAILED, actor=ApprovalActor.USER
        )
        return approval

    try:
        await send_email(
            to_email=contact_email,
            subject=approval.message_subject or "",
            body=approval.message_body or "",
        )
        await _transition(
            db, approval, to_state=ApprovalState.COMPLETED, actor=ApprovalActor.USER
        )
    except Exception:
        await _transition(
            db, approval, to_state=ApprovalState.LINK_FAILED, actor=ApprovalActor.USER
        )
    return approval


async def _contact_email(db: AsyncSession, approval: PendingApproval) -> str | None:
    """The contact's current email, queried fresh rather than off a
    (possibly not-loaded, and possibly stale by send time)
    relationship — this always reflects whatever the contact's email
    was most recently edited to, including an edit made after the
    ``PendingApproval`` row was created (§3.3 of the redesign doc:
    editing a contact's email goes through the normal contact-update
    path, not a field on this table)."""
    result = await db.execute(
        select(ContactIdentifier.normalized_value)
        .where(
            ContactIdentifier.contact_id == approval.contact_id,
            ContactIdentifier.type == ContactIdentifierType.EMAIL,
        )
        .order_by(ContactIdentifier.created_at.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def report_opened(
    db: AsyncSession, approval: PendingApproval, *, success: bool
) -> PendingApproval:
    """Client reports whether ``Linking.openURL`` for the WhatsApp deep
    link resolved. ``COMPLETED`` means "we successfully handed off to
    WhatsApp," never "the user actually tapped Send inside WhatsApp" —
    the backend has no way to observe that (§17.2).

    ``whatsapp`` only — an ``email``-channel approval never passes
    through ``approved`` (see ``approve()``), so calling this for one is
    always a 409, same as calling it on an already-settled approval of
    any channel.

    Idempotency exception (§17.2, §43's approval-service handoff task):
    a retry of this exact call arriving after the first one already
    succeeded (e.g. a slow response the client gave up on and resent) is
    a benign no-op, not a 409 — returns the already-settled approval
    unchanged. Only ``APPROVED`` is a genuine transition; any other
    *non-settled* state (still ``PENDING_APPROVAL``, etc.) is a real
    invalid-transition error, not this exception.
    """
    if approval.channel != ApprovalChannel.WHATSAPP:
        raise AppError(
            status.HTTP_409_CONFLICT,
            ErrorCodes.INVALID_APPROVAL_TRANSITION,
            f"report-opened is not used for the '{approval.channel.value}' channel",
        )

    if approval.state == ApprovalState.APPROVED:
        to_state = ApprovalState.COMPLETED if success else ApprovalState.LINK_FAILED
        await _transition(db, approval, to_state=to_state, actor=ApprovalActor.USER)
        return approval

    if approval.state in (ApprovalState.COMPLETED, ApprovalState.LINK_FAILED):
        return approval  # benign retry — already settled, nothing to do

    raise AppError(
        status.HTTP_409_CONFLICT,
        ErrorCodes.INVALID_APPROVAL_TRANSITION,
        f"Cannot report-opened for an approval in state '{approval.state.value}'",
    )


async def update_message(
    db: AsyncSession,
    approval: PendingApproval,
    *,
    message_subject: str | None,
    message_body: str | None,
) -> PendingApproval:
    """Let the user edit the composed draft before sending
    (``PATCH /mobile/approvals/{id}``). Email only — whatsapp has no
    stored message to edit (§20.3, composed client-side). Only while
    still ``PENDING_APPROVAL``; once sent there's nothing left to edit,
    and a ``LINK_FAILED`` retry re-sends whatever was last saved rather
    than reopening editing."""
    if approval.channel != ApprovalChannel.EMAIL:
        raise AppError(
            status.HTTP_409_CONFLICT,
            ErrorCodes.INVALID_APPROVAL_TRANSITION,
            f"Editing the message is not supported for the '{approval.channel.value}' channel",
        )
    if approval.state != ApprovalState.PENDING_APPROVAL:
        raise AppError(
            status.HTTP_409_CONFLICT,
            ErrorCodes.INVALID_APPROVAL_TRANSITION,
            f"Cannot edit an approval in state '{approval.state.value}'",
        )
    if message_subject is not None:
        approval.message_subject = message_subject
    if message_body is not None:
        approval.message_body = message_body
    approval.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return approval


async def dismiss(db: AsyncSession, approval: PendingApproval) -> PendingApproval:
    """User explicitly dismissed from the approval queue UI without
    sending — distinguished from EXPIRED for analytics (§17.2: did the
    user see it and choose not to act, vs. never see it at all)."""
    if approval.state != ApprovalState.PENDING_APPROVAL:
        raise AppError(
            status.HTTP_409_CONFLICT,
            ErrorCodes.INVALID_APPROVAL_TRANSITION,
            f"Cannot dismiss an approval in state '{approval.state.value}'",
        )
    await _transition(db, approval, to_state=ApprovalState.DISMISSED, actor=ApprovalActor.USER)
    return approval


async def expire_stale_approvals(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Scheduler sweep (§17.2): any approval still ``PENDING_APPROVAL``
    after ``_EXPIRY_DAYS`` is marked ``EXPIRED``, not deleted — the audit
    trail is kept. Run this on the same periodic cadence as the reminder
    evaluation (see app/services/scheduler.py run_scheduler_cycle)."""
    now = now or datetime.now(timezone.utc)

    result = await db.execute(
        select(PendingApproval).where(
            PendingApproval.state == ApprovalState.PENDING_APPROVAL,
            PendingApproval.expires_at < now,
        )
    )
    stale = result.scalars().all()

    for approval in stale:
        await _transition(
            db, approval, to_state=ApprovalState.EXPIRED, actor=ApprovalActor.SCHEDULER
        )

    return len(stale)
