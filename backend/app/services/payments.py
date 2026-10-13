"""
services/payments.py — PayPal order lifecycle, on top of the raw PayPal
API calls in app/services/paypal.py (MOBILE_APP_ARCHITECTURE_SPEC.md §23).

None of these functions commit — callers (the router) control the
transaction, matching the convention in app/services/approvals.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCodes
from app.db.models import Payment, PaymentProvider, PaymentStatus, Subscription, SubscriptionStatus
from app.services import paypal


async def get_subscription(db: AsyncSession, user_id: UUID) -> Subscription | None:
    result = await db.execute(select(Subscription).where(Subscription.user_id == user_id))
    return result.scalar_one_or_none()


async def start_order(
    db: AsyncSession, *, user_id: UUID, plan: str
) -> tuple[Subscription, Payment, paypal.CreatedOrder]:
    """§23 step 1: get-or-create the user's ``Subscription`` row, create
    the PayPal order, and record a ``Payment`` row for it. Raises 400
    ``already_subscribed`` if the user already has an active
    subscription (§35's documented error for this endpoint) — re-
    subscribing while pending/cancelled/expired is allowed (the user may
    be retrying a failed checkout, or resubscribing after expiry)."""
    plan_info = paypal.validate_plan(plan)  # 400 invalid_plan if unknown

    subscription = await get_subscription(db, user_id)
    if subscription is not None and subscription.status == SubscriptionStatus.ACTIVE:
        raise AppError(
            status.HTTP_400_BAD_REQUEST,
            ErrorCodes.ALREADY_SUBSCRIBED,
            "You already have an active subscription.",
        )

    if subscription is None:
        subscription = Subscription(
            user_id=user_id,
            payment_provider=PaymentProvider.PAYPAL,
            plan=plan,
            status=SubscriptionStatus.PENDING,
        )
        db.add(subscription)
    else:
        subscription.plan = plan
        subscription.status = SubscriptionStatus.PENDING
        subscription.updated_at = datetime.now(timezone.utc)
    await db.flush()

    order = await paypal.create_order(plan=plan)

    payment = Payment(
        subscription_id=subscription.id,
        provider_order_id=order.order_id,
        amount=plan_info["amount"],
        currency=plan_info["currency"],
        status=PaymentStatus.PENDING,
    )
    db.add(payment)
    await db.flush()

    return subscription, payment, order


async def _get_owned_payment(
    db: AsyncSession, *, provider_order_id: str, user_id: UUID
) -> tuple[Payment, Subscription]:
    payment_result = await db.execute(
        select(Payment).where(Payment.provider_order_id == provider_order_id)
    )
    payment = payment_result.scalar_one_or_none()
    if payment is None:
        raise AppError(
            status.HTTP_400_BAD_REQUEST,
            ErrorCodes.ORDER_NOT_FOUND,
            "No order found for that id.",
        )

    subscription_result = await db.execute(
        select(Subscription).where(Subscription.id == payment.subscription_id)
    )
    subscription = subscription_result.scalar_one()

    if subscription.user_id != user_id:
        # 400, not 404, to match §35's documented response for this
        # endpoint ("400 order mismatch/not found") — this is the one
        # case in this codebase where an ownership mismatch isn't a 404,
        # because the architecture doc specifies it explicitly this way.
        raise AppError(
            status.HTTP_400_BAD_REQUEST,
            ErrorCodes.ORDER_MISMATCH,
            "This order does not belong to you.",
        )

    return payment, subscription


async def capture_and_apply(
    db: AsyncSession, *, provider_order_id: str, user_id: UUID
) -> Subscription:
    """§23 step 4: server-to-server capture, then apply the result.
    Idempotent on ``provider_order_id`` (§12.2) — a payment already
    ``COMPLETED`` is returned as-is rather than re-captured."""
    payment, subscription = await _get_owned_payment(
        db, provider_order_id=provider_order_id, user_id=user_id
    )

    if payment.status == PaymentStatus.COMPLETED:
        return subscription

    try:
        captured = await paypal.capture_order(provider_order_id)
    except AppError:
        payment.status = PaymentStatus.FAILED
        await db.flush()
        raise

    _apply_capture(payment=payment, subscription=subscription, captured=captured)
    await db.flush()
    return subscription


def _apply_capture(
    *, payment: Payment, subscription: Subscription, captured: paypal.CapturedOrder
) -> None:
    now = datetime.now(timezone.utc)
    payment.status = PaymentStatus.COMPLETED
    payment.amount = captured.amount
    payment.currency = captured.currency
    payment.captured_at = now

    subscription.status = SubscriptionStatus.ACTIVE
    subscription.provider_subscription_id = payment.provider_order_id
    subscription.current_period_end = paypal.compute_period_end(subscription.plan)
    subscription.cancel_at_period_end = False
    subscription.updated_at = now


async def apply_webhook_capture_completed(db: AsyncSession, *, provider_order_id: str) -> bool:
    """Webhook-driven counterpart to ``capture_and_apply`` (§23: "webhooks
    handle ongoing lifecycle state"). Unlike the client-driven path, this
    never calls PayPal's capture endpoint itself — PayPal has already told
    us the capture completed; this just makes our own records match.
    Returns ``False`` (handled gracefully, not an error) if no ``Payment``
    row matches the order id, since an unrecognized/foreign order id in a
    webhook event is routine — multiple PayPal integrations can share one
    webhook endpoint subscription in sandbox setups."""
    result = await db.execute(
        select(Payment).where(Payment.provider_order_id == provider_order_id)
    )
    payment = result.scalar_one_or_none()
    if payment is None:
        return False
    if payment.status == PaymentStatus.COMPLETED:
        return True  # already applied via the client-driven capture path

    subscription_result = await db.execute(
        select(Subscription).where(Subscription.id == payment.subscription_id)
    )
    subscription = subscription_result.scalar_one()

    payment.status = PaymentStatus.COMPLETED
    payment.captured_at = datetime.now(timezone.utc)

    subscription.status = SubscriptionStatus.ACTIVE
    subscription.provider_subscription_id = provider_order_id
    subscription.current_period_end = paypal.compute_period_end(subscription.plan)
    subscription.cancel_at_period_end = False
    subscription.updated_at = datetime.now(timezone.utc)

    await db.flush()
    return True


_CAPTURE_COMPLETED_EVENTS = {"PAYMENT.CAPTURE.COMPLETED"}
_CAPTURE_FAILED_EVENTS = {"PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.REFUNDED"}


def _extract_order_id(event: dict) -> str | None:
    """PayPal's capture-event payloads nest the originating order id under
    ``resource.supplementary_data.related_ids.order_id`` — ``resource.id``
    itself is the *capture* id, not the order id our ``Payment`` rows are
    keyed on."""
    resource = event.get("resource", {})
    return (
        resource.get("supplementary_data", {})
        .get("related_ids", {})
        .get("order_id")
    )


async def handle_webhook_event(db: AsyncSession, event: dict) -> None:
    """Dispatches a verified PayPal webhook event (§23: "webhooks handle
    ongoing lifecycle state"). Unrecognized event types and events with no
    extractable/matching order id are no-ops, not errors — PayPal retries
    an endpoint that doesn't return 200, so an unhandled-but-valid event
    must still ack cleanly rather than fail the request."""
    event_type = event.get("event_type", "")
    order_id = _extract_order_id(event)
    if not order_id:
        return

    if event_type in _CAPTURE_COMPLETED_EVENTS:
        await apply_webhook_capture_completed(db, provider_order_id=order_id)
    elif event_type in _CAPTURE_FAILED_EVENTS:
        await apply_webhook_capture_denied_or_refunded(db, provider_order_id=order_id)


async def apply_webhook_capture_denied_or_refunded(
    db: AsyncSession, *, provider_order_id: str
) -> bool:
    """A payment failure/refund lifecycle event (§23) revokes the
    entitlement — the subscription is cancelled, not silently left
    ``ACTIVE``. Returns ``False`` if no matching ``Payment`` row exists,
    same reasoning as ``apply_webhook_capture_completed``."""
    result = await db.execute(
        select(Payment).where(Payment.provider_order_id == provider_order_id)
    )
    payment = result.scalar_one_or_none()
    if payment is None:
        return False

    subscription_result = await db.execute(
        select(Subscription).where(Subscription.id == payment.subscription_id)
    )
    subscription = subscription_result.scalar_one()

    payment.status = PaymentStatus.FAILED
    subscription.status = SubscriptionStatus.CANCELLED
    subscription.updated_at = datetime.now(timezone.utc)

    await db.flush()
    return True
