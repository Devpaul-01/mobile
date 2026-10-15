# Milestone 3: Reminders — Frontend Integration Guide

Written for someone picking this up without prior context. If something
here doesn't match the code, trust the code and flag it.

**Machine-readable contract:** `docs/api/openapi.yaml`.

All endpoints below require the `Authorization: Bearer <access_token>`
header, same as contacts.

---

## A. What's ready — and what this milestone deliberately is not

A reminder is a **stored preference** right now: "remind me about this
contact, N days before their birthday, on these channels." There is no
scheduler yet, so **nothing fires**. No push notification, email, or
WhatsApp message is ever sent as a result of creating a reminder today —
that's a separate, later piece of work (a scheduler + delivery services),
not bundled into this milestone. Build the UI for creating/editing
reminders now; the "it actually notifies you" part comes later.

The one thing this milestone *does* deliver end-to-end is the explicit
ask behind it: applying one reminder definition to several contacts at
once, via multi-select — see `POST /bulk` below. There is **no persisted
"Group" concept** — selecting contacts is a one-time action scoped to that
single API call, not something saved for reuse. If you want a "my
groups" feature later, that's new scope, not something this endpoint
secretly already supports.

8 automated tests back this (`backend/app/tests/test_reminders.py`), and
the migration (`0003_reminders`) has been run against a real Postgres
database in both directions.

## B. Available endpoints

Base path: `/mobile/reminders`.

---

### `POST /` — create one reminder for one contact

```json
{ "contact_id": "3fae2c10-...", "lead_time_days": 3, "channels": ["push", "email"] }
```

- `lead_time_days` — optional, default `0` (means "on the day"). 0–365.
- `channels` — optional, default `["push"]`. Must be a non-empty list from
  `push` / `email` / `whatsapp`. Picking `email` or `whatsapp` here doesn't
  send anything yet (§A) — it's just recorded as the user's intent for
  when delivery is built.

**201 Created:** the created reminder —
```json
{
  "id": "...",
  "contact_id": "3fae2c10-...",
  "lead_time_days": 3,
  "channels": ["push", "email"],
  "enabled": true,
  "created_at": "...",
  "updated_at": "..."
}
```

**404** (`contact_not_found`) if `contact_id` doesn't exist, isn't yours,
or has been deleted. **422** for an empty/invalid `channels` list.

---

### `POST /bulk` — apply one reminder to several selected contacts at once

This is the feature this milestone exists for.

```json
{
  "contact_ids": ["3fae2c10-...", "9bc1..."],
  "lead_time_days": 2,
  "channels": ["push"]
}
```

- `contact_ids` — required, 1–500 per call. This is whatever the user
  multi-selected on a contacts list screen in that moment — nothing about
  that selection is saved as a reusable group.
- Same `lead_time_days`/`channels` defaults as the single-create endpoint,
  applied identically to every contact in the list.

**200 OK:**
```json
{
  "created": [ /* one reminder object per successfully-created contact, same shape as POST / */ ],
  "skipped_contact_ids": ["9bc1..."]
}
```

A `contact_id` that doesn't exist, isn't yours, or was deleted is **not**
an error for the whole call — it's silently skipped and listed in
`skipped_contact_ids`, so a multi-select of 50 contacts doesn't fail
entirely because one was deleted a moment earlier. Show the user a summary
("Applied to 49 of 50 — 1 contact no longer exists") rather than treating
a non-empty `skipped_contact_ids` as a failure state.

**401** if not authenticated. **422** if `contact_ids` is empty or over
500, or `channels` is invalid.

---

### `GET /` — list the user's reminders

**Query params:** `contact_id` (optional, filter to one contact's
reminders — useful for a contact-detail screen showing "reminders for
this person"), `limit` (default 50, max 200), `offset` (default 0).

**200 OK:**
```json
{ "items": [ /* reminder objects, newest first */ ], "total": 3, "limit": 50, "offset": 0 }
```

---

### `GET /{id}` — fetch one reminder

Same shape as a `POST /` response. **404** (`reminder_not_found`) if it
doesn't exist or isn't yours.

---

### `PATCH /{id}` — edit a reminder

All fields optional — send only what changed.
```json
{ "lead_time_days": 7, "enabled": false, "channels": ["push"] }
```
`enabled` lets the user pause a reminder without deleting it (e.g. toggle
off, keep the lead-time/channel config for later). **404** same as `GET`.

---

### `DELETE /{id}`

Hard-deletes the reminder (unlike contacts, reminders aren't soft-deleted
— there's no "reminder history" concept yet that a delete would need to
preserve). **204 No Content** on success, **404** if it's already gone or
isn't yours.

## C. Known limitations

- **Nothing fires.** Repeated from §A because it's the single most
  important thing not to build UI promises around yet.
- **No "undo bulk apply."** If a multi-select apply was a mistake, the
  user (or your UI) has to delete the created reminders individually —
  there's no bulk-delete endpoint yet either.
- **No group/saved-selection feature**, by explicit design choice (§A).

## D. Local dev

Same setup as Milestones 1–2. After pulling this branch:
```
alembic upgrade head
```
