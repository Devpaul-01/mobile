# Milestone 1: Auth Backend — Frontend Integration Guide

Written for someone picking this up without prior context on the backend. If
something here doesn't match what you see, trust the code over this doc and
flag it — ping Seyi.

**Machine-readable contract:** `docs/api/openapi.yaml` in this same repo —
generated directly from the running code (not hand-written), so it cannot
drift from the field names/types below. If you use an API client generator
(openapi-typescript, orval, etc.), point it at that file.

---

## A. What's ready

A complete, tested account system: register, verify email, log in, stay
logged in across app restarts, log out, reset a forgotten password. Nothing
about contacts, reminders, or push notifications yet — that's the next
milestone.

Every endpoint below has an automated test backing it (19 tests, all
passing — `backend/app/tests/test_auth.py`), and the migration that creates
the database tables has been run against a real Postgres database, not just
SQLite (`backend/alembic/versions/0001_initial_auth_schema.py`).

## B. Available endpoints

Base path for everything below: `/mobile/auth`. So `POST /register` in this
doc means `POST {API_URL}/mobile/auth/register`.

None of these need special headers beyond `Content-Type: application/json`
except where noted. None take query parameters.

---

### `POST /register`

Create an account. Does **not** log the user in — they must verify their
email first.

**Body:**
```json
{ "email": "jane@example.com", "password": "at-least-8-chars" }
```

**201 Created:**
```json
{ "message": "Account created. Please check your email to verify your account before logging in." }
```

**Errors:**
| Status | code | When |
|---|---|---|
| 400 | `email_already_registered` | Email already has an account |
| 422 | `validation_error` | Password under 8 chars, or email isn't a valid address — see `errors[]` for which field |
| 429 | `rate_limit_exceeded` | More than 5 requests/minute from this IP |

**Email sending is stubbed right now** (logs to the backend console instead
of sending a real email — see §G). Ask Seyi for the verification token
directly during development rather than expecting an inbox.

---

### `POST /verify-email`

Consume the token from the verification email/link.

**Body:**
```json
{ "token": "the-token-string" }
```

**200 OK:**
```json
{ "message": "Email verified. You can now log in." }
```

**400 Bad Request**, code `not_found`, if the token is wrong, already used, or expired (tokens last 24 hours).

---

### `POST /login`

**Body:**
```json
{ "email": "jane@example.com", "password": "at-least-8-chars" }
```

**200 OK:**
```json
{
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "token_type": "bearer"
}
```

**Save both tokens** (see §D for where). Use `access_token` on every
authenticated request; use `refresh_token` only to get a new pair when the
access token expires.

**Errors:**
| Status | code | When |
|---|---|---|
| 401 | `invalid_credentials` | Wrong email or password (same error for both — don't tell the user which one was wrong) |
| 403 | `email_not_verified` | Account exists but hasn't verified email yet |
| 403 | `account_locked` | 5 failed attempts in a row — locked for 15 minutes |
| 429 | `rate_limit_exceeded` | More than 5/minute or 20/hour from this IP |

---

### `POST /refresh`

Exchange a refresh token for a brand-new pair. **Important:** the old
refresh token stops working the instant you call this — see §D for why that
matters for your refresh logic.

**Body:**
```json
{ "refresh_token": "eyJ..." }
```

**200 OK:** same shape as `/login`.

**401 Unauthorized**, code `invalid_refresh_token`, if the token is
expired, already used, or doesn't belong to a real session. **Treat this as
"the user must log in again"** — clear stored tokens and send them to the
login screen.

---

### `POST /logout`

**Body:**
```json
{ "refresh_token": "eyJ..." }
```

**200 OK:**
```json
{ "message": "Logged out" }
```

Call this when the user taps "log out," then clear local tokens regardless
of the response — don't block the UI on this call succeeding.

---

### `GET /me`

Returns the logged-in user's profile. **Requires auth** — send
`Authorization: Bearer <access_token>`.

**200 OK:**
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "email": "jane@example.com",
  "created_at": "2026-10-10T12:00:00Z",
  "email_verified": true,
  "name": null,
  "timezone": null
}
```

`name` and `timezone` are `null` until a later milestone adds a way to set
them — the columns exist, there's just no endpoint to write them yet.

**401 Unauthorized** (`invalid_credentials`) if the access token is missing, expired, or malformed — refresh and retry once (see §D).

---

### `POST /request-email-verification`

Resend the verification email for an unverified account — use this for a
"resend verification email" button on the `email_not_verified` login error
(§E).

**Body:** `{ "email": "jane@example.com" }`

**200 OK always**, same anti-enumeration reasoning as `/request-password-reset` below — don't infer anything from the response about whether the account exists or was already verified.

---

### `POST /request-password-reset`

**Body:** `{ "email": "jane@example.com" }`

**200 OK always**, even if the email doesn't exist — this is intentional
(prevents someone from using this endpoint to check which emails have
accounts). Don't show a different message based on the response; always say
something like "If that account exists, check your email."

---

### `POST /reset-password`

**Body:**
```json
{ "token": "the-token-from-the-email", "new_password": "new-password-here" }
```

**200 OK:** `{ "message": "Password reset. Please log in with your new password." }`

This also **logs the user out everywhere** — every existing session's
tokens stop working, by design (if someone reset the password, any old
session might belong to whoever had the old password).

**400 Bad Request** (`not_found`) if the token is wrong, used, or expired (30 minutes).

---

## C. Request/response shape reference

Every error response (any 4xx/5xx) has this shape:

```json
{
  "detail": "Human-readable message, safe to show directly or use as a fallback",
  "code": "machine_readable_snake_case_code",
  "timestamp": "2026-10-10T12:00:00Z",
  "errors": null
}
```

`errors` is only non-null on `422` validation errors, and looks like:
```json
{
  "errors": [
    { "field": "password", "message": "String should have at least 8 characters" }
  ]
}
```

**Match on `code`, never on `detail`.** `detail` strings may get reworded later; `code` values are a contract and won't change without this doc changing too.

## D. Authentication and session state

**How the token pair works:**
- `access_token` — short-lived (15 minutes). Send it as `Authorization: Bearer <token>` on every authenticated request.
- `refresh_token` — long-lived (30 days). Use it **only** to call `/refresh` when the access token expires. Never send it as an `Authorization` header.

**Store both in SecureStore** (`expo-secure-store`, already in this repo's
dependencies), never in AsyncStorage or plain state — these are
credentials, not app data.

**The refresh token is single-use — this changes how you must write your refresh logic:**

Calling `/refresh` immediately invalidates the refresh token you sent it,
even if you don't use the new one. This means:

- ✅ **Do:** keep one in-flight refresh call at a time. If multiple API calls discover an expired access token at once, queue them behind a single `/refresh` call, then retry all of them with the new access token.
- ❌ **Don't:** fire `/refresh` from two places independently (e.g. a background timer *and* a 401 interceptor) — the second call will get a 401 `invalid_refresh_token` because the first one already rotated it, even though nothing is actually wrong.

A standard pattern: a single `refreshPromise` variable that every caller
awaits instead of each calling `/refresh` themselves.

**Suggested flow:**
1. On app start, read stored tokens. If none, go to login.
2. On every API call, attach `access_token`. If the call returns 401, call `/refresh` once (using the shared in-flight promise above), retry the original call with the new access token. If `/refresh` itself returns 401, clear tokens and go to login — the session is over.
3. On login/register success, store both tokens.
4. On logout, call `/logout`, then clear stored tokens regardless of the response.

## E. Error handling — what needs special frontend behavior

- **`account_locked` (403)** — tell the user to wait 15 minutes rather than letting them keep retrying; there's no "unlock" action available to them.
- **`email_not_verified` (403)** on login — offer a way to resend the verification email (you'll need a "resend" button calling a verification-resend endpoint; not built yet, see §G).
- **`rate_limit_exceeded` (429)** — show a generic "too many attempts, try again shortly" rather than exposing the exact limit; limits may be tuned later without notice.
- **`invalid_refresh_token` (401)** from `/refresh` — always means "log in again," never retry this one.

## F. Configuration and local testing

**Running the backend locally:**
```bash
cd backend
cp .env.example .env
# .env.example has placeholder secrets — JWT_SECRET_KEY etc. See its
# comments for how to generate real ones for local dev.
docker-compose up
```

This starts Postgres, Redis (not used yet, but already wired for later
milestones), and the backend with migrations applied automatically. The API
is then at **`http://localhost:8001`**.

