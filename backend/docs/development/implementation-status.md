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

## Session 3 — 2026-10-12 (branch `feat/reminders`, off `feat/contacts-import-dedup`)

### Scope answer received
Asked above whether "apply a reminder to a group of contacts" meant a
persisted Group entity or a one-time multi-select — answer: **multi-select,
no Group entity.** Built accordingly.

### Completed
- `Reminder` model + migration `0003_reminders` (verified both directions
  against real Postgres). One contact can have more than one reminder row
  (e.g. separate "3 days before" and "on the day" reminders) — no
  uniqueness constraint added, matching the architecture doc's §36
  one-to-many relationship rather than inventing a restriction it doesn't
  specify.
- `channels` stored as a JSON list of strings (`push`/`email`/`whatsapp`),
  not the architecture doc's suggested array/bitmask column — portable
  across Postgres and the SQLite test DB, no bit-arithmetic needed for 3
  possible values. Flagged as a deliberate implementation choice, not an
  oversight.
- Endpoints (`app/api/routers/reminders.py`, `/mobile/reminders`):
  `POST /` (single contact), **`POST /bulk`** (the explicit ask — apply one
  reminder definition to a multi-selected list of contact IDs, 1–500 per
  call; a contact not owned/not found is skipped and reported in
  `skipped_contact_ids`, not a whole-call failure), `GET /` (paginated,
  optional `contact_id` filter), `GET /{id}`, `PATCH /{id}`, `DELETE /{id}`
  (hard delete — reminders aren't soft-deleted, unlike contacts; see the
  model docstring for why).
- **Explicitly not built in this milestone** (see
  `docs/frontend-integration/milestone-3-reminders.md` §A for the
  frontend-facing version of this caveat): the scheduler, `ReminderOccurrence`,
  push/email/WhatsApp delivery, and the pending-approval state machine
  (architecture doc Phases 6-9). A `Reminder` row today is a stored
  preference that nothing acts on yet — creating one does not send
  anything. Kept out deliberately, per the standing instruction not to
  build the entire backend in one pass; this is flagged loudly here and in
  the frontend doc so it's never mistaken for "reminders work end-to-end."
- 8 new tests (`app/tests/test_reminders.py`): auth-required, 404 on a
  contact not owned, 422 on an invalid channel, full CRUD round trip, bulk
  apply creating one reminder per selected contact, bulk apply skipping
  (not failing on) a contact the caller doesn't own, and empty-list/auth
  validation on the bulk endpoint. 39 tests total on this branch (31 from
  Milestones 1-2 + 8 new).
- OpenAPI regenerated; new frontend doc:
  `docs/frontend-integration/milestone-3-reminders.md`.

### Tests
- `pytest`: 39 passed, 0 failed
- `ruff check app/`: clean
- `mypy app/`: clean
- `alembic upgrade head` + `downgrade base` for `0003`: verified against
  local Postgres 16

### Branches / commits
- Branch: `feat/reminders` (off `feat/contacts-import-dedup` — needs the
  `Contact` model, so it can't be independent of that branch the way
  `feat/redis-infra` is)

### Ready for frontend
Everything in `docs/frontend-integration/milestone-3-reminders.md` —
create, bulk-apply, list, get, edit, delete. Reiterating because it's
easy to miss: **no notification is ever sent as a result of these calls
yet.**

### Known blockers / unfinished
1. Same deploy-secrets and public-repo-security items as prior sessions —
   unchanged, not re-raised in detail each time.
2. Scheduler/delivery (Phases 6-9) is the natural next piece of work once
   the user wants reminders to actually do something, and is a
   substantially larger unit (APScheduler job, timezone-aware trigger
   calculation, `ReminderOccurrence` idempotency, push/email/WhatsApp
   services, the pending-approval state machine) — not started.

### Recommended next steps
1. Merge/review whenever the user is ready — three open branches now
   (`feat/backend-foundation`, `feat/contacts-import-dedup`,
   `feat/redis-infra`, `feat/reminders`), none merged to `main`.
2. Decide whether the scheduler/delivery phase is next, or whether
   something else (payments, object storage, the mobile client itself) is
   a higher priority before reminders actually fire.

