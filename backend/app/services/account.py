"""
services/account.py — GDPR export / account deletion
(MOBILE_APP_ARCHITECTURE_SPEC.md §27, Phase 13).

Neither function commits — the router controls the transaction, matching
the convention in app/services/approvals.py and app/services/payments.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ApprovalHistory,
    Contact,
    ContactIdentifier,
    ContactPhoto,
    PendingApproval,
    Payment,
    PushToken,
    Reminder,
    ReminderOccurrence,
    Subscription,
    User,
)
from app.services import storage


async def export_user_data(db: AsyncSession, user: User) -> dict[str, Any]:
    """Builds the full export document (§27.4): "profile, contacts +
    identifiers, reminders + occurrence history, approval history,
    payment/subscription summary (not raw PayPal data)." Contact photos
    aren't named in §27.4's list explicitly, but are included here too —
    §27.1 frames the export's shape as "covering every owned entity
    type," and a photo is an owned entity like any other.

    "Not raw PayPal data" (§27.4) is honored by omitting
    ``provider_order_id``/``provider_subscription_id`` from the payment/
    subscription summary below — those are PayPal-internal identifiers,
    meaningless to the user reading their own export, not data about
    them.
    """
    contacts_result = await db.execute(
        select(Contact).where(Contact.user_id == user.id).order_by(Contact.created_at)
    )
    contacts = contacts_result.scalars().all()

    contacts_export = []
    for contact in contacts:
        identifiers_result = await db.execute(
            select(ContactIdentifier).where(ContactIdentifier.contact_id == contact.id)
        )
        identifiers = identifiers_result.scalars().all()

        photo_result = await db.execute(
            select(ContactPhoto).where(ContactPhoto.contact_id == contact.id)
        )
        photo = photo_result.scalar_one_or_none()

        contacts_export.append(
            {
                "id": str(contact.id),
                "name": contact.name,
                "birthday_month": contact.birthday_month,
                "birthday_day": contact.birthday_day,
                "birthday_year": contact.birthday_year,
                "source": contact.source.value,
                "notes": contact.notes,
                "deleted_at": contact.deleted_at.isoformat() if contact.deleted_at else None,
                "created_at": contact.created_at.isoformat(),
                "identifiers": [
                    {
                        "type": identifier.type.value,
                        "normalized_value": identifier.normalized_value,
                        "raw_value": identifier.raw_value,
                        "normalization_failed": identifier.normalization_failed,
                        "created_at": identifier.created_at.isoformat(),
                    }
                    for identifier in identifiers
                ],
                "photo": (
                    {
                        "content_type": photo.content_type,
                        "size_bytes": photo.size_bytes,
                        "uploaded_at": photo.uploaded_at.isoformat(),
                    }
                    if photo is not None
                    else None
                ),
            }
        )

    reminders_result = await db.execute(
        select(Reminder).where(Reminder.user_id == user.id).order_by(Reminder.created_at)
    )
    reminders = reminders_result.scalars().all()

    reminders_export = []
    for reminder in reminders:
        occurrences_result = await db.execute(
            select(ReminderOccurrence)
            .where(ReminderOccurrence.reminder_id == reminder.id)
            .order_by(ReminderOccurrence.occurrence_year)
        )
        occurrences = occurrences_result.scalars().all()
        reminders_export.append(
            {
                "id": str(reminder.id),
                "contact_id": str(reminder.contact_id),
                "lead_time_days": reminder.lead_time_days,
                "channels": reminder.channels,
                "enabled": reminder.enabled,
                "created_at": reminder.created_at.isoformat(),
                "occurrences": [
                    {
                        "occurrence_year": occurrence.occurrence_year,
                        "fired_at": occurrence.fired_at.isoformat(),
                        "email_status": (
                            occurrence.email_status.value if occurrence.email_status else None
                        ),
                        "email_attempts": occurrence.email_attempts,
                        "email_sent_at": (
                            occurrence.email_sent_at.isoformat()
                            if occurrence.email_sent_at
                            else None
                        ),
                    }
                    for occurrence in occurrences
                ],
            }
        )

    approvals_result = await db.execute(
        select(PendingApproval)
        .where(PendingApproval.user_id == user.id)
        .order_by(PendingApproval.created_at)
    )
    approvals = approvals_result.scalars().all()

    approvals_export = []
    for approval in approvals:
        history_result = await db.execute(
            select(ApprovalHistory)
            .where(ApprovalHistory.approval_id == approval.id)
            .order_by(ApprovalHistory.at)
        )
        history = history_result.scalars().all()
        approvals_export.append(
            {
                "id": str(approval.id),
                "contact_id": str(approval.contact_id),
                "channel": approval.channel.value,
                "state": approval.state.value,
                "created_at": approval.created_at.isoformat(),
                "expires_at": approval.expires_at.isoformat(),
                "history": [
                    {
                        "from_state": entry.from_state.value if entry.from_state else None,
                        "to_state": entry.to_state.value,
                        "actor": entry.actor.value,
                        "at": entry.at.isoformat(),
                    }
                    for entry in history
                ],
            }
        )

    subscription_result = await db.execute(
        select(Subscription).where(Subscription.user_id == user.id)
    )
    subscription = subscription_result.scalar_one_or_none()
    subscription_export = None
    if subscription is not None:
        payments_result = await db.execute(
            select(Payment)
            .where(Payment.subscription_id == subscription.id)
            .order_by(Payment.created_at)
        )
        payments = payments_result.scalars().all()
        subscription_export = {
            "plan": subscription.plan,
            "status": subscription.status.value,
            "payment_provider": subscription.payment_provider.value,
            "current_period_end": (
                subscription.current_period_end.isoformat()
                if subscription.current_period_end
                else None
            ),
            "cancel_at_period_end": subscription.cancel_at_period_end,
            "payments": [
                {
                    "amount": str(payment.amount),
                    "currency": payment.currency,
                    "status": payment.status.value,
                    "captured_at": payment.captured_at.isoformat() if payment.captured_at else None,
                    "created_at": payment.created_at.isoformat(),
                }
                for payment in payments
            ],
        }

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "profile": {
            "id": str(user.id),
            "email": user.email,
            "name": user.name,
            "timezone": user.timezone,
            "created_at": user.created_at.isoformat(),
        },
        "contacts": contacts_export,
        "reminders": reminders_export,
        "approvals": approvals_export,
        "subscription": subscription_export,
    }


async def delete_account(db: AsyncSession, user: User) -> dict[str, int]:
    """§27.2's ordered deletion workflow. Returns a small summary dict
    (counts) purely so callers/tests can assert on what happened — the
    response the mobile client actually gets back is just a success
    message (§35).

    Step 1 ("cancel any active PayPal subscription") is, under this
    codebase's Orders-API-only payment implementation (Session 7 —
    app/services/paypal.py's module docstring), **genuinely a no-op**:
    there is no recurring billing agreement to cancel via PayPal's API,
    only a local entitlement window, and that local ``Subscription`` row
    is deleted along with everything else by the FK cascade below in
    this same transaction regardless of what status it holds beforehand
    — there is no external call to make, and no observable difference
    between flipping its status first and not doing so, since nothing
    reads it in between. This is flagged explicitly as a consequence of
    that earlier payment-model decision, not an oversight: the step
    exists in the architecture doc only because a *real* recurring
    agreement would need cancelling before its id becomes unreachable,
    which isn't this milestone's situation.

    Step 2 ("revoke all push tokens") needs no external Expo call either
    — Expo's push service has no "invalidate this token" API; a token
    simply stops being used once its ``PushToken`` row is gone, which the
    FK cascade below handles.

    Step 3 (S3 object deletion) runs synchronously here, not via an async
    task queue — this codebase deliberately has no task queue (§13.3's
    APScheduler-over-Celery decision), and a single prefix-delete call is
    cheap enough not to need one. If it fails for some reason (storage
    reachable but erroring) that failure is allowed to propagate and
    abort the deletion entirely — ``delete_objects_by_prefix`` only
    quietly no-ops when storage isn't *configured* at all (a deployment
    with no object storage set up shouldn't be unable to delete
    accounts), not when a configured store actually errors.
    """
    push_token_result = await db.execute(
        select(PushToken.id).where(PushToken.user_id == user.id)
    )
    push_token_count = len(push_token_result.scalars().all())

    deleted_objects = storage.delete_objects_by_prefix(f"{user.id}/")

    await db.delete(user)
    await db.flush()

    return {"push_tokens_revoked": push_token_count, "storage_objects_deleted": deleted_objects}
