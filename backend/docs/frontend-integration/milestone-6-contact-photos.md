# Milestone 6: Contact Photo Upload (Cloudflare R2) — Frontend Integration Guide

Written for someone picking this up without prior context. If something
here doesn't match the code, trust the code and flag it.

**Machine-readable contract:** `docs/api/openapi.yaml`.

All endpoints below require the `Authorization: Bearer <access_token>`
header, same as contacts/reminders.

---

## A. What this is, and the one thing that differs from the architecture doc

A user can attach **one optional photo per contact** (no photo library,
no multiple images — matches the architecture doc's explicit MVP scope
cut). The actual image bytes never pass through our backend — the mobile
app uploads directly to object storage using a short-lived presigned
upload, and the backend only ever stores metadata (`ContactPhoto`: object
key, content type, size).

**Storage provider: Cloudflare R2, not AWS S3** — an explicit team
decision. R2's API is S3-compatible, so nothing about the request/response
shapes below differs from a "normal" S3 presigned-upload flow; this is
purely a backend configuration detail, invisible to the client.

**One real deviation from the architecture doc's upload diagram, worth
knowing before you build the upload UI:** the doc's §26.2 diagram shows
the client doing a plain `PUT` to a presigned URL. This implementation
uses a **presigned POST** instead (`url` + `fields`, a multipart form
upload), because only a presigned POST's policy conditions let the
storage layer itself enforce the 5MB size limit and content-type match
— a bare PUT URL has no such enforcement. Concretely:

```
POST <upload_url>  (multipart/form-data)
  field: key            = <object_key from the presign response>
  field: Content-Type   = <the same content_type you requested>
  field: <...other fields from the presign response, send them all as-is>
  field: file            = <the actual image bytes, this field last>
```

Most HTTP/upload libraries (and `FormData` in JS) handle this natively —
just make sure every field from the presign response's `fields` object is
included in the form, in addition to the file itself.

## B. Flow, end to end

```
1. POST /mobile/uploads/presign { content_type }
     → { upload_url, fields, object_key, expires_at }
2. Client does the multipart POST described above, directly to upload_url
     (bypasses our backend entirely for the actual bytes)
3. POST /mobile/uploads/confirm { object_key, contact_id }
     → backend verifies the object actually exists and matches size/type
       expectations (never trusts the client's claim that step 2 worked)
     → creates/replaces the ContactPhoto row
     → { id, contact_id, content_type, size_bytes, uploaded_at, view_url }
4. Show the photo using view_url (a short-lived presigned GET — re-fetch
   a fresh one via step 5 if it's gone stale, don't cache it long-term)
5. GET /mobile/uploads/contact-photo/{contact_id}
     → re-fetch metadata + a fresh view_url any time you need to display
       the photo again later (e.g. app relaunch)
```

## C. Endpoints

Base path: `/mobile/uploads`.

---

### `POST /presign`

```json
{ "content_type": "image/jpeg" }
```
- `content_type` — one of `image/jpeg`, `image/png`, `image/webp`. Anything
  else is a **400** (`invalid_content_type`), rejected before a presigned
  upload is even generated.
- `purpose` — optional, defaults to `"contact_photo"` (the only value
  that exists today — included in the schema so a second upload purpose,
  if one's ever added, doesn't need a breaking change here).

**200 OK:**
```json
{
  "upload_url": "https://...",
  "fields": { "key": "...", "Content-Type": "image/jpeg", "...": "..." },
  "object_key": "<user_id>/contacts/<uuid>.jpg",
  "expires_at": "..."
}
```
This presigned upload is valid for **5 minutes** — if the upload hasn't
happened by then, request a new one rather than retrying the same `url`.

Note there's no `contact_id` in this request — see §D for why, and don't
read anything into that beyond "the backend doesn't know which contact
this is for yet at this step."

---

### `POST /confirm`

```json
{ "object_key": "<from the presign response>", "contact_id": "..." }
```

**200 OK:** the `ContactPhoto`, including a fresh `view_url`.
**400** (`upload_not_found`) if nothing exists at that `object_key` yet —
show the user an upload-failed state and let them retry from step 1.
**400** (`invalid_content_type`) if the uploaded object exceeds 5MB (the
oversized object is deleted from storage automatically) or isn't an
allowed content type.
**404** (`contact_not_found`) if `contact_id` doesn't exist, isn't yours,
or was deleted.

Confirming a new photo for a contact that already has one **replaces**
it — the old object is deleted from storage, not left orphaned.

---

### `GET /contact-photo/{contact_id}`

**200 OK:** the contact's `ContactPhoto` with a freshly-issued `view_url`.
**404** (`contact_photo_not_found`) if the contact has no photo.

---

### `DELETE /contact-photo/{contact_id}`

Removes the photo (deletes both the metadata row and the storage object).
**204 No Content** on success. **404** (`contact_photo_not_found`) if
there isn't one.

## D. Known limitations

- **No ownership check at presign time.** The presign request carries no
  `contact_id` (matches the architecture doc's own diagram exactly), so
  there's nothing to check ownership of yet — ownership is enforced
  entirely at `/confirm`, where `contact_id` is actually known. Don't
  treat a successful presign as proof the eventual contact is valid;
  `/confirm` is the real gate.
- **No orphan cleanup yet.** If a client gets a presigned upload and
  never calls `/confirm` (abandoned upload), the object stays in R2
  indefinitely for now — the architecture doc's daily orphan-sweep job
  (§26.3) isn't built in this milestone. Low-cost, low-urgency; flagged
  rather than silently skipped.
- **No automatic storage cleanup on contact/account deletion yet.**
  Deleting a contact or account doesn't currently also delete its R2
  object — this is explicitly tied to the (not-yet-built) account
  deletion flow (architecture doc §27.2, Phase 13), which is where that
  cascade cleanup is scoped to happen.
- **Still just one photo per contact**, by the architecture doc's own
  explicit MVP scope cut — no gallery/library feature exists or is
  planned for this milestone.

## E. Local dev

Same setup as Milestones 1-5, plus these new required env vars for the
upload endpoints to work (everything else in the app still boots fine
without them — only the upload endpoints themselves fail loudly if
unset):
```
STORAGE_ENDPOINT_URL=https://<account_id>.r2.cloudflarestorage.com
STORAGE_BUCKET_NAME=<bucket name>
STORAGE_ACCESS_KEY_ID=<R2 access key id>
STORAGE_SECRET_ACCESS_KEY=<R2 secret access key>
```
After pulling this branch:
```
alembic upgrade head
```
