"""
api/routers/account.py — GDPR export / account deletion (§27, Phase 13).

Mounted under ``/mobile/account`` (set in main.py).

Endpoints
---------
GET    /mobile/account/export   Full JSON export of everything the user
                                 owns (§27.4) — available any time, not
                                 only as a pre-deletion step.
DELETE /mobile/account          Re-authenticates with the user's current
                                 password (§27.2 — "requires
                                 re-authentication confirmation"), then
                                 runs the full deletion workflow.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.errors import AppError, ErrorCodes
from app.core.security import verify_password
from app.db.models import User
from app.db.session import get_db
from app.schemas.account import DeleteAccountRequest
from app.schemas.common import MessageResponse
from app.services.account import delete_account, export_user_data

router = APIRouter()


@router.get("/export")
async def export_account(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await export_user_data(db, current_user)


@router.delete("", response_model=MessageResponse)
async def delete_account_endpoint(
    data: DeleteAccountRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    if not verify_password(data.password, current_user.password_hash):
        raise AppError(
            status.HTTP_401_UNAUTHORIZED,
            ErrorCodes.WRONG_PASSWORD,
            "Incorrect password.",
        )

    await delete_account(db, current_user)
    await db.commit()
    return MessageResponse(message="Your account has been deleted.")
