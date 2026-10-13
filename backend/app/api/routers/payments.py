"""
api/routers/payments.py — PayPal payments (§23/§24, Phase 12).

Mounted under ``/mobile/payments`` (set in main.py).

Endpoints
---------
GET  /mobile/payments/status                 Current subscription state.
POST /mobile/payments/paypal/create-order     §23 step 1: creates a
                                               PayPal order, returns the
                                               hosted approval URL.
POST /mobile/payments/paypal/capture          §23 step 4: server-to-
                                               server capture — the only
                                               step that actually
                                               confirms money changed
                                               hands.
POST /mobile/payments/paypal/webhook          Unauthenticated,
                                               signature-verified —
                                               PayPal's own lifecycle
                                               events (§23's "webhooks
                                               handle ongoing lifecycle
                                               state").
"""

from __future__ import annotations

import json as json_module

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.errors import AppError, ErrorCodes
from app.db.models import User
from app.db.session import get_db
from app.schemas.common import ErrorResponse, MessageResponse
from app.schemas.payment import (
    CaptureOrderRequest,
    CaptureOrderResponse,
    CreateOrderRequest,
    CreateOrderResponse,
    NoSubscriptionOut,
    SubscriptionStatusOut,
)
from app.services import payments as payments_service
from app.services import paypal

_ERROR_RESPONSES: dict[int | str, dict] = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
}

router = APIRouter(responses=_ERROR_RESPONSES)


@router.get("/status", response_model=SubscriptionStatusOut | NoSubscriptionOut)
async def get_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SubscriptionStatusOut | NoSubscriptionOut:
    subscription = await payments_service.get_subscription(db, current_user.id)
    if subscription is None:
        return NoSubscriptionOut()
    return SubscriptionStatusOut.model_validate(subscription)


@router.post("/paypal/create-order", response_model=CreateOrderResponse)
async def create_order(
    data: CreateOrderRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CreateOrderResponse:
    _subscription, _payment, order = await payments_service.start_order(
        db, user_id=current_user.id, plan=data.plan
    )
    await db.commit()
    return CreateOrderResponse(paypal_order_id=order.order_id, approval_url=order.approval_url)


@router.post("/paypal/capture", response_model=CaptureOrderResponse)
async def capture_order(
    data: CaptureOrderRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CaptureOrderResponse:
    subscription = await payments_service.capture_and_apply(
        db, provider_order_id=data.paypal_order_id, user_id=current_user.id
    )
    await db.commit()
    return CaptureOrderResponse(status=subscription.status, plan=subscription.plan)


@router.post("/paypal/webhook", response_model=MessageResponse, include_in_schema=True)
async def paypal_webhook(request: Request, db: AsyncSession = Depends(get_db)) -> MessageResponse:
    raw_body = await request.body()

    verified = await paypal.verify_webhook_signature(headers=dict(request.headers), body=raw_body)
    if not verified:
        raise AppError(
            status.HTTP_400_BAD_REQUEST,
            ErrorCodes.INVALID_WEBHOOK_SIGNATURE,
            "Could not verify PayPal webhook signature.",
        )

    event = json_module.loads(raw_body)
    await payments_service.handle_webhook_event(db, event)
    await db.commit()
    return MessageResponse(message="received")
