"""
api/routers/notifications.py — Push-token registration.

Mounted under ``/mobile/notifications`` (set in main.py).

POST /mobile/notifications/register-token
    Upsert on (user_id, device_token) per architecture doc §12.2/§35: a
    device re-registering the same token (app reinstall, token refresh
    with an unchanged value, etc.) just bumps ``last_used_at`` rather than
    creating a duplicate row or erroring.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import PushToken, User
from app.db.session import get_db
from app.schemas.common import ErrorResponse
from app.schemas.notification import PushTokenOut, PushTokenRegister

_ERROR_RESPONSES: dict[int | str, dict] = {
    401: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}

router = APIRouter(responses=_ERROR_RESPONSES)


@router.post(
    "/register-token", response_model=PushTokenOut, status_code=status.HTTP_200_OK
)
async def register_push_token(
    data: PushTokenRegister,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PushTokenOut:
    result = await db.execute(
        select(PushToken).where(
            PushToken.user_id == current_user.id,
            PushToken.device_token == data.device_token,
        )
    )
    existing = result.scalar_one_or_none()

    if existing is not None:
        existing.platform = data.platform
        existing.last_used_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(existing)
        return PushTokenOut.model_validate(existing)

    push_token = PushToken(
        user_id=current_user.id,
        device_token=data.device_token,
        platform=data.platform,
    )
    db.add(push_token)
    await db.commit()
    await db.refresh(push_token)
    return PushTokenOut.model_validate(push_token)
