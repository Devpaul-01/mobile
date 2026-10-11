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
