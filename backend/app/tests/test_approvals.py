"""Tests for the WhatsApp pending-approval state machine (§17) —
app/services/approvals.py and app/api/routers/approvals.py."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.security import hash_password
from app.db.models import (
    ApprovalActor,
    ApprovalChannel,
    ApprovalHistory,
    ApprovalState,
    Contact,
    ContactIdentifier,
    ContactSource,
    PendingApproval,
    Reminder,
    ReminderOccurrence,
    User,
)
from app.services.approvals import (
    approve,
    create_completed_approval,
    create_pending_approval,
    dismiss,
    expire_stale_approvals,
    report_opened,
    update_message,
)

pytestmark = pytest.mark.asyncio


async def _make_user(db_session: AsyncSession, email: str) -> User:
    user = User(
        email=email, password_hash=hash_password("TestPass123!"), email_verified=True,
        failed_login_count=0,
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def _make_approval(db_session: AsyncSession, *, user: User) -> PendingApproval:
    contact = Contact(user_id=user.id, name="WhatsApp Target", source=ContactSource.MANUAL)
    db_session.add(contact)
    await db_session.flush()

    reminder = Reminder(
        user_id=user.id, contact_id=contact.id, lead_time_days=0, channels=["whatsapp"]
    )
    db_session.add(reminder)
    await db_session.flush()

    occurrence = ReminderOccurrence(reminder_id=reminder.id, occurrence_year=2026)
    db_session.add(occurrence)
    await db_session.flush()

    return await create_pending_approval(
        db_session,
        reminder_occurrence_id=occurrence.id,
        user_id=user.id,
        contact_id=contact.id,
    )


async def _make_email_approval(
    db_session: AsyncSession, *, user: User, contact_email: str | None = "target@example.com"
) -> PendingApproval:
    contact = Contact(user_id=user.id, name="Email Target", source=ContactSource.MANUAL)
    db_session.add(contact)
    await db_session.flush()
    if contact_email:
        db_session.add(
            ContactIdentifier(
                contact_id=contact.id,
                type="email",
                normalized_value=contact_email,
                raw_value=contact_email,
            )
        )
        await db_session.flush()

    reminder = Reminder(
        user_id=user.id, contact_id=contact.id, lead_time_days=0, channels=["email"]
    )
    db_session.add(reminder)
    await db_session.flush()

    occurrence = ReminderOccurrence(reminder_id=reminder.id, occurrence_year=2026)
    db_session.add(occurrence)
    await db_session.flush()

    return await create_pending_approval(
        db_session,
        reminder_occurrence_id=occurrence.id,
        user_id=user.id,
        contact_id=contact.id,
        channel=ApprovalChannel.EMAIL,
        message_subject="Happy Birthday!",
        message_body="Hope your day is great.",
    )


# ---------------------------------------------------------------------------
# Service-level: every transition in §17.1
# ---------------------------------------------------------------------------


async def test_create_pending_approval_writes_initial_history_row(db_session: AsyncSession):
    user = await _make_user(db_session, "create-approval@example.com")
    approval = await _make_approval(db_session, user=user)
    await db_session.commit()

    assert approval.state == ApprovalState.PENDING_APPROVAL
    assert approval.expires_at > datetime.now(timezone.utc)

    result = await db_session.execute(
        select(ApprovalHistory).where(ApprovalHistory.approval_id == approval.id)
    )
    history = result.scalars().all()
    assert len(history) == 1
    assert history[0].from_state is None
    assert history[0].to_state == ApprovalState.PENDING_APPROVAL
    assert history[0].actor == ApprovalActor.SCHEDULER


async def test_approve_transitions_pending_to_approved(db_session: AsyncSession):
    user = await _make_user(db_session, "approve-user@example.com")
    approval = await _make_approval(db_session, user=user)

    result = await approve(db_session, approval)
    await db_session.commit()

    assert result.state == ApprovalState.APPROVED


async def test_approve_rejects_non_pending_state(db_session: AsyncSession):
    user = await _make_user(db_session, "approve-twice@example.com")
    approval = await _make_approval(db_session, user=user)
    await approve(db_session, approval)
    await db_session.commit()

    with pytest.raises(AppError) as exc_info:
        await approve(db_session, approval)
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "invalid_approval_transition"


async def test_report_opened_success_transitions_approved_to_completed(
    db_session: AsyncSession,
):
    user = await _make_user(db_session, "report-success@example.com")
    approval = await _make_approval(db_session, user=user)
    await approve(db_session, approval)

    result = await report_opened(db_session, approval, success=True)
    await db_session.commit()

    assert result.state == ApprovalState.COMPLETED


async def test_report_opened_failure_transitions_approved_to_link_failed(
    db_session: AsyncSession,
):
    user = await _make_user(db_session, "report-failure@example.com")
    approval = await _make_approval(db_session, user=user)
    await approve(db_session, approval)

    result = await report_opened(db_session, approval, success=False)
    await db_session.commit()

    assert result.state == ApprovalState.LINK_FAILED


async def test_report_opened_is_idempotent_on_already_settled_approval(
    db_session: AsyncSession,
):
    """A retried report-opened call after the first one already succeeded
    must be a benign no-op, not a 409 (§17.2's explicit exception)."""
    user = await _make_user(db_session, "report-retry@example.com")
    approval = await _make_approval(db_session, user=user)
    await approve(db_session, approval)
    await report_opened(db_session, approval, success=True)
    await db_session.commit()

    result = await report_opened(db_session, approval, success=True)
    await db_session.commit()
    assert result.state == ApprovalState.COMPLETED

    # Only one real transition into COMPLETED should have been recorded.
    history_result = await db_session.execute(
        select(ApprovalHistory).where(
            ApprovalHistory.approval_id == approval.id,
            ApprovalHistory.to_state == ApprovalState.COMPLETED,
        )
    )
    assert len(history_result.scalars().all()) == 1


async def test_report_opened_rejects_when_never_approved(db_session: AsyncSession):
    user = await _make_user(db_session, "report-never-approved@example.com")
    approval = await _make_approval(db_session, user=user)

    with pytest.raises(AppError) as exc_info:
        await report_opened(db_session, approval, success=True)
    assert exc_info.value.status_code == 409


async def test_dismiss_transitions_pending_to_dismissed(db_session: AsyncSession):
    user = await _make_user(db_session, "dismiss-user@example.com")
    approval = await _make_approval(db_session, user=user)

    result = await dismiss(db_session, approval)
    await db_session.commit()

    assert result.state == ApprovalState.DISMISSED


async def test_dismiss_rejects_non_pending_state(db_session: AsyncSession):
    user = await _make_user(db_session, "dismiss-approved@example.com")
    approval = await _make_approval(db_session, user=user)
    await approve(db_session, approval)
    await db_session.commit()

    with pytest.raises(AppError) as exc_info:
        await dismiss(db_session, approval)
    assert exc_info.value.status_code == 409


async def test_expire_stale_approvals_marks_old_pending_items_expired(
    db_session: AsyncSession,
):
    user = await _make_user(db_session, "expire-user@example.com")
    approval = await _make_approval(db_session, user=user)
    approval.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.flush()

    expired_count = await expire_stale_approvals(db_session)
    await db_session.commit()

    assert expired_count == 1
    await db_session.refresh(approval)
    assert approval.state == ApprovalState.EXPIRED


async def test_expire_stale_approvals_ignores_items_not_yet_due(db_session: AsyncSession):
    user = await _make_user(db_session, "not-expired-user@example.com")
    await _make_approval(db_session, user=user)  # expires 7 days from now, not yet due
    await db_session.commit()

    expired_count = await expire_stale_approvals(db_session)
    assert expired_count == 0


async def test_expire_stale_approvals_ignores_already_settled_items(db_session: AsyncSession):
    user = await _make_user(db_session, "settled-user@example.com")
    approval = await _make_approval(db_session, user=user)
    await approve(db_session, approval)
    approval.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.flush()

    expired_count = await expire_stale_approvals(db_session)
    assert expired_count == 0


# ---------------------------------------------------------------------------
# Email channel: approve sends immediately, no report-opened step
# ---------------------------------------------------------------------------


async def test_approve_email_channel_sends_and_completes_directly(db_session: AsyncSession):
    """Unlike whatsapp, email never passes through APPROVED — approve()
    sends via SMTP (no-op in tests, since SMTP_HOST is unset — see
    services/smtp.py) and lands directly on COMPLETED."""
    user = await _make_user(db_session, "email-approve@example.com")
    approval = await _make_email_approval(db_session, user=user)

    result = await approve(db_session, approval)
    await db_session.commit()

    assert result.state == ApprovalState.COMPLETED
    history_result = await db_session.execute(
        select(ApprovalHistory).where(ApprovalHistory.approval_id == approval.id)
    )
    history = history_result.scalars().all()
    # Creation + this one completion — never passes through APPROVED.
    assert [h.to_state for h in history] == [
        ApprovalState.PENDING_APPROVAL,
        ApprovalState.COMPLETED,
    ]


async def test_approve_email_channel_fails_with_no_contact_email(db_session: AsyncSession):
    user = await _make_user(db_session, "email-approve-noemail@example.com")
    approval = await _make_email_approval(db_session, user=user, contact_email=None)

    result = await approve(db_session, approval)
    await db_session.commit()

    assert result.state == ApprovalState.LINK_FAILED


async def test_approve_email_channel_is_retryable_after_link_failed(db_session: AsyncSession):
    """A failed email send is retry-able — the user can tap Send again
    from the same card, unlike whatsapp which only ever approves from
    PENDING_APPROVAL."""
    user = await _make_user(db_session, "email-retry@example.com")
    approval = await _make_email_approval(db_session, user=user, contact_email=None)
    await approve(db_session, approval)  # fails: no contact email
    await db_session.commit()
    assert approval.state == ApprovalState.LINK_FAILED

    # Give it an email now and retry.
    db_session.add(
        ContactIdentifier(
            contact_id=approval.contact_id,
            type="email",
            normalized_value="now-has-email@example.com",
            raw_value="now-has-email@example.com",
        )
    )
    await db_session.flush()

    result = await approve(db_session, approval)
    await db_session.commit()
    assert result.state == ApprovalState.COMPLETED


async def test_report_opened_rejects_email_channel(db_session: AsyncSession):
    """report-opened is whatsapp-only — email never needs it (approve()
    settles directly), and calling it for an email approval is always a
    409, not a silent no-op."""
    user = await _make_user(db_session, "email-reportopened@example.com")
    approval = await _make_email_approval(db_session, user=user)
    await approve(db_session, approval)
    await db_session.commit()

    with pytest.raises(AppError) as exc_info:
        await report_opened(db_session, approval, success=True)
    assert exc_info.value.status_code == 409


async def test_update_message_edits_subject_and_body(db_session: AsyncSession):
    user = await _make_user(db_session, "email-edit@example.com")
    approval = await _make_email_approval(db_session, user=user)

    result = await update_message(
        db_session, approval, message_subject="New subject", message_body="New body"
    )
    await db_session.commit()

    assert result.message_subject == "New subject"
    assert result.message_body == "New body"


async def test_update_message_partial_update_leaves_other_field(db_session: AsyncSession):
    user = await _make_user(db_session, "email-edit-partial@example.com")
    approval = await _make_email_approval(db_session, user=user)

    result = await update_message(db_session, approval, message_subject="New subject only", message_body=None)
    await db_session.commit()

    assert result.message_subject == "New subject only"
    assert result.message_body == "Hope your day is great."  # unchanged


async def test_update_message_rejects_whatsapp_channel(db_session: AsyncSession):
    user = await _make_user(db_session, "whatsapp-edit-rejected@example.com")
    approval = await _make_approval(db_session, user=user)  # whatsapp, no message content

    with pytest.raises(AppError) as exc_info:
        await update_message(db_session, approval, message_subject="x", message_body="y")
    assert exc_info.value.status_code == 409


async def test_update_message_rejects_after_already_sent(db_session: AsyncSession):
    user = await _make_user(db_session, "email-edit-after-send@example.com")
    approval = await _make_email_approval(db_session, user=user)
    await approve(db_session, approval)
    await db_session.commit()

    with pytest.raises(AppError) as exc_info:
        await update_message(db_session, approval, message_subject="too late", message_body=None)
    assert exc_info.value.status_code == 409


async def test_create_completed_approval_records_system_actor_and_success(
    db_session: AsyncSession,
):
    """Auto-send (services/scheduler.py) records its outcome through this
    function — actor SYSTEM, already-settled, no scheduler/user approve
    step in between."""
    user = await _make_user(db_session, "autosend-record@example.com")
    contact = Contact(user_id=user.id, name="Auto Target", source=ContactSource.MANUAL)
    db_session.add(contact)
    await db_session.flush()
    reminder = Reminder(
        user_id=user.id, contact_id=contact.id, lead_time_days=0, channels=["email"]
    )
    db_session.add(reminder)
    await db_session.flush()
    occurrence = ReminderOccurrence(reminder_id=reminder.id, occurrence_year=2026)
    db_session.add(occurrence)
    await db_session.flush()

    approval = await create_completed_approval(
        db_session,
        reminder_occurrence_id=occurrence.id,
        user_id=user.id,
        contact_id=contact.id,
        message_subject="Happy Birthday!",
        message_body="Auto-sent body",
        success=True,
    )
    await db_session.commit()

    assert approval.state == ApprovalState.COMPLETED
    history_result = await db_session.execute(
        select(ApprovalHistory).where(ApprovalHistory.approval_id == approval.id)
    )
    history = history_result.scalars().all()
    assert len(history) == 1
    assert history[0].actor == ApprovalActor.SYSTEM
    assert history[0].to_state == ApprovalState.COMPLETED


# ---------------------------------------------------------------------------
# Expiry floor: must never land before the contact's birthday
# ---------------------------------------------------------------------------


async def test_create_pending_approval_expiry_never_before_birthday(db_session: AsyncSession):
    """A reminder with a long lead time (e.g. 14 days) creates its
    approval well before the birthday — the usual 7-day expiry would
    land before the birthday itself in that case, which must not
    happen."""
    user = await _make_user(db_session, "expiry-floor@example.com")
    contact = Contact(user_id=user.id, name="Future Birthday", source=ContactSource.MANUAL)
    db_session.add(contact)
    await db_session.flush()
    reminder = Reminder(
        user_id=user.id, contact_id=contact.id, lead_time_days=14, channels=["whatsapp"]
    )
    db_session.add(reminder)
    await db_session.flush()
    occurrence = ReminderOccurrence(reminder_id=reminder.id, occurrence_year=2026)
    db_session.add(occurrence)
    await db_session.flush()

    far_future_birthday = date(2026, 12, 25)
    approval = await create_pending_approval(
        db_session,
        reminder_occurrence_id=occurrence.id,
        user_id=user.id,
        contact_id=contact.id,
        birthday_this_occurrence=far_future_birthday,
    )
    await db_session.commit()

    assert approval.expires_at.date() >= far_future_birthday


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


async def _auth_headers(async_client: AsyncClient, db_session: AsyncSession) -> dict[str, str]:
    user = User(
        email="approvalhttpuser@example.com",
        password_hash=hash_password("TestPass123!"),
        email_verified=True,
        failed_login_count=0,
    )
    db_session.add(user)
    await db_session.commit()

    login_resp = await async_client.post(
        "/mobile/auth/login",
        json={"email": "approvalhttpuser@example.com", "password": "TestPass123!"},
    )
    assert login_resp.status_code == 200
    access_token = login_resp.json()["access_token"]
    return {"Authorization": f"Bearer {access_token}"}


async def _get_user_by_email(db_session: AsyncSession, email: str) -> User:
    result = await db_session.execute(select(User).where(User.email == email))
    return result.scalar_one()


async def test_list_approvals_requires_authentication(async_client: AsyncClient):
    resp = await async_client.get("/mobile/approvals")
    assert resp.status_code == 401


async def test_approve_requires_authentication(async_client: AsyncClient):
    resp = await async_client.post(
        "/mobile/approvals/00000000-0000-0000-0000-000000000000/approve"
    )
    assert resp.status_code == 401


async def test_approve_404s_for_approval_not_owned(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)
    other_user = await _make_user(db_session, "not-mine-approval@example.com")
    other_approval = await _make_approval(db_session, user=other_user)
    await db_session.commit()

    resp = await async_client.post(
        f"/mobile/approvals/{other_approval.id}/approve", headers=headers
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "approval_not_found"


async def test_full_approval_flow_end_to_end_via_http(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)
    user = await _get_user_by_email(db_session, "approvalhttpuser@example.com")
    approval = await _make_approval(db_session, user=user)
    await db_session.commit()

    list_resp = await async_client.get("/mobile/approvals", headers=headers)
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1
    assert list_resp.json()["items"][0]["state"] == "pending_approval"

    approve_resp = await async_client.post(
        f"/mobile/approvals/{approval.id}/approve", headers=headers
    )
    assert approve_resp.status_code == 200
    assert approve_resp.json()["state"] == "approved"

    report_resp = await async_client.post(
        f"/mobile/approvals/{approval.id}/report-opened",
        json={"success": True},
        headers=headers,
    )
    assert report_resp.status_code == 200
    assert report_resp.json()["state"] == "completed"


async def test_dismiss_via_http(async_client: AsyncClient, db_session: AsyncSession):
    headers = await _auth_headers(async_client, db_session)
    user = await _get_user_by_email(db_session, "approvalhttpuser@example.com")
    approval = await _make_approval(db_session, user=user)
    await db_session.commit()

    resp = await async_client.post(
        f"/mobile/approvals/{approval.id}/dismiss", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "dismissed"


async def test_approve_already_approved_returns_409_via_http(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)
    user = await _get_user_by_email(db_session, "approvalhttpuser@example.com")
    approval = await _make_approval(db_session, user=user)
    await approve(db_session, approval)
    await db_session.commit()

    resp = await async_client.post(
        f"/mobile/approvals/{approval.id}/approve", headers=headers
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "invalid_approval_transition"


async def test_list_approvals_filters_by_state(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)
    user = await _get_user_by_email(db_session, "approvalhttpuser@example.com")
    pending_approval = await _make_approval(db_session, user=user)
    approved_approval = await _make_approval(db_session, user=user)
    await approve(db_session, approved_approval)
    await db_session.commit()

    resp = await async_client.get(
        "/mobile/approvals", params={"state": "pending_approval"}, headers=headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == str(pending_approval.id)


async def test_update_approval_message_requires_authentication(async_client: AsyncClient):
    resp = await async_client.patch(
        "/mobile/approvals/00000000-0000-0000-0000-000000000000",
        json={"message_body": "x"},
    )
    assert resp.status_code == 401


async def test_update_approval_message_via_http(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)
    user = await _get_user_by_email(db_session, "approvalhttpuser@example.com")
    approval = await _make_email_approval(db_session, user=user)
    await db_session.commit()

    resp = await async_client.patch(
        f"/mobile/approvals/{approval.id}",
        json={"message_subject": "Edited subject", "message_body": "Edited body"},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["message_subject"] == "Edited subject"
    assert body["message_body"] == "Edited body"


async def test_update_approval_message_404s_for_approval_not_owned(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)
    other_user = await _make_user(db_session, "not-mine-email-approval@example.com")
    other_approval = await _make_email_approval(db_session, user=other_user)
    await db_session.commit()

    resp = await async_client.patch(
        f"/mobile/approvals/{other_approval.id}",
        json={"message_body": "nope"},
        headers=headers,
    )
    assert resp.status_code == 404


async def test_email_approval_approve_via_http_completes_directly(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)
    user = await _get_user_by_email(db_session, "approvalhttpuser@example.com")
    approval = await _make_email_approval(db_session, user=user)
    await db_session.commit()

    resp = await async_client.post(
        f"/mobile/approvals/{approval.id}/approve", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "completed"


async def test_email_approval_report_opened_via_http_is_409(
    async_client: AsyncClient, db_session: AsyncSession
):
    headers = await _auth_headers(async_client, db_session)
    user = await _get_user_by_email(db_session, "approvalhttpuser@example.com")
    approval = await _make_email_approval(db_session, user=user)
    await approve(db_session, approval)
    await db_session.commit()

    resp = await async_client.post(
        f"/mobile/approvals/{approval.id}/report-opened",
        json={"success": True},
        headers=headers,
    )
    assert resp.status_code == 409
