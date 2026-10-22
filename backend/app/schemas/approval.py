"""
schemas/approval.py — Pydantic schemas for the pending-approval state
machine (MOBILE_APP_ARCHITECTURE_SPEC.md §17; whatsapp + email).

There's no "create" schema here — a ``PendingApproval`` is only ever
created by the scheduler (app/services/scheduler.py) when a firing
reminder requests the whatsapp channel, or when its email channel isn't
eligible for (or isn't configured for) auto-send — never by a direct API
call. The API only moves an existing row through its states, and (email
only) edits its message content before sending.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.core.validators import sanitize_text
from app.db.models import ApprovalChannel, ApprovalState


class ApprovalOut(BaseModel):
    id: UUID
    reminder_occurrence_id: UUID
    contact_id: UUID
    channel: ApprovalChannel
    state: ApprovalState
    message_subject: str | None = None
    message_body: str | None = None
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


class ApprovalMessageUpdate(BaseModel):
    """PATCH /mobile/approvals/{id} — edit the composed draft before
    sending. Email channel only (see services/approvals.py
    update_message); both fields optional/independent, a field left out
    of the payload is left unchanged."""

    message_subject: str | None = Field(default=None, max_length=255)
    message_body: str | None = Field(default=None, max_length=10_000)

    _sanitize_subject = field_validator("message_subject", mode="after")(sanitize_text)
    _sanitize_body = field_validator("message_body", mode="after")(sanitize_text)
