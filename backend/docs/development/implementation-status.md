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

## Session 2 — 2026-10-11

### Completed
- Milestone 2 backend: contacts import + dedup, on branch
  `feat/contacts-import-dedup` (branched off `feat/backend-foundation`,
  since this depends on the unmerged auth foundation).
- New tables: `contacts`, `contact_identifiers` (migration
  `0002_contacts_and_identifiers`, verified both directions — `upgrade
  head` and `downgrade base` — against a real local Postgres 16 instance,
  same as Milestone 1's migration).
- Dedup logic implemented exactly per architecture doc §15.2/§15.4
  (`app/services/dedup.py`): matching is phone/email only, never name;
  `device_contact_id` is checked as a fast path before falling back to
  phone/email; zero matches creates, one match merges, more than one
  match is flagged in `requires_review` and left untouched; a malformed
  phone (`normalization_failed`) is stored but excluded from matching; a
  known full birthday is never downgraded to month-only, and a
  `manual`-source contact's birthday is never overwritten by import at
  all.
- Endpoints (`app/api/routers/contacts.py`, mounted at `/mobile/contacts`):
  `POST /import`, `GET /`, `GET /{id}`, `PATCH /{id}`, `DELETE /{id}`
  (soft delete). A `PATCH` that touches the birthday flips `source` to
  `manual` — see the router's docstring for why that's the right moment
  to apply the never-downgrade protection.
- 12 new tests (`app/tests/test_contacts.py`) covering the dedup service
  directly (create/update/multi-match-review/birthday-never-downgrades/
  manual-never-overwritten/device-id-fast-path/malformed-phone-excluded)
  and the router end-to-end (auth-required, import→list, 404 on another
  user's contact, PATCH flips source, DELETE soft-deletes). 31 tests
  total now pass (19 from Milestone 1 + 12 new). Ruff and mypy clean.
- OpenAPI regenerated; new frontend handoff doc:
  `docs/frontend-integration/milestone-2-contacts.md`.

### Decisions from the team, recorded 2026-10-11 (not yet implemented —
### logged here so the right phase picks them up; none of the underlying
### infrastructure exists in this codebase yet)
1. **Object storage: Cloudflare (R2) instead of AWS (S3).** Affects the
   architecture doc's object-storage phase (media/portfolio uploads,
   originally scoped against S3). No storage code has been written yet in
   this backend, so there's nothing to migrate — when that phase starts,
   build directly against Cloudflare R2 (S3-compatible API, so the
   `boto3`/`aioboto3`-style client code the doc assumed still applies,
   just pointed at R2's endpoint with R2 credentials instead of AWS's).
2. **Distributed lock for APScheduler.** The architecture doc's §13.3
   scheduler is currently specced as in-process/single-instance — no
   APScheduler code exists in this repo yet (reminders/scheduling is a
   later phase). When that phase starts, it needs a distributed lock
   (e.g. Postgres advisory lock, or a Redis lock once Redis is in place)
   so the job doesn't double-fire if this service ever runs more than one
   replica. Flagging now so the scheduler isn't built single-instance-only
   and then has to be reworked.
3. **Rate limiting / session caching: fall back to in-memory when Redis is
   down.** Current rate limiter (`core/rate_limiter.py`) is already
   in-process/SlowAPI, not Redis-backed — Redis-backed rate limiting is
   architecture doc Phase 10 (§25/§29), not built yet. When that phase
   adds the Redis-backed limiter/session cache, it needs a fallback path
   to the in-process behavior if Redis is unreachable, rather than failing
   closed (or open) on a Redis outage. Logged here for whoever picks up
   Phase 10.

### New feature request, recorded 2026-10-11 — scope note
User asked for an endpoint to apply one reminder to a group of users at
once ("bulk apply a reminder to a group"). This cannot be built against
what exists today: there is no `Reminder` model/table, and no concept of a
group at all — `app/db/models.py`'s `Contact` docstring explicitly notes
`group_id` was left out because no `message_groups` table exists yet.
Building this endpoint means standing up the Reminders domain (architecture
doc's scheduler phase, §13) and a Groups domain together, not a small
addition to Milestone 2. Treating this as the scope of **Milestone 3**
rather than bolting it onto contacts — will scope and build that as its
own branch/unit next, and will flag back if "group" should mean something
narrower (e.g. just multi-select contacts at send-time, no persisted Group
entity) before building persisted group membership that isn't needed.

### Tests
- `pytest`: 31 passed, 0 failed
- `ruff check app/`: clean
- `mypy app/`: clean
- `alembic upgrade head` + `downgrade base` for `0002`: verified against
  local Postgres 16 (same ephemeral-instance approach as Session 1)

### Branches / commits
- Branch: `feat/contacts-import-dedup` (off `feat/backend-foundation`)
- See git log on that branch for actual commit hash(es).

### Ready for frontend
Everything in `docs/frontend-integration/milestone-2-contacts.md` —
import, list, get, edit, soft-delete. Still nothing for reminders, groups,
or push notifications.

### Known blockers / unfinished
1. Same deploy-secrets blocker as Session 1 — unchanged.
2. Public-repo security exposure — still the user's call on timing, still
   unresolved, not re-raised every session per standing guidance.
3. The three team decisions above are recorded but **not implemented** —
   nothing to implement yet since their target subsystems don't exist in
   this codebase.

### Recommended next steps
1. Merge/review `feat/backend-foundation` and `feat/contacts-import-dedup`
   whenever the user is ready — neither has been merged to `main` per
   standing instruction not to merge without explicit permission.
2. Milestone 3: Reminders + Groups domain, scoped specifically to support
   "apply a reminder to a group of users at once" — needs a short
   scoping pass (what a "group" is, whether reminders are per-contact or
   per-user-account, how the APScheduler distributed-lock decision above
   factors in before that scheduler code is written) before implementation
   starts, to avoid building the wrong shape twice.
