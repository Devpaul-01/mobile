"""
schemas/approval.py — Pydantic schemas for the WhatsApp pending-approval
state machine (MOBILE_APP_ARCHITECTURE_SPEC.md §17).

There's no "create" schema here — a ``PendingApproval`` is only ever
created by the scheduler (app/services/scheduler.py) when a firing
reminder requests the whatsapp channel, never by a direct API call. The
API only moves an existing row through its states.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.db.models import ApprovalChannel, ApprovalState


class ApprovalOut(BaseModel):
    id: UUID
    reminder_occurrence_id: UUID
    contact_id: UUID
    channel: ApprovalChannel
    state: ApprovalState
    created_at: datetime
    updated_at: datetime
    expires_at: datetime

    model_config = {"from_attributes": True}


class ApprovalListResponse(BaseModel):
    items: list[ApprovalOut]
    total: int
    limit: int
    offset: int


class ReportOpenedRequest(BaseModel):
    success: bool
