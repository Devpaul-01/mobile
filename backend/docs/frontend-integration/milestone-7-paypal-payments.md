# Milestone 7: PayPal Payments — Frontend Integration Guide

Written for someone picking this up without prior context. If something
here doesn't match the code, trust the code and flag it.

**Machine-readable contract:** `docs/api/openapi.yaml`.

All endpoints below except the webhook require the
`Authorization: Bearer <access_token>` header, same as contacts/reminders.

---

## A. What this is, and one thing that differs from how "subscription"
might sound

A user can subscribe to a paid plan via PayPal. The backend never
receives, stores, or handles PayPal credentials from the client — the
mobile app only ever sees a hosted `approval_url` to open and an
`order_id` to hand back.

**This is PayPal's one-time Orders API (create + capture), not PayPal's
separate recurring-billing Subscriptions API.** The architecture doc's
own diagram and endpoint inventory describe exactly a create-order/
capture/webhook flow, not a create-subscription endpoint, so that's what
this implements. Practically, this means:
- There is no automatic recurring charge. A successful capture grants the
  user a fixed-duration entitlement (`current_period_end`).
- "Renewing" is the user (or your app, prompting them) going through
  `create-order` → approve → `capture` again before that date lapses —
  PayPal does not charge them again on its own.
- The webhook endpoint exists and is wired up (for `PAYMENT.CAPTURE.*`
  lifecycle events), ready for a future move to true recurring billing
  without a schema change, but there's no recurring agreement actually
  running yet.

**Pricing is a placeholder.** The current plan catalog
(`app/services/paypal.py PLAN_CATALOG`) is `monthly` ($4.99/30 days) and
`yearly` ($39.99/365 days) — explicitly flagged as starting values for
MVP, not a finalized business decision. Confirm real prices before launch.

## B. Flow, end to end

```
1. User taps "Subscribe" and picks a plan ("monthly" | "yearly")
2. POST /mobile/payments/paypal/create-order { plan }
     → { paypal_order_id, approval_url }
3. Client opens approval_url in expo-web-browser (in-app browser, not
   external Safari/Chrome) — user approves the purchase in PayPal's
   hosted UI
4. PayPal redirects back to the app (Universal Link, same mechanism as
   verify-email/reset-password deep links)
5. POST /mobile/payments/paypal/capture { paypal_order_id }
     → backend calls PayPal's Capture API server-to-server — this is the
       ONLY step that actually confirms money changed hands. The app
       saying "the user approved" is never trusted on its own.
     → { status: "active", plan }
6. Client refetches GET /mobile/payments/status → UI updates
```

## C. Endpoints

Base path: `/mobile/payments`.

---

### `GET /status`

**200 OK**, one of two shapes:
```json
{ "status": "none" }
```
— the user has never subscribed. Or:
```json
{
  "plan": "monthly",
  "status": "pending" | "active" | "cancelled" | "expired",
  "payment_provider": "paypal",
  "current_period_end": "2026-11-13T00:00:00Z",
  "cancel_at_period_end": false
}
```
`"pending"` means an order was created but never captured (abandoned
checkout, or still mid-approval) — don't treat this as an active
subscription anywhere in the UI.

---

### `POST /paypal/create-order`

```json
{ "plan": "monthly" }
```
**200 OK:** `{ "paypal_order_id": "...", "approval_url": "https://..." }`
**400** (`invalid_plan`) for an unknown plan string.
**400** (`already_subscribed`) if the user already has an **active**
subscription — re-subscribing while `pending`/`cancelled`/`expired` is
allowed (covers retrying an abandoned checkout, or resubscribing after
expiry).

---

### `POST /paypal/capture`

```json
{ "paypal_order_id": "..." }
```
**200 OK:** `{ "status": "active", "plan": "monthly" }`
**400** (`order_not_found`) if that order id doesn't exist.
**400** (`order_mismatch`) if that order belongs to a different user —
note this is a **400 here, not a 404** like everywhere else ownership is
checked in this API; that's the architecture doc's own documented
response shape for this specific endpoint, not an inconsistency.
**502** (`paypal_error`) if PayPal couldn't capture the order (e.g. it was
never actually approved by the user) — show a generic "payment couldn't
be completed" message and let the user retry from `create-order`.

Calling this twice with the same `paypal_order_id` is safe — the second
call returns the same already-active result rather than attempting (and
failing) to capture an already-captured order.

---

### `POST /paypal/webhook`

Not called by the mobile app — configure this URL as the webhook endpoint
in the PayPal developer dashboard. No `Authorization` header; the backend
verifies PayPal's own transmission signature instead. Included here only
so whoever manages the PayPal app configuration knows it exists.

## D. Known limitations

- **No true recurring billing.** See section A — this is intentionally an
  Orders-API one-time-capture-per-period model, not PayPal Subscriptions.
  If recurring billing becomes a real requirement, the webhook plumbing
  is already in place but the create/capture flow itself would need to
  move to PayPal's Subscriptions API endpoints.
- **No App Store / Play Store IAP compliance determination.** The
  architecture doc (§24) explicitly flags this as unresolved — whether
  PayPal alone is acceptable for a digital-service subscription under
  Apple/Google's payment policies, or native IAP is required, needs a
  policy/legal review before this ships to either store. The schema
  (`Subscription.payment_provider`) is hedged for `apple_iap`/
  `google_iap` values but neither is implemented.
- **Pricing is a placeholder** (section A) — confirm real values before
  launch.
- **No per-user rate limiting yet** on `create-order` (the architecture
  doc's §29 inventory specifies 10/hr per user) — this needs the Redis-
  backed per-user rate limiter from Phase 10, which is a separate,
  not-yet-merged branch in this project's history. Flagged, not silently
  skipped.

## E. Local dev

Same setup as prior milestones, plus these new required env vars for the
payment endpoints to work (everything else in the app still boots fine
without them — only the payment endpoints themselves fail loudly if
unset):
```
PAYPAL_CLIENT_ID=<sandbox or live client id>
PAYPAL_CLIENT_SECRET=<sandbox or live client secret>
PAYPAL_WEBHOOK_ID=<webhook id from the PayPal developer dashboard>
PAYPAL_API_BASE=https://api-m.sandbox.paypal.com   # default; use
                                                     # https://api-m.paypal.com for production
```
After pulling this branch:
```
alembic upgrade head
```
