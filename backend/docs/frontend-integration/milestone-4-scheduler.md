# Milestone 4: Reminder Scheduler + Push Registration — Frontend Integration Guide

Written for someone picking this up without prior context. If something
here doesn't match the code, trust the code and flag it.

**Machine-readable contract:** `docs/api/openapi.yaml`.

---

## A. What's new — reminders actually fire now

As of Milestone 3, creating a `Reminder` only stored a preference — "remind
me about this contact, N days before their birthday, on these channels" —
and **nothing was ever sent**. That changes here: a background job
(running inside the API process, not a separate worker) checks every 15
minutes for reminders whose trigger moment has arrived, and for each one
that fires:

- **`push`** — sent (today: logged, see §C) to every device token the
  user has registered via the new endpoint below.
- **`email`** — sent (today: logged, see §C) to the user's account email,
  with up to 3 retry attempts on later scheduler passes if it fails.
- **`whatsapp`** — as of this milestone, creates a `PendingApproval` row
  rather than sending anything. The approval queue + approve/dismiss
  flow is built in **Milestone 5** — see
  `docs/frontend-integration/milestone-5-whatsapp-approvals.md`, which
  supersedes the note that used to be here.

There is still no UI-facing "notification feed" or "did my reminder fire"
endpoint — the mobile client finds out a reminder fired the same way it
would with any push notification or email: by receiving it (once real
delivery providers are wired up — see §C).

## B. New endpoint: register a device for push

### `POST /mobile/notifications/register-token`

Call this once per app foreground/launch, or whenever the OS hands the
app a new or refreshed push token — it's always safe to call again with
the same token (upsert, not create-or-fail).

```json
{ "device_token": "<opaque token from APNs/FCM/Expo>", "platform": "ios" }
```

- `platform` — `"ios"` or `"android"`.

**200 OK:**
```json
{
  "id": "...",
  "device_token": "...",
  "platform": "ios",
  "created_at": "...",
  "last_used_at": "..."
}
```

Registering the same `(user, device_token)` pair again just bumps
`last_used_at` on the existing row — it does not create a duplicate, and
the response's `id` stays the same. **401** if not authenticated. **422**
for an unknown `platform` value.

There is no "unregister" endpoint yet — if that turns out to matter
(e.g. explicit logout-should-stop-push), ask for it as its own small
addition rather than assuming it's covered.

## C. Known limitations — read this before building delivery-dependent UI

- **Sending is stubbed for both working channels.** `push` and `email`
  both run through real scheduling, real retry/attempt bookkeeping, and
  real "did this fire" records — but the actual send is a log line, not a
  call to APNs/FCM or a real email provider (SendGrid, etc.). No device
  will actually receive anything yet. Swapping in a real provider is a
  small, isolated change (`app/services/push.py`, `app/services/email.py`)
  that doesn't require touching the scheduler itself.
- **`whatsapp` channel: now builds a `PendingApproval`, as of Milestone
  5.** See `docs/frontend-integration/milestone-5-whatsapp-approvals.md`
  for the approval-queue endpoints and UI flow.
- **Push failures are never retried** (by design — push is treated as
  best-effort/fire-and-forget). Email failures *are* retried, up to 3
  total attempts, on later 15-minute scheduler passes.
- **Fixed send time.** Every reminder fires at 9:00 AM in the user's own
  timezone (`User.timezone`, defaulting to UTC if the user never set one)
  — there's no per-reminder time-of-day setting yet.
- **No "test fire" or "preview" endpoint.** There's currently no way for
  the frontend to ask "what would fire for this reminder and when" without
  waiting for the real trigger moment.

## D. Local dev

Same setup as Milestones 1–3. After pulling this branch:
```
alembic upgrade head
```

The scheduler runs automatically on app startup unless
`SCHEDULER_ENABLED=false` is set in the environment (the test suite always
sets this, so pytest never starts a real background job).