## Session 4 — 2026-10-12 (branch `feat/reminder-scheduler`, off `feat/reminders`)

### What shipped
- **The reminder scheduler is real now** — Phases 6-7 of the architecture
  doc (`app/services/scheduler.py`). An in-process APScheduler job
  (`AsyncIOScheduler`, 15-minute interval, `max_instances=1`,
  `coalesce=True` — §13.3's "no separate worker needed for one periodic
  job" call) checks every enabled `Reminder` and fires the ones whose
  trigger moment has landed in the trailing 15-minute window.
- **Trigger-time calculation** checks 3 candidate years
  (`today.year - 1/0/+1`) per reminder per pass, not just the current
  year — needed because `lead_time_days` (0-365) can push the trigger
  moment into an adjacent calendar year (e.g. a Jan 2 birthday with a
  5-day lead fires in *December of the prior year*). Fixed local send
  time of 9:00 AM in the user's own `timezone` (defaulting to UTC),
  converted to UTC for the comparison. Feb 29 falls back to Feb 28 on a
  non-leap candidate year.
- **`ReminderOccurrence`** (new table) is the idempotency anchor per §16.1
  — one row per `(reminder_id, occurrence_year)`, enforced by a unique
  constraint the scheduler relies on as the actual correctness guarantee
  (not a pre-check `SELECT`, which would race under concurrent runs). The
  insert is wrapped in a SAVEPOINT (`db.begin_nested()`), not a plain
  `db.rollback()` — a bug caught before it shipped: a full rollback on a
  duplicate-occurrence conflict would have destroyed every *other*
  occurrence already created earlier in the same evaluation pass, before
  the caller gets to commit.
- **Distributed lock, now implemented** — the team's earlier decision
  ("implement distributed lock mechanism for apscheduler," Session 2/3)
  was logged as deferred specifically because no scheduler existed yet;
  that precondition is gone now that this branch adds one. Implemented as
  a Postgres session-level advisory lock
  (`pg_try_advisory_lock`/`pg_advisory_unlock`, fixed key) around each
  scheduler cycle, deliberately choosing Postgres over Redis to avoid
  coupling this branch to the not-yet-merged `feat/redis-infra` branch. A
  non-Postgres bind (SQLite, in tests) always succeeds — there's no
  multi-instance concern to guard against there.
- **Email delivery + retry bookkeeping** (§16.3): `EmailStatus`
  (pending/sent/failed/failed_permanent) and `email_attempts` tracked on
  `ReminderOccurrence`. A failed send is retried on later scheduler passes
  (`retry_failed_emails`), up to 3 total attempts, then marked
  `failed_permanent`.
- **Push delivery**: fire-and-forget per §16.3 — no retry, no status
  tracking on the occurrence at all (unlike email). Sent to every
  `PushToken` row registered for the user.
- **Both `services/email.py` and `services/push.py` are stubs** — they
  log instead of calling a real provider (SendGrid / APNs-FCM), mirroring
  the existing `services/auth_tokens.py._deliver` pattern from Milestone
  1. Swapping in real providers later is isolated to these two files.
- **`whatsapp` channel: explicitly not implemented.** The architecture
  doc's WhatsApp design needs a `PendingApproval`/approval-history state
  machine (Phase 9) that doesn't exist in this codebase. A reminder with
  `whatsapp` in its channels still gets its `ReminderOccurrence` created
  (so the yearly-fire bookkeeping stays correct once Phase 9 lands), but
  nothing is sent, queued, or logged as a real attempt — flagged loudly in
  code and in the frontend doc rather than silently dropped. Deferred to
  its own future branch, per the standing instruction not to build the
  entire backend in one pass.
- **`PushToken`** (new table, minimal slice of Phase 7): upsert on
  `(user_id, device_token)` per §12.2/§35 via a new endpoint —
  `POST /mobile/notifications/register-token`
  (`app/api/routers/notifications.py`). No status/retry tracking, since
  push failures are never retried.
- **`app/main.py`**: added a `lifespan` context manager that calls
  `init_scheduler()` on startup (guarded by `settings.scheduler_enabled`,
  a new config flag) and shuts the scheduler down on app shutdown.
  `conftest.py` forces `SCHEDULER_ENABLED=false` for the whole test suite
  so pytest never starts a real background job against the per-test
  SQLite engine.
- Migration `0004_reminder_occurrences_and_push_tokens` — both tables,
  verified in both directions (`upgrade head`, `downgrade -1`,
  `downgrade base`) against local Postgres 16.
- 18 new tests (14 in `app/tests/test_scheduler.py` covering trigger-time
  correctness, timezone offset, the year-wraparound edge case, the Feb-29
  fallback, idempotency across repeated runs, disabled/soft-deleted
  skipping, push/email/whatsapp downstream actions, and email retry
  progression including the permanent-failure path; 4 in
  `app/tests/test_notifications.py` covering auth, create, upsert, and
  invalid-platform validation). 57 tests total on this branch (39 from
  Milestones 1-3 + 18 new).
- OpenAPI regenerated; new frontend doc:
  `docs/frontend-integration/milestone-4-scheduler.md`.

### Tests
- `pytest`: 57 passed, 0 failed
- `ruff check app/ alembic/`: clean
- `mypy app/`: clean
- `alembic upgrade head`, `downgrade -1`, `downgrade base` for `0004`:
  verified against local Postgres 16

### Branches / commits
- Branch: `feat/reminder-scheduler` (off `feat/reminders` — needs the
  `Reminder` model)

### Ready for frontend
Everything in `docs/frontend-integration/milestone-4-scheduler.md` — the
new push-token registration endpoint, plus the fact that `push` and
`email` reminders now actually run on a schedule (sending is still
stubbed/logged, not a real provider call yet — see that doc's §C).

### Known blockers / unfinished
1. Same deploy-secrets and public-repo-security items as prior sessions —
   unchanged, not re-raised in detail each time.
2. Real push/email provider integration (APNs/FCM or Expo; SendGrid) —
   both are stubs today.
3. The WhatsApp pending-approval state machine (architecture doc Phase 9)
   — the next major deferred unit, scoped as its own future branch.

### Recommended next steps
1. Merge/review whenever the user is ready — five open branches now
   (`feat/backend-foundation`, `feat/contacts-import-dedup`,
   `feat/redis-infra`, `feat/reminders`, `feat/reminder-scheduler`), none
   merged to `main`.
2. Scope and build the WhatsApp pending-approval state machine (Phase 9)
   as its own branch.
3. Wire up real push/email providers when ready to actually notify real
   devices/inboxes.

## Session 5 — 2026-10-13 (branch `feat/whatsapp-approvals`, off `feat/reminder-scheduler`)

### What shipped
- **The WhatsApp pending-approval state machine is built** — architecture
  doc §17, Phase 9. This was the one remaining channel the scheduler
  (Milestone 4) only logged a warning for; it's now wired to a real state
  machine, matching the product constraint that WhatsApp can never be
  auto-sent.
- **New tables**: `PendingApproval` (one row per WhatsApp send awaiting
  the user's manual tap, unique on `reminder_occurrence_id` as
  belt-and-suspenders alongside the occurrence table's own uniqueness —
  §17.3) and `ApprovalHistory` (append-only audit trail of every state
  transition, §17.4 — `from_state` nullable only for the creation row).
- **Every transition in §17.1's diagram, implemented exactly**
  (`app/services/approvals.py`):
  `PENDING_APPROVAL → APPROVED` (user taps "Send via WhatsApp", recorded
  *before* the client opens the deep link so it survives a user backing
  out), `APPROVED → COMPLETED`/`LINK_FAILED` (client reports whether
  `Linking.openURL` resolved), `PENDING_APPROVAL → EXPIRED` (scheduler
  sweep, 7-day window, row kept not deleted), `PENDING_APPROVAL →
  DISMISSED` (explicit user action, distinguished from EXPIRED for
  analytics). Every transition writes an `ApprovalHistory` row through a
  single `_transition` helper — the only place `state` is ever written —
  so no transition can happen unlogged.
- **The one deliberate exception to "invalid transition = 409," per §17.2
  and the architecture doc's own §43 handoff note**: a `report-opened`
  call that arrives after the approval already reached `COMPLETED`/
  `LINK_FAILED` (a client retry after a slow response) is a benign
  no-op, not a 409 — implemented exactly as flagged in the spec as a
  deliberate exception to the general rule.
- **Endpoints** (`app/api/routers/approvals.py`, `/mobile/approvals`):
  `GET /` (paginated, optional `state` filter), `POST /{id}/approve`,
  `POST /{id}/report-opened`, `POST /{id}/dismiss`. No create endpoint —
  a `PendingApproval` only ever comes from the scheduler firing a
  whatsapp-channel reminder, never a direct API call.
- **Scheduler wiring** (`app/services/scheduler.py`): the `whatsapp`
  branch of `_trigger_downstream_actions` now calls
  `create_pending_approval` instead of only logging, and also sends a
  push notification pointing at the Approval Queue (§17.2 — "also
  triggers a push notification," regardless of whether `push` is itself
  one of the reminder's own channels) via a new
  `send_approval_queue_push` stub in `services/push.py`, kept distinct
  from the existing birthday-reminder push so the two notification types
  don't get conflated in logs/analytics later. `run_scheduler_cycle` now
  also runs `expire_stale_approvals` every cycle, alongside occurrence
  evaluation and email retry.
- Migration `0005_pending_approvals_and_approval_history` — both tables,
  verified in both directions (`upgrade head`, `downgrade -1`,
  `downgrade base`) against local Postgres 16.
- 20 new tests: 19 in `app/tests/test_approvals.py` (every transition in
  §17.1's diagram including the report-opened idempotency exception, the
  409 cases, the expiry sweep's three branches (expires stale items,
  ignores not-yet-due items, ignores already-settled items), and the
  full HTTP layer — auth, 404 on not-owned, end-to-end approve →
  report-opened flow, dismiss, state filtering), plus 1 new test in
  `app/tests/test_scheduler.py` confirming the scheduler sends the
  approval-queue push (an existing scheduler test was also updated,
  not just renamed, to assert a real `PendingApproval` row is created,
  not only an occurrence). 77 tests total on this branch (57 from
  Milestones 1-4 + 20 new).
- OpenAPI regenerated; new frontend doc:
  `docs/frontend-integration/milestone-5-whatsapp-approvals.md`, which
  also explicitly corrects the Milestone 4 doc's now-outdated "whatsapp:
  nothing happens" note.

### Tests
- `pytest`: 77 passed, 0 failed
- `ruff check app/ alembic/`: clean
- `mypy app/`: clean
- `alembic upgrade head`, `downgrade -1`, `downgrade base` for `0005`:
  verified against local Postgres 16

### Branches / commits
- Branch: `feat/whatsapp-approvals` (off `feat/reminder-scheduler` —
  needs `ReminderOccurrence`)

### Ready for frontend
Everything in `docs/frontend-integration/milestone-5-whatsapp-approvals.md`
— the approval queue list + approve/report-opened/dismiss actions, plus
the precise, important distinction between `COMPLETED` ("we handed off to
WhatsApp") and actual message delivery (unobservable, never claimed).

### Known blockers / unfinished
1. Same deploy-secrets and public-repo-security items as prior sessions —
   unchanged, not re-raised in detail each time.
2. Real push/email provider integration — still stubs, unchanged from
   Session 4.
3. No "undo" for a dismissed/expired approval, by design (§17 doesn't
   call for one) — flagged in the frontend doc, not a gap to close here.

### Recommended next steps
1. Merge/review whenever the user is ready — six open branches now
   (`feat/backend-foundation`, `feat/contacts-import-dedup`,
   `feat/redis-infra`, `feat/reminders`, `feat/reminder-scheduler`,
   `feat/whatsapp-approvals`), none merged to `main`. Note:
   `feat/redis-infra` is still a sibling branch off `backend-foundation`
   only — it was never folded into the reminders/scheduler/approvals
   line, by deliberate choice (see Session 2/3 notes), so it needs its
   own separate merge.
2. With auth, contacts, reminders, scheduler, and WhatsApp approvals all
   in place, the backend now covers the core "remember and reach out"
   loop end-to-end (push/email still log-only, WhatsApp hands off to a
   real deep link). Remaining architecture-doc phases not yet started:
   PayPal payments (§23/§24), S3 object storage (§26, noting the team's
   earlier Cloudflare-instead-of-AWS decision), GDPR export/account
   deletion (§27).
