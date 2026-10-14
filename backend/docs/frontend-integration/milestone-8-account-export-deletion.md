# Milestone 8: GDPR Export & Account Deletion — Frontend Integration Guide

Written for someone picking this up without prior context. If something
here doesn't match the code, trust the code and flag it.

**Machine-readable contract:** `docs/api/openapi.yaml`.

Both endpoints below require the `Authorization: Bearer <access_token>`
header, same as every other mobile endpoint.

---

## A. What this is

Settings → Privacy gets two actions: export everything the account owns
as a JSON file, and permanently delete the account. Deletion requires
re-entering the current password as a confirmation step (§27.2) — not
present in the existing extension backend this project's patterns come
from, but a reasonable strengthening given deletion is irreversible.

**One thing worth knowing if you're also the one who built the payments
feature (Milestone 7):** this product's PayPal integration is a one-time-
order model, not true recurring billing (see
`milestone-7-paypal-payments.md`), so "cancel the subscription before
deleting" is a genuine no-op here — there's no external PayPal agreement
to cancel, and whatever the subscription row said gets deleted right
along with everything else anyway. Nothing to build or call for that on
the client side.

## B. Flow, end to end

### Export
```
GET /mobile/account/export
  → a single JSON document with everything the account owns
  → app downloads/shares it as a file (e.g. via expo-sharing) — there's
    no pagination or streaming concern at this product's data volumes
```

### Deletion
```
1. User taps "Delete Account" in Settings → Privacy
2. App shows a confirmation screen asking for their current password
3. DELETE /mobile/account { password }
     → 401 wrong_password if it doesn't match — show inline, let them retry
     → 200 on success
4. App clears SecureStore (tokens are already meaningless — the account
   is gone), navigates to a "your account has been deleted" confirmation
   screen, then to Login
```

## C. Endpoints

Base path: `/mobile/account`.

---

### `GET /export`

**200 OK**, shape:
```json
{
  "exported_at": "2026-10-14T00:00:00Z",
  "profile": { "id": "...", "email": "...", "name": "...", "timezone": "...", "created_at": "..." },
  "contacts": [
    {
      "id": "...", "name": "...", "birthday_month": 6, "birthday_day": 12, "birthday_year": null,
      "source": "manual", "notes": "...", "deleted_at": null, "created_at": "...",
      "identifiers": [ { "type": "phone", "normalized_value": "+1...", "raw_value": "...", "normalization_failed": false, "created_at": "..." } ],
      "photo": { "content_type": "image/jpeg", "size_bytes": 102400, "uploaded_at": "..." } | null
    }
  ],
  "reminders": [
    {
      "id": "...", "contact_id": "...", "lead_time_days": 3, "channels": ["push", "email"], "enabled": true, "created_at": "...",
      "occurrences": [ { "occurrence_year": 2026, "fired_at": "...", "email_status": "sent", "email_attempts": 1, "email_sent_at": "..." } ]
    }
  ],
  "approvals": [
    {
      "id": "...", "contact_id": "...", "channel": "whatsapp", "state": "completed", "created_at": "...", "expires_at": "...",
      "history": [ { "from_state": null, "to_state": "pending_approval", "actor": "scheduler", "at": "..." }, "..." ]
    }
  ],
  "subscription": {
    "plan": "monthly", "status": "active", "payment_provider": "paypal",
    "current_period_end": "...", "cancel_at_period_end": false,
    "payments": [ { "amount": "4.99", "currency": "USD", "status": "completed", "captured_at": "...", "created_at": "..." } ]
  } | null
}
```
`subscription` is `null` if the user never subscribed. Payment entries
deliberately omit PayPal's own order/subscription ids (§27.4 — "not raw
PayPal data") since those identify PayPal-side records, not the user.

---

### `DELETE /account`

```json
{ "password": "the user's current password" }
```
**200 OK:** `{ "message": "Your account has been deleted." }`
**401** (`wrong_password`) — show inline, don't lock the user out of
retrying (this isn't the login-attempt lockout, no rate limit here yet —
see "Known limitations").

Everything the account owns is gone after this: contacts, reminders,
occurrence history, approvals, push tokens, subscription/payment rows,
and any uploaded contact photo (both the database row and the actual
object in Cloudflare R2). There is no undo.

## D. Known limitations

- **No automated Sentry data deletion.** Error events already sent to
  Sentry before deletion aren't retroactively removed by this flow
  (architecture doc §27.3) — a separate manual process if ever required.
- **Backups retain deleted data until they themselves age out** — this
  flow doesn't (and can't) reach into `pg_dump`-based backups. Standard
  and should be disclosed in the privacy policy, not silently assumed
  away.
- **No rate limiting on the deletion endpoint** — a wrong-password retry
  isn't throttled the way login attempts are. Low risk (it requires a
  valid access token already), but flagged rather than silently assumed
  covered.
- **PayPal's own transaction records** are retained per PayPal's own
  policies, outside this system's control.

## E. Local dev

No new environment variables — this milestone only adds two endpoints on
top of infrastructure every prior milestone already requires. No new
migration either (no new tables).
