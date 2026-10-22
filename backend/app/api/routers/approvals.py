"""
api/routers/approvals.py — Pending-approval queue (§17; whatsapp + email).

Mounted under ``/mobile/approvals`` (set in main.py).

Endpoints
---------
GET   /mobile/approvals                  List the user's approvals
                                          (paginated, optional ``state``
                                          filter).
PATCH /mobile/approvals/{id}             Edit the composed draft before
                                          sending (email channel only).
POST  /mobile/approvals/{id}/approve     User taps "Send". For
                                          whatsapp, records the tap
                                          before the deep link opens.
                                          For email, sends immediately
                                          via SMTP and settles directly
                                          (see services/approvals.py).
POST  /mobile/approvals/{id}/report-opened
                                          Whatsapp only — client reports
                                          whether the deep link opened.
                                          409 for an email-channel
                                          approval (see
                                          services/approvals.py).
POST  /mobile/approvals/{id}/dismiss     User dismisses without sending.

There is no create endpoint — a ``PendingApproval`` only ever comes from
the scheduler (app/services/scheduler.py).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.errors import AppError, ErrorCodes
from app.db.models import ApprovalState, PendingApproval, User
from app.db.session import get_db
from app.schemas.approval import (
    ApprovalListResponse,
    ApprovalMessageUpdate,
    ApprovalOut,
    ReportOpenedRequest,
)
from app.schemas.common import ErrorResponse
from app.services.approvals import approve as approve_service
from app.services.approvals import dismiss as dismiss_service
from app.services.approvals import report_opened as report_opened_service
from app.services.approvals import update_message as update_message_service

_ERROR_RESPONSES: dict[int | str, dict] = {
    401: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}

router = APIRouter(responses=_ERROR_RESPONSES)


async def _get_owned_approval(
    approval_id: UUID, current_user: User, db: AsyncSession
) -> PendingApproval:
    result = await db.execute(
        select(PendingApproval).where(
            PendingApproval.id == approval_id, PendingApproval.user_id == current_user.id
        )
    )
    approval = result.scalar_one_or_none()
    if approval is None:
        raise AppError(
            status.HTTP_404_NOT_FOUND, ErrorCodes.APPROVAL_NOT_FOUND, "Approval not found"
        )
    return approval


@router.get("", response_model=ApprovalListResponse)
async def list_approvals(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    state: ApprovalState | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ApprovalListResponse:
    conditions = [PendingApproval.user_id == current_user.id]
    if state is not None:
        conditions.append(PendingApproval.state == state)

    count_result = await db.execute(
        select(func.count(PendingApproval.id)).where(*conditions)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(PendingApproval)
        .where(*conditions)
        .order_by(PendingApproval.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    items = result.scalars().all()
    return ApprovalListResponse(
        items=[ApprovalOut.model_validate(a) for a in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.patch("/{approval_id}", response_model=ApprovalOut)
async def update_approval_message(
    approval_id: UUID,
    data: ApprovalMessageUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApprovalOut:
    approval = await _get_owned_approval(approval_id, current_user, db)
    approval = await update_message_service(
        db,
        approval,
        message_subject=data.message_subject,
        message_body=data.message_body,
    )
    await db.commit()
    await db.refresh(approval)
    return ApprovalOut.model_validate(approval)


@router.post("/{approval_id}/approve", response_model=ApprovalOut)
async def approve_approval(
    approval_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApprovalOut:
    approval = await _get_owned_approval(approval_id, current_user, db)
    approval = await approve_service(db, approval)
    await db.commit()
    await db.refresh(approval)
    return ApprovalOut.model_validate(approval)


@router.post("/{approval_id}/report-opened", response_model=ApprovalOut)
async def report_opened_endpoint(
    approval_id: UUID,
    data: ReportOpenedRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApprovalOut:
    approval = await _get_owned_approval(approval_id, current_user, db)
    approval = await report_opened_service(db, approval, success=data.success)
    await db.commit()
    await db.refresh(approval)
    return ApprovalOut.model_validate(approval)


@router.post("/{approval_id}/dismiss", response_model=ApprovalOut)
async def dismiss_approval(
    approval_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApprovalOut:
    approval = await _get_owned_approval(approval_id, current_user, db)
    approval = await dismiss_service(db, approval)
    await db.commit()
    await db.refresh(approval)
    return ApprovalOut.model_validate(approval)
