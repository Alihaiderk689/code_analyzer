# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A full-stack code analysis platform (Django REST Framework backend + React/Vite frontend): paste/upload code or connect a GitHub repo for static analysis, sandboxed runtime-error detection, AI-powered explanations/refactors/chat, and security scanning — plus automatic AI code review on every pull request. See `README.md` for the full feature list and environment variable reference.

## Commands

### Docker
`docker compose up --build` (repo root, after `cp .env.example .env` + filling in `SECRET_KEY`) runs the whole stack — Postgres, Redis, backend (gunicorn), a Celery worker, and the frontend (nginx, serving the built SPA and reverse-proxying `/api/`, `/admin/`, `/static/` to the backend, and `/media/` directly from a volume shared with backend) — at http://localhost. The backend container has **no port published to the host at all** — nginx is the only way in, including the GitHub OAuth callback (`GITHUB_OAUTH_REDIRECT_URI` goes through nginx, not a direct backend port). No `celery_beat` service: this app has no scheduled/periodic tasks. `backend/Dockerfile`'s `entrypoint.sh` only runs `migrate`/`collectstatic` for the gunicorn command (not the Celery worker, which reuses the same image), so two containers starting together can't race the same migration. See root `.env.example` for the Docker-specific env vars (`POSTGRES_*`, `FRONTEND_PORT`) — distinct from `backend/.env.example`, which is for running the backend directly on the host.

### Backend (`backend/`)
```bash
python -m venv venv && source venv/bin/activate && pip install -r requirements.txt
cp .env.example .env            # fill in SECRET_KEY, DATABASE_URL_DEV, etc.
python manage.py migrate
python manage.py runserver

python manage.py test                                    # full suite
python manage.py test accounts                           # one app
python manage.py test accounts.tests.LoginTests           # one class
python manage.py test accounts.tests.LoginTests.test_login_wrong_password_rejected  # one test
python manage.py check                                    # Django system check
python manage.py makemigrations --check --dry-run         # verify no missing migrations
```
GitHub PR review needs a Celery worker + Redis: `celery -A config worker --loglevel=info`.

### Frontend (`frontend/`)
```bash
npm install
npm run dev            # vite dev server
npm run build           # production build (vite build)
npm run lint            # oxlint
npm test                # vitest run (see note below)
npx vitest run src/lib/api.test.js   # one file
```
**Note:** `vitest.config.js` only runs `src/**/*.test.js` under a plain Node environment (no DOM/JSX) — it exercises pure-JS lib modules (`api.js`, `format.js`) only. There is no component/page-level test setup in this repo.

### CI
Two workflows. `.github/workflows/ci.yml` is the gate — backend (`check` + `makemigrations --check` + `test` against real Postgres) and frontend (`lint`, `test`, `build`) as parallel jobs, plus advisory `pip-audit`/`npm audit`. It triggers on PRs to `main`/`dev` and declares `workflow_call:`, which is what makes it importable. `.github/workflows/deploy.yml` triggers on push to `main`, runs `uses: ./.github/workflows/ci.yml` as its `ci` job, and a `deploy` job with `needs: ci` — so there is exactly one definition of "the tests" and the deploy path cannot drift from what a PR was checked against. `ci.yml` deliberately has no `push:` trigger: `deploy.yml` calls it, and adding one would run every job twice for the same commit. A commit pushed straight to `dev` with no PR is therefore not tested.

## Architecture

### Two independent origins, always
The frontend (Vite SPA) and backend (DRF API under `/api/`) are separate origins even in local dev (`:5173` vs `:8000`). Every cross-cutting concern (CORS, cookies, CSRF) is built around that.

### Auth: httpOnly cookies + CSRF double-submit
JWT access/refresh tokens live in httpOnly cookies (`accounts/cookies.py`), not `localStorage` — frontend JS never sees the raw tokens.
- `accounts/authentication.py`'s `CookieJWTAuthentication` (the sole `DEFAULT_AUTHENTICATION_CLASSES` entry) reads an `Authorization: Bearer` header if present (non-browser clients), otherwise the `access_token` cookie. **Cookie-sourced auth enforces CSRF** on unsafe methods (reusing DRF's own `CSRFCheck`); header-sourced auth doesn't need it.
- The frontend must call `GET /api/auth/csrf/` once on boot (`AuthContext.jsx` does this via `primeCsrf()` in `api.js`) to get the JS-readable `csrftoken` cookie before any mutating request — otherwise every POST/PATCH/DELETE 403s with "CSRF token missing."
- `POST /api/auth/refresh/` reads the refresh token from its cookie only, never the request body.
- `AUTH_COOKIE_SECURE`/`AUTH_COOKIE_SAMESITE` in `config/settings.py` are `Lax`+non-secure in dev, `None`+`Secure` in production. **This whole scheme requires frontend and backend to share a registrable domain** (e.g. `app.example.com` / `api.example.com`) — cookies aren't JS-readable across genuinely different domains, which the CSRF double-submit pattern depends on.
- Every object-owning endpoint scopes its queryset by the requesting user (`owner=request.user`, `integration__user=request.user`, etc.) — this pattern is consistent across `accounts`, `analyses`, `chat`, `adminapi`, and `github_integration`; keep it that way for any new endpoint.

### Settings: one `.env`, `ENVIRONMENT` drives everything
`config/settings.py` reads a single `.env` file; `ENVIRONMENT` (`development` or `production`, validated against a fixed set) derives `DEBUG`, which `DATABASE_URL_*` is used, cookie `Secure`/`SameSite` flags, and whether HSTS/secure-cookie production hardening is applied. `_RUNNING_TESTS` (`'test' in sys.argv`) separately relaxes throttle rates and log verbosity during `manage.py test` — real rates would make unrelated tests trip shared IP-keyed throttle buckets, since Django's test client reuses one `REMOTE_ADDR` and `LocMemCache` persists for the whole test run.

