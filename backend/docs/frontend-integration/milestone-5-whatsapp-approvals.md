# Milestone 5: WhatsApp Pending-Approval Queue — Frontend Integration Guide

Written for someone picking this up without prior context. If something
here doesn't match the code, trust the code and flag it.

**Machine-readable contract:** `docs/api/openapi.yaml`.

All endpoints below require the `Authorization: Bearer <access_token>`
header, same as contacts/reminders.

---

## A. Why this exists

WhatsApp can **never** be auto-sent — there's no API that lets a backend
send a WhatsApp message on a user's behalf. The best the backend can do is
prepare everything (the contact, the pre-filled message) and hand the user
a deep link they tap themselves. This milestone is that hand-off,
end-to-end on the backend: the scheduler (Milestone 4) now creates a
`PendingApproval` row whenever a firing reminder has `whatsapp` in its
channels, and this milestone adds the API the mobile app uses to show that
item in an "Approval Queue" screen and move it through its lifecycle.

**What "sent" means here, precisely — read this before writing any UI
copy:** `COMPLETED` means *"we successfully handed off to WhatsApp"* —
i.e. `Linking.openURL` resolved. It does **not** mean the message was
actually sent or delivered. The backend has no way to know whether the
user tapped Send inside WhatsApp once it opened. Don't write UI copy that
implies delivery confirmation ("Message sent!") — something like "Opened
in WhatsApp" is accurate; "Sent" is not.

## B. The state machine

```
PENDING_APPROVAL --(user taps "Send via WhatsApp")--> APPROVED
APPROVED --(app successfully opened the deep link)--> COMPLETED
APPROVED --(WhatsApp not installed / open failed)----> LINK_FAILED
PENDING_APPROVAL --(not actioned within 7 days)-------> EXPIRED
PENDING_APPROVAL --(user dismisses without sending)---> DISMISSED
```

A few things worth knowing about how this is enforced server-side:
- Calling `/approve` records the transition **before** your app opens the
  deep link — so even if the user backs out of WhatsApp immediately after,
  the approval is still correctly `APPROVED`, not stuck `PENDING_APPROVAL`.
- `/report-opened` is safe to call twice with the same result if your
  first call's response was lost to a flaky connection — a repeat call
  after the approval already reached `COMPLETED`/`LINK_FAILED` just
  returns the current state again rather than erroring.
- Any other out-of-order call (e.g. approving something already approved,
  or dismissing something already approved) is a real `409` — the
  frontend shouldn't normally be able to trigger this if it only exposes
  actions that make sense for an item's current state, but it's not
  silently ignored either.

## C. Endpoints

Base path: `/mobile/approvals`.

---

### `GET /` — list the user's approval queue

**Query params:** `state` (optional — one of `pending_approval` /
`approved` / `completed` / `link_failed` / `expired` / `dismissed`; a
queue screen almost certainly wants `?state=pending_approval`), `limit`
(default 50, max 200), `offset` (default 0).

**200 OK:**
```json
{
  "items": [
    {
      "id": "...",
      "reminder_occurrence_id": "...",
      "contact_id": "...",
      "channel": "whatsapp",
      "state": "pending_approval",
      "created_at": "...",
      "updated_at": "...",
      "expires_at": "..."
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

This response does **not** include the contact's name or pre-filled
message — join against `GET /mobile/contacts/{contact_id}` using
`contact_id` to build the actual approval card (name, photo, etc.), and
construct the WhatsApp deep link client-side per the architecture doc
§20.2/§20.3 (`wa.me/<E.164 without +>?text=<encoded message>`) — that
construction is a mobile-side concern, not something this API returns
pre-built.

---

### `POST /{id}/approve` — user taps "Send via WhatsApp"

Call this **before** calling `Linking.openURL` on the WhatsApp deep link,
not after — see §B above for why.

**200 OK:** the approval, now `state: "approved"`.
**404** (`approval_not_found`) if it doesn't exist or isn't yours.
**409** (`invalid_approval_transition`) if it's not currently
`pending_approval`.

---

### `POST /{id}/report-opened` — report whether the deep link opened

Call this right after your `Linking.canOpenURL`/`Linking.openURL`
attempt.

```json
{ "success": true }
```

- `success: true` → `state` becomes `completed`.
- `success: false` → `state` becomes `link_failed` (show a "WhatsApp isn't
  installed" fallback per architecture doc §17.2 — e.g. copy the message
  to the clipboard).

**200 OK:** the approval, in its new state. **404/409** as above (409
only for a genuinely invalid call — e.g. calling this on an approval still
`pending_approval` that was never approved first).

---

### `POST /{id}/dismiss` — user dismisses without sending

No body. **200 OK:** the approval, now `state: "dismissed"`. **404/409**
as above.

## D. Known limitations

- **No "undo."** Once dismissed or expired, there's no way to bring an
  approval back — the user would need to wait for next year's occurrence
  (or create a fresh reminder) for a new one to fire.
- **Expiry is a background sweep, not instant.** An approval becomes
  `EXPIRED` the next time the scheduler runs after its 7-day window
  passes (the scheduler runs every 15 minutes, so this is never more than
  15 minutes late in practice, not an exact-to-the-second cutoff).
- **No push notification content is defined here.** The scheduler already
  sends a push notification pointing at the approval queue when a
  `PendingApproval` is created (reusing the same push path as any other
  reminder) — this milestone didn't change that push payload's content,
  only what exists for the user to navigate to.

## E. Local dev

Same setup as Milestones 1-4. After pulling this branch:
```
alembic upgrade head
```
