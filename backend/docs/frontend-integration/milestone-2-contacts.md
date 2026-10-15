# Milestone 2: Contacts Import & Dedup — Frontend Integration Guide

Written for someone picking this up without prior context. If something
here doesn't match the code, trust the code and flag it.

**Machine-readable contract:** `docs/api/openapi.yaml` (regenerated from the
running app — see Milestone 1's doc for tooling notes).

All endpoints below require the `Authorization: Bearer <access_token>`
header from Milestone 1's login flow, and the account must have a verified
email (same rule as everything else).

---

## A. What's ready

Importing a device's contact list, deduping it against anything already
saved, and basic CRUD on the result: list, fetch one, edit, delete.

Nothing about reminders, scheduling, or push notifications yet — contacts
are just stored and matched in this milestone.

31 automated tests back this + Milestone 1
(`backend/app/tests/test_contacts.py`, `test_auth.py`), and the new tables'
migration (`0002_contacts_and_identifiers`) has been run against a real
Postgres database in both directions, not just SQLite.

## B. The dedup rule, in plain terms

This is the part worth understanding before you build the import screen,
because it decides what the response looks like.

When you send up a batch of device contacts, each one is matched against
contacts already saved for this user **by phone number or email only —
never by name.** Two different people can share a name; the same person
can be saved under different name spellings. So:

- **No match** → a new contact is created.
- **Exactly one match** → the existing contact is updated (any new phone/
  email you sent is added to it; the birthday is updated only if doing so
  doesn't lose information — see below).
- **More than one match** (e.g. the phone you sent matches contact A, but
  the email you sent matches a different contact B) → nothing is created
  or changed for that row. It comes back in `requires_review` instead, so
  you can show the user a "is this the same person?" prompt. **This case
  needs its own UI eventually (a merge-picker) — there's no backend
  endpoint for actually merging two contacts yet.** For now, treat
  `requires_review` entries as "skipped, needs a human."

**Birthday rule:** a contact's birthday is never *downgraded*. If a contact
already has a full birthday (month + day + year) and a later import only
has month + day (common — device contacts frequently have no year), the
year is kept. If the user has manually edited a contact (any `PATCH` that
touches the birthday — see §E), that contact's birthday is never
overwritten by a later import at all, even with a full date.

**Normalize phone numbers before sending them.** The server does not
re-validate phone number *content*, only that each is a non-empty string
under 32 chars. If your device-contacts library can't produce a clean
E.164 number for some entry (no country code, garbage characters, etc.),
send it anyway with `"normalization_failed": true` — it's still stored
(so the user can fix it later, and so it exists for anything that needs
the raw value), but it's excluded from matching, so a malformed number can
never cause two different people to get merged.

## C. Available endpoints

Base path for everything below: `/mobile/contacts`.

---

### `POST /import`

Send a batch of device contacts. Can be called repeatedly (e.g. every time
the app opens) — already-imported contacts are matched and updated, not
duplicated.

**Body:**
```json
{
  "contacts": [
    {
      "name": "Jane Doe",
      "phones": [
        { "value": "+15551234567", "normalization_failed": false }
      ],
      "emails": ["jane@example.com"],
      "birthday": { "month": 6, "day": 15, "year": 1990 },
      "device_contact_id": "device-local-id-abc123"
    }
  ]
}
```

Notes on fields:
- `phones`, `emails` — both optional lists, default to empty. A contact
  with neither can still be imported (it just won't ever be matched by
  anything — it'll always come back as "no match" → created new, on every
  import, since there's nothing to match on).
- `birthday` — optional. `year` is optional *within* it (device contacts
  are commonly year-less — don't invent a year to fill this in).
- `device_contact_id` — optional, but send it when you have it. It's used
  as a fast first-match check (so re-importing the same device contact
  after its phone number changed still updates the same backend contact,
  rather than creating a duplicate because the phone no longer matches).
- 1–2000 contacts per call (`422` if you send 0 or more than 2000 — batch
  larger imports into multiple calls).

**200 OK:**
```json
{
  "imported": 12,
  "updated": 3,
  "requires_review": [
    {
      "name": "Ambiguous Person",
      "device_contact_id": "device-local-id-xyz",
      "matched_contact_ids": ["3fae...", "9bc1..."]
    }
  ]
}
```

**Errors:**
| Status | code | When |
|---|---|---|
| 401 | `invalid_credentials` | Missing/expired access token |
| 403 | `email_not_verified` / `account_locked` | Same as every authenticated endpoint |
| 422 | `validation_error` | Bad shape — see `errors[]` |
| 429 | `rate_limit_exceeded` | More than 10 import calls/minute from this IP |

---

### `GET /`

List the user's contacts (deleted ones excluded), sorted by name.

**Query params:** `limit` (default 50, max 200), `offset` (default 0).

**200 OK:**
```json
{
  "items": [
    {
      "id": "3fae2c10-...",
      "name": "Jane Doe",
      "birthday_month": 6,
      "birthday_day": 15,
      "birthday_year": 1990,
      "source": "device_import",
      "notes": null,
      "created_at": "2026-10-11T12:00:00Z",
      "updated_at": "2026-10-11T12:00:00Z"
    }
  ],
  "total": 42,
  "limit": 50,
  "offset": 0
}
```

Note this list intentionally does **not** include phone numbers or emails
— there's no "contact detail with identifiers" view in this milestone.
If you need to show a contact's phone/email back to the user, flag it;
it's a small addition (a `GET /{id}/identifiers`-style endpoint) that
wasn't needed for anything built so far.

---

### `GET /{id}`

Fetch one contact by id. Same shape as one item from the list above.

**404** (`contact_not_found`) if it doesn't exist, isn't yours, or has
been deleted.

---

### `PATCH /{id}`

Edit a contact. All fields optional — send only what changed.

```json
{ "name": "Jane A. Doe", "birthday_month": 6, "birthday_day": 16, "notes": "met at conference" }
```

**Important:** if the request includes any of `birthday_month`,
`birthday_day`, `birthday_year`, the contact's `source` flips to
`"manual"` as a side effect. That's intentional (§B above) — it's what
protects a hand-corrected birthday from being silently overwritten by a
future device re-import. There's no way to edit the birthday without this
flip; if you need to preview or confirm that with the user, build that in
the UI, since the API doesn't expose a "dry run."

**200 OK:** the updated contact, same shape as `GET /{id}`.

**404** (`contact_not_found`) same as above.

---

### `DELETE /{id}`

Soft-deletes the contact (it stops showing up in `GET /` and `GET /{id}`
immediately). There's no "undo" endpoint yet — deletion is final from the
app's perspective even though the row technically still exists.

**204 No Content** on success. **404** (`contact_not_found`) if it's
already gone or isn't yours.

---

## D. Known limitations / what's deliberately not here yet

- **No merge endpoint.** `requires_review` tells you two existing contacts
  might be the same person, but there's no API to actually merge them.
  Surfacing this to the user as "needs review, can't fix it in-app yet" is
  the honest thing to show right now.
- **No way to fetch a contact's phone numbers/emails back.** Only `name`,
  `birthday`, `notes`, `source`, timestamps. See the note under `GET /`.
- **No bulk delete / bulk export.**
- **Groups do not exist yet.** There's no way to tag or group contacts, so
  anything like "send this reminder to 10 people at once" isn't buildable
  against this milestone — that depends on a Reminders + Groups domain
  that hasn't been started (see the implementation status log for where
  that's tracked).

## E. Local dev

Same setup as Milestone 1 — nothing new to install. After pulling this
branch, run the new migration:
```
alembic upgrade head
```