### Static files: WhiteNoise; media: nginx, not Django
`django.contrib.staticfiles` only auto-serves under `runserver`/`DEBUG=True`, and this app has no cloud storage (S3/Cloudinary/etc.) or separate static-file server in front of it — so `STATIC_ROOT` + WhiteNoise (`whitenoise.middleware.WhiteNoiseMiddleware`, right after `SecurityMiddleware`) serve collected static files (admin CSS/JS) directly from gunicorn in every environment; nginx just proxies `/static/` to it. Media (user-uploaded avatars) is different: `config/urls.py`'s media route *is* `DEBUG`-gated, same as Django's default, since it's dev-only/inefficient by Django's own docs — in Docker Compose, nginx serves `/media/` directly from a `media_data` volume shared with the backend container instead (see `frontend/nginx.conf`), never through Django at all.

### AI: one client, three call sites
`ai/client.py` wraps the Groq SDK (`generate_text`, `generate_chat_reply`) and is the only place that talks to Groq. It's shared by three otherwise-unrelated call sites, all of which catch AI-call exceptions the same way (503 "AI service is currently unavailable"): the floating chat (`ai/views.py`), per-analysis suggestions/explanation/refactor (`analyses/ai_views.py`), and per-analysis persisted chat (`chat/views.py`). Prompt-building for the chat surfaces is centralized in `ai/prompts.py` so the floating and per-analysis chats describe an `Analysis` identically.

### Analysis engine + sandbox
`analyses/engine.py` does static analysis (`ast` + `pyflakes` for real syntax/undefined-name/unused-import checks, plus generic textual checks for TODOs/long lines/comments). For Python, it also runs `analyses/sandbox.py`, which executes the submitted code under macOS's Seatbelt sandbox (`sandbox-exec`) to catch runtime errors static analysis can't predict — network denied, filesystem writes confined to a scratch dir, project directory and the invoking user's home directory both denied for reads. It's macOS-only and self-disclosing: `is_available()` returns `False` on any other host, and that "unavailable" case surfaces as a zero-quality-score-impact informational issue (`runtime_check_unavailable` in `ISSUE_PENALTIES`) rather than degrading silently. `_run_analysis()` in `analyses/analysis_views.py` catches any unexpected engine failure and marks the `Analysis` row `FAILED` rather than leaving it stuck in `PENDING`/`RUNNING` or 500ing.

### Security scanning is a separate pipeline
"Security Analysis Mode" (`analyses/security_views.py`, `analyses/services/`) is deliberately independent of the main analyze/AI-suggestions flow: Bandit + a custom rules engine (`bandit_service.py`, `custom_rules_service.py`) produce findings, `ai_security_service.py` enriches them with AI-written explanations/remediation, and `security_service.py` aggregates — cached in its own `Analysis.security_report` field, not mixed into `issues`.

### Two different rate-limiting systems — don't confuse them
1. **DRF throttle scopes** (`core/throttling.py`) — per-endpoint request-rate limits (login, register, password reset, AI calls, analysis creation), keyed by user id or IP. Named `ScopedIdentityRateThrottle` subclasses, not DRF's built-in `ScopedRateThrottle` (that reads its scope from a `throttle_scope` attribute on the *view*, which is easy to forget and then silently never throttles).
2. **Domain-specific daily quotas** — e.g. `chat/rate_limit.py` (chat messages) and the GitHub file-check quota. These reset at the *user's own local midnight*, not a server-time or rolling window: the client reports its UTC offset (`tz_offset_minutes`, `Date.getTimezoneOffset()` convention) on every quota-relevant request, since the server has no other way to know the user's timezone.

### Errors are always JSON
`core/exceptions.py`'s `custom_exception_handler` (the DRF `EXCEPTION_HANDLER`) ensures every unhandled exception still returns clean JSON with a traceback logged server-side, instead of Django's HTML 500 page — important since this is a pure JSON API with no server-rendered pages to fall back to.

### GitHub integration pipeline
OAuth App (not a GitHub App) → webhook delivery (HMAC-verified via `X-Hub-Signature-256`, `github_integration/services/signature.py`) → acknowledged immediately, actual review work queued as a Celery task (`github_integration/tasks.py`) since webhooks must be ack'd in seconds → `PRAnalysisService` analyzes changed files → posts a scored review comment on the PR. Stored GitHub access tokens are Fernet-encrypted at rest (`github_integration/services/encryption.py`, key from `GITHUB_TOKEN_ENCRYPTION_KEY`), validated lazily so the app still boots without GitHub configured.

### Structured logging
`core/logging_formatters.py`'s `StructuredFormatter` appends any `extra={...}` passed to a `logger.info(...)`/`logger.warning(...)` call as trailing JSON — without it, `extra` fields are silently dropped by Python's default formatter. Used extensively in `github_integration` (OAuth, webhooks, Celery jobs) and worth using for any new structured log call.

### Frontend API layer
`frontend/src/lib/api.js` (`apiFetch`) is the single fetch wrapper — handles the CSRF header, cookie credentials, network-failure-to-`ApiError` wrapping, and automatic refresh-and-retry on 401. `frontend/src/lib/resources.js` is the single place every backend endpoint is called from; add new endpoints there rather than calling `apiFetch` directly from a page. `frontend/src/lib/validation.js` mirrors the validation rules in `backend/accounts/validators.py` — keep both in sync when changing password/name/email/username rules.