**From the Expo app:** point `EXPO_PUBLIC_API_URL` (or whatever env var name
the app's API client expects) at `http://localhost:8001`. If you're testing
on a physical device rather than a simulator, `localhost` won't reach your
laptop — use your machine's LAN IP instead, and make sure `CORS_ORIGINS` in
`backend/.env` includes wherever the Expo dev server is actually running
from (default `http://localhost:8081`).

**Interactive API docs:** once the backend is running, FastAPI serves a live
Swagger UI at `http://localhost:8001/docs` — you can try every endpoint
from the browser without writing any frontend code first. Good for
sanity-checking a request shape before wiring it up.

**No real email provider is configured yet** — see §G. During development,
watch the backend's console output; verification/reset "emails" are logged
there as `[dev email stub] ... token=...`.

## G. Known limitations — not ready yet

- **Email is not actually sent.** Registration and password-reset "emails" are logged to the backend console, not delivered. Real sending (SendGrid) is a later milestone. Until then, dev/testing requires getting the token from the backend logs or asking Seyi for it directly.
- **No session list / "log out other devices" endpoint** — `/me` exists, but there's no way yet to see or revoke other active sessions. Planned for a later milestone (the extension backend has this pattern already, if you want a preview of the shape it'll take).
- **No profile-update endpoint** — `/me` returns `name`/`timezone` but there's no `PUT` to set them yet.
- **No account deletion or data export endpoints yet.**
- **Rate limits are per-server-process, not shared across multiple backend instances.** Not your concern as a frontend engineer, just don't be surprised if the exact threshold feels inconsistent in a multi-instance deployment later — this is a known, intentional-for-now backend limitation, not a bug to report.

## H. Frontend action items

You can build, right now, against what's live:

- [ ] Registration screen → `POST /register`
- [ ] "Check your email" screen after registration (email not real yet — see §G, so build the UI but expect to test it with a token Seyi gives you directly)
- [ ] Email verification handling (deep link or manual token entry for now — no `verify-email-page` HTML endpoint like the extension backend has; that's a `GET` HTML page pattern we deliberately did **not** carry over to mobile, see the architecture doc §22)
- [ ] Login screen → `POST /login`, handle all four error cases in §B
- [ ] "Resend verification email" button → `POST /request-email-verification`
- [ ] Token storage in SecureStore (§D)
- [ ] Axios/fetch interceptor implementing the single-flight refresh pattern in §D — **this is the part most likely to have subtle bugs if rushed, worth getting right first**
- [ ] `/me` call on app start to confirm the stored session is still valid, redirect to login on 401
- [ ] Logout button → `POST /logout` + clear tokens
- [ ] "Forgot password" flow → `POST /request-password-reset` → `POST /reset-password`
- [ ] Account-locked and rate-limit messaging (§E)

Not buildable yet (next milestone): anything to do with contacts, reminders, or notifications.
