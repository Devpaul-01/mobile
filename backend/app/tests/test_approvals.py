"""Tests for the WhatsApp pending-approval state machine (§17) —
app/services/approvals.py and app/api/routers/approvals.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.security import hash_password
from app.db.models import (
    ApprovalActor,
    ApprovalHistory,
    ApprovalState,
    Contact,
    ContactSource,
    PendingApproval,
    Reminder,
    ReminderOccurrence,
    User,
)
from app.services.approvals import (
    approve,
    create_pending_approval,
    dismiss,
    expire_stale_approvals,
    report_opened,
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
