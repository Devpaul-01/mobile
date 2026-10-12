"""
schemas/notification.py — Pydantic schemas for push-token registration.

Scope note: this is just the device-token upsert (architecture doc
§12.2/§35) so the scheduler (app/services/scheduler.py) has somewhere real
to read tokens from for the "push" channel. It does not send anything
itself — see app/services/push.py for the (stubbed) send path.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints
from typing import Annotated

from app.db.models import PushPlatform


class PushTokenRegister(BaseModel):
    device_token: Annotated[str, StringConstraints(min_length=1, max_length=255)] = Field(
        ..., description="Opaque push token issued by APNs/FCM (or Expo) to this device."
    )
    platform: PushPlatform


class PushTokenOut(BaseModel):
    id: UUID
    device_token: str
    platform: PushPlatform
    created_at: datetime
    last_used_at: datetime

    model_config = {"from_attributes": True}
