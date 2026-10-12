# Implementation Status

Reconciled against the repository at each session start — this file is a
log, not a substitute for checking actual repo state before trusting it.

## Session 1 — 2026-10-10

### Completed
- Repo reconnaissance: read both Gitea-origin repos (mirrored to GitHub —
  Gitea itself wasn't reachable from the build environment) end-to-end
  against what the architecture doc claims they contain. Everything in the
  doc checked out against the real `bm-ts/backend` code, with two
  corrections: rate limiting is Postgres-backed for message-sends only (not
  auth), and the mobile repo's deploy target needs to move from Gitea's own
  registry trigger flow to a GitHub Actions + Coolify-webhook flow since the
  mobile repos now live on GitHub.
- **Security finding, reported, not yet actioned by the user:** the
  `linkedin` (bm-ts) repo is public and has committed Postgres backup dumps
  (`bm-ts/backend/backups/*.sql.gz` — real user emails, hashed passwords,
  contacts, and live password-reset/email-verification tokens) and what
  appear to be real `JWT_SECRET_KEY`/`SMTP_ENCRYPTION_KEY` values in
  `backend/.env.example`. User acknowledged and said they'd handle it later
  — not blocking, but still open.
- Milestone 1 backend: new standalone FastAPI service at `backend/` in this
  repo (`devpaul-01/mobile`), distinct database/secrets from the extension
  backend. Scope: auth only (register, login, refresh, logout, me,
  email-verification, password-reset) — matches architecture doc §11/§35.
- Patterns copied and adapted from `bm-ts/backend` (not reinvented):
  structured `AppError`/`ErrorCodes`, JWT access/refresh with session-bound
  rotation and reuse detection, Argon2 hashing, account lockout (5 attempts
  / 15 min), anti-enumeration password-reset responses, security headers
  middleware, SlowAPI rate limiting (IP-keyed — no Facebook-ID concept here,
  see `core/rate_limiter.py` docstring), Alembic async migration setup.
- Found and fixed one real bug while porting the pattern: `logout()`
  unconditionally called `_ensure_revoked_token` even when
  `_revoke_session_record` had already inserted the same JTI, causing a
  unique-constraint double-insert in the same transaction. Fixed in
  `api/routers/auth.py` (see its docstring on the `session_revoked` guard).
- Email delivery is stubbed (`services/auth_tokens.py` logs the token
  instead of sending) — real SendGrid integration is architecture doc §19 /
  Phase 8, intentionally not pulled forward.
- 19 tests, all passing. Ruff and mypy both clean (explicit minimal ruff
  rule selection pinned in `pyproject.toml` — see its comment for why).
- Migration `0001_initial_auth_schema` verified both directions
  (`upgrade head` / `downgrade base`) against a real local Postgres 16
  instance, not just SQLite — SQLite can't run this migration directly
  (`ALTER TABLE ADD CONSTRAINT` isn't supported there), which is expected
  and fine since production is always Postgres; the test suite itself still
  runs against SQLite for speed (see `app/tests/conftest.py`).
- GitHub Actions CI (`​.github/workflows/backend-ci.yml`): lint + typecheck +
  test job, plus a separate Postgres-service job that runs the migration
  both directions on every push touching `backend/**`.
- GitHub Actions deploy workflow (`.github/workflows/backend-deploy.yml`):
  builds the image, pushes to the same Gitea container registry the
  extension backend uses, triggers a Coolify webhook — mirrors
  `bm-ts`'s `deploy.yaml` pattern. Gated on pushes to `develop`, not `main`.
  **Not yet usable** — needs three GitHub repo secrets
  (`REGISTRY_USER`, `REGISTRY_PASSWORD`, `COOLIFY_WEBHOOK_URL`) added by
  whoever has access to those values; per the user's instruction, Claude
  does not have and should not be given those secrets directly.
- Frontend handoff doc: `docs/frontend-integration/milestone-1-auth.md`.
- API contract: `docs/api/openapi.yaml` — generated directly from the
  running app's `app.openapi()`, not hand-written, so it can't drift from
  the actual field names/types.

### Branches / commits
- Branch: `feat/backend-foundation`
- See git log on that branch for the actual commit(s) and hashes — this
  file doesn't duplicate them so it can't go stale relative to git.

### Tests
- `pytest`: 19 passed, 0 failed (`backend/app/tests/test_auth.py`)
- `ruff check app/`: clean
- `mypy app/`: clean
- `alembic upgrade head` + `alembic downgrade base`: verified against local
  Postgres 16 (ephemeral — not part of this repo, torn down after the
  check; re-verify in CI's Postgres-service job on every PR going forward)

### Ready for frontend
Everything in §B of the handoff doc — register through password reset.
Nothing involving contacts, reminders, approvals, or push notifications.

### Known blockers / unfinished
1. **Deploy secrets not yet in GitHub repo settings** (see above) — CI
   (lint/test/migrations) runs fine without them; only the deploy workflow
   is blocked.
2. **Public-repo security exposure** — user's call on timing, not re-raising
   every session, but still unresolved as of this entry.
3. `requirements.txt` in the original mobile-repo stub listed `celery`,
   which the architecture doc explicitly rejects in favor of in-process
   APScheduler (§13.3) — removed as part of this session's cleanup; flagged
   to the user, no response yet on whether that was leftover template cruft
   or an intentional reconsideration. Built to the doc's decision
   (APScheduler) by default unless told otherwise.

### Recommended next steps
1. Add the three deploy secrets to the GitHub repo (user/whoever has
   Coolify + registry access).
2. Milestone 2: contacts (import, CRUD, the dedup logic the architecture
   doc describes) — next smallest useful chunk per architecture doc §36.
3. Decide whether to pull SendGrid integration forward before Milestone 2
   or keep deferring it — affects whether real users can actually complete
   registration during any beta testing that happens before Phase 8.

## Redis adoption — 2026-10-11/12 (branch `feat/redis-infra`, off `feat/backend-foundation`)

Note: this branch was cut from `feat/backend-foundation` directly (not from
`feat/contacts-import-dedup`), since none of this touches contacts code —
keeping it a separate logical unit/PR per the standing git-discipline
instruction. Milestone 2's own status entry lives on its own branch and
will show up here once that branch merges.

### Why now, not Phase 10
Per an explicit team decision, Redis is adopted in this phase rather than
deferred to Phase 10 as `MOBILE_APP_ARCHITECTURE_SPEC.md` §25/§29/§41
originally scoped it. Decision, as given: use Redis now; fall back to
in-memory behavior when Redis is down, for both rate limiting and session
caching.

### Completed
- **Rate limiting** (`core/rate_limiter.py`): now Redis-backed when
  `REDIS_URL` is set, using SlowAPI/`limits`' own built-in
  `in_memory_fallback_enabled=True` — no custom fallback code needed. If
  Redis is unreachable, the limiter detects the dead storage, switches to
  an in-process counter automatically, and retries the real storage later
  (self-healing). If `REDIS_URL` isn't set at all (local dev, test suite),
  it's pure in-memory, same as Milestone 1.
- **Session-validity caching** (new `core/redis_client.py`, wired into
  `api/deps.py get_current_token_payload` and
  `api/routers/auth.py _revoke_session_record`): a short-TTL (60s) positive
  cache on top of the real Postgres `UserSession` check, not a replacement
  for it. **This is a deliberate, flagged departure from
  MOBILE_APP_ARCHITECTURE_SPEC.md §25**, which says session/token validity
  should never be cached, for durability/auditability of revocation. The
  trade-off accepted here, per the team's explicit instruction to use Redis
  for this now: a session revoked at the exact moment its cache entry was
  just written could still read as valid for up to 60 seconds if the
  explicit cache-invalidation call (which runs on every revocation path —
  logout, password-reset-revokes-all) itself fails. Every cache read is
  still a **positive-only** cache — a miss, or Redis being down, always
  falls through to the real Postgres check; nothing is ever trusted *only*
  because Redis said so.
- Every Redis call in both modules is wrapped so a Redis failure degrades
  to "behave as if Redis doesn't exist" — never a 500, never a security
  downgrade (a down Redis can only make things slower, by forcing more
  Postgres round trips / falling back to in-process rate limiting, never
  less correct).
- `redis` (the Python client) added to `pyproject.toml`. CI
  (`.github/workflows/backend-ci.yml`) now brings up a `redis:7-alpine`
  service container for the lint-and-test job, since `test_redis.py`
  exercises real Redis behavior, not just the unreachable-Redis fallback
  path.
- 6 new tests (`app/tests/test_redis.py`): session-cache round trip against
  real Redis, cross-session-id isolation, never-raises-when-unreachable (x2
  variants: not configured at all, configured but unreachable), and two
  rate-limiter tests (enforces against real Redis; falls back to in-memory
  and keeps enforcing when Redis is unreachable) built against a small
  standalone FastAPI app configured the same way `core/rate_limiter.py`
  configures the real one — the app-wide limiter is a singleton built once
  at import time from whatever `REDIS_URL` was in the process environment,
  so it can't be reconfigured per-test.
- `docker-compose.yml` and `.env.example` already had a `redis`
  service/`REDIS_URL` stubbed in from the original scaffold cleanup
  (Session 1) — nothing to add there, they just start being used now.

### Not done in this phase (intentionally — see §25's other Redis uses)
- Dashboard aggregate caching (`cache:dashboard:{user_id}`) — there's no
  dashboard endpoint yet (nothing to cache).
- PayPal pending-order mapping (`paypal:pending:{order_id}`) — no payments
  code exists yet.
Both are still Redis uses the architecture doc scoped to later phases; only
the two the team explicitly asked for now (rate limiting, session caching)
were pulled forward.

### Tests
- `pytest`: 25 passed (19 from Milestone 1 + 6 new), 0 failed — run with a
  real local Redis on port 6399 for the "Redis is up" cases.
- `ruff check app/`: clean
- `mypy app/`: clean

### Branches / commits
- Branch: `feat/redis-infra` (off `feat/backend-foundation`, not off the
  contacts branch — no dependency between them)

### Recommended next steps
1. Merge whenever ready — same standing rule, not merged to `main` without
   explicit permission.
2. Milestone 3 (separate branch, off `feat/contacts-import-dedup` since it
   needs the `Contact` model): reminders, scoped specifically to "apply a
   reminder to several selected contacts at once" per the team's
   explicit scope answer (multi-select contacts at creation time, no
   persisted Group entity) — not the full scheduler/push/email/approval
   machinery from architecture doc Phases 6–9, which is a separate,
   larger undertaking.
