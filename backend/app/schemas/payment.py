"""
schemas/payment.py — Pydantic schemas for PayPal payments
(MOBILE_APP_ARCHITECTURE_SPEC.md §23, Phase 12).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field

from app.db.models import PaymentProvider, PaymentStatus, SubscriptionStatus
from app.services.paypal import PLAN_CATALOG


class CreateOrderRequest(BaseModel):
    plan: str = Field(description=f"One of: {', '.join(sorted(PLAN_CATALOG))}")


class CreateOrderResponse(BaseModel):
    paypal_order_id: str
    approval_url: str


class CaptureOrderRequest(BaseModel):
    paypal_order_id: str


class SubscriptionStatusOut(BaseModel):
    plan: str
    status: SubscriptionStatus
    payment_provider: PaymentProvider
    current_period_end: datetime | None
    cancel_at_period_end: bool

    model_config = {"from_attributes": True}


class NoSubscriptionOut(BaseModel):
    """Returned by ``GET /status`` when the user has never subscribed —
    distinct from an expired/cancelled ``Subscription`` row, which still
    carries real history worth returning."""

    status: str = "none"


class PaymentOut(BaseModel):
    id: UUID
    provider_order_id: str
    amount: Decimal
    currency: str
    status: PaymentStatus
    captured_at: datetime | None

    model_config = {"from_attributes": True}


class CaptureOrderResponse(BaseModel):
    status: SubscriptionStatus
    plan: str
