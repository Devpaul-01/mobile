"""
services/approvals.py — WhatsApp pending-approval state machine
(MOBILE_APP_ARCHITECTURE_SPEC.md §17).

Every transition here is recorded to ``ApprovalHistory`` (§17.4) — the
single ``_transition`` helper is the only place a ``PendingApproval.state``
is ever written, specifically so no transition can happen without also
being logged.

None of these functions commit — callers (the router, or the scheduler's
expiry sweep) control the transaction, matching the convention used
throughout app/services/scheduler.py and app/services/reminders.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import status

from app.core.errors import AppError, ErrorCodes
from app.db.models import (
    ApprovalActor,
    ApprovalChannel,
    ApprovalHistory,
    ApprovalState,
    PendingApproval,
)

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


async def create_pending_approval(
    db: AsyncSession,
    *,
    reminder_occurrence_id,
    user_id,
    contact_id,
) -> PendingApproval:
    """Called only by the scheduler (app/services/scheduler.py) when a
    firing reminder requests the whatsapp channel — never from an API
    router. Records the creation itself as the first ``ApprovalHistory``
    row (``from_state=None``) so the audit trail's first entry explains
    how the approval came to exist at all."""
    approval = PendingApproval(
        reminder_occurrence_id=reminder_occurrence_id,
        user_id=user_id,
        contact_id=contact_id,
        channel=ApprovalChannel.WHATSAPP,
        state=ApprovalState.PENDING_APPROVAL,
        expires_at=datetime.now(timezone.utc) + timedelta(days=_EXPIRY_DAYS),
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


async def approve(db: AsyncSession, approval: PendingApproval) -> PendingApproval:
    """User tapped "Send via WhatsApp" — recorded *before* the client
    opens the deep link (§17.2), so the transition survives even if the
    user backs out of WhatsApp afterward."""
    if approval.state != ApprovalState.PENDING_APPROVAL:
        raise AppError(
            status.HTTP_409_CONFLICT,
            ErrorCodes.INVALID_APPROVAL_TRANSITION,
            f"Cannot approve an approval in state '{approval.state.value}'",
        )
    await _transition(db, approval, to_state=ApprovalState.APPROVED, actor=ApprovalActor.USER)
    return approval


async def report_opened(
    db: AsyncSession, approval: PendingApproval, *, success: bool
) -> PendingApproval:
    """Client reports whether ``Linking.openURL`` for the WhatsApp deep
    link resolved. ``COMPLETED`` means "we successfully handed off to
    WhatsApp," never "the user actually tapped Send inside WhatsApp" —
    the backend has no way to observe that (§17.2).

    Idempotency exception (§17.2, §43's approval-service handoff task):
    a retry of this exact call arriving after the first one already
    succeeded (e.g. a slow response the client gave up on and resent) is
    a benign no-op, not a 409 — returns the already-settled approval
    unchanged. Only ``APPROVED`` is a genuine transition; any other
    *non-settled* state (still ``PENDING_APPROVAL``, etc.) is a real
    invalid-transition error, not this exception.
    """
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
