# Production Release Security Checklist

Run through this before every production deployment. It verifies that a **specific deploy** is configured correctly; it does not explain *why* any of these controls exist — that is [SECURITY.md](SECURITY.md).

Every item below is enforced or provided by code in this repository. If an item cannot be checked, the deploy is not ready.

---

## 1. Environment & Configuration

- [ ] `ENVIRONMENT=production` is set. Everything else in this section derives from it, and a typo (`prod`, `Production`) fails startup rather than silently falling back to development.
- [ ] `DEBUG` is `False` — derived automatically from `ENVIRONMENT`, so confirm by checking `ENVIRONMENT`, not by setting `DEBUG` directly.
- [ ] `SECRET_KEY` is set to a long, random, deployment-specific value, and has never been committed. It has **no fallback**: a missing value fails startup.
- [ ] `OTP_PEPPER_KEY` is set, or its fallback to `SECRET_KEY` is a deliberate choice. It is **optional by design** — leaving it blank keys OTP hashing off `SECRET_KEY`. Set it separately only if OTP hashes need to rotate independently of Django's signing key.
- [ ] `ALLOWED_HOSTS` lists the real hostnames this deploy serves. `core/settings_validation.py` refuses to boot in production on `'*'` or an empty list, so a successful start already proves this — confirm the hostnames are the *intended* ones.
- [ ] `FRONTEND_URL` points at the production frontend. Verification and password-reset emails build their links from it.
- [ ] Third-party credentials are populated for every feature this deploy is meant to serve. Each is optional at boot and fails lazily with a `503` from the specific endpoint that needed it, so a missing key is invisible until a user hits it:
  - [ ] `BREVO_API_KEY`, `BREVO_SENDER_EMAIL` (verified sender in Brevo), `BREVO_SENDER_NAME` — **required for any signup to complete**, since OTP delivery runs through it.
  - [ ] `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_OAUTH_REDIRECT_URI` (exactly matching the OAuth App's callback URL), `GITHUB_WEBHOOK_SECRET`, `GITHUB_WEBHOOK_BASE_URL` (publicly reachable).
  - [ ] `GOOGLE_CLIENT_ID` — the only Google value; there is deliberately no client secret.
  - [ ] At least one of `GROQ_API_KEY` / `GEMINI_API_KEY` / `OPENROUTER_API_KEY`, or AI features are unavailable.
- [ ] `COOKIE_DOMAIN` is set (leading dot, e.g. `.example.com`) **if** frontend and backend are on different subdomains of one domain. Without it, the frontend cannot read `csrftoken` and every mutating request fails with "CSRF token missing".

## 2. Network & Web Security

- [ ] `CORS_ALLOWED_ORIGINS` lists the production frontend origin(s) explicitly. No wildcard — the setting is parsed as a literal allowlist.
- [ ] `CSRF_TRUSTED_ORIGINS` matches. It is **derived from `CORS_ALLOWED_ORIGINS` in code**, not configured separately, so verify the CORS value and the two cannot drift.
- [ ] Cookies are correct for production. Set automatically from `ENVIRONMENT`; confirm rather than override:
  - [ ] `SESSION_COOKIE_SECURE = True`, `CSRF_COOKIE_SECURE = True`, `SECURE_SSL_REDIRECT = True`.
  - [ ] Access/refresh JWT cookies are `httpOnly`, `Secure`, `SameSite=None`. **`None`, not `Lax`** — frontend and backend are separate origins in production, and `Lax` would stop the browser sending auth cookies on cross-site requests. `Lax` is the *development* value.
  - [ ] `csrftoken` is **not** `httpOnly`. This is required: the double-submit pattern needs frontend JS to read it. Do not set `CSRF_COOKIE_HTTPONLY`.
- [ ] Security headers are live on a real production response (`curl -sI https://<host>/api/...`):
  - [ ] `Strict-Transport-Security: max-age=31536000; includeSubDomains` — **production only**, and only on requests served over HTTPS. `preload` is intentionally absent; `manage.py check --deploy` reports `security.W021` for this, which is expected and accepted.
  - [ ] `X-Content-Type-Options: nosniff`
  - [ ] `X-Frame-Options: DENY`
  - [ ] `Referrer-Policy: strict-origin-when-cross-origin`
  - [ ] `Content-Security-Policy` with `frame-ancestors 'none'` (from `core/middleware.py`; API responses get the strict `default-src 'none'` policy, HTML responses a same-origin one).
  - [ ] `Permissions-Policy` denying camera, geolocation, microphone, payment, USB, and the rest.
- [ ] TLS terminates in front of the app and HTTP redirects to HTTPS.

## 3. Authentication & Access Control

- [ ] Rate limiting is active on every auth endpoint — `login` (10/min), `register` (5/hour), `password_reset` (20/hour), `otp_verify` (20/hour), `otp_resend` (5/hour), plus blanket `anon` (60/min) / `user` (300/min).
- [ ] Composite **IP + target-email** throttling is live on OTP verification at `otp_verify_account` (10/hour), applied *alongside* the IP-keyed `otp_verify`, not instead of it.
- [ ] The cache backing those throttles is real and shared. Throttle counters live in Django's cache; with the default per-process `LocMemCache`, every gunicorn worker keeps its own counters and the effective limit multiplies by worker count. **Verify a shared backend (Redis) is configured, or treat the published rates as per-worker.**
- [ ] The Django admin site at `/admin/` is reachable only as intended (IP-restricted, or behind SSO / disabled) and no default or weak superuser account exists.
- [ ] Admin API endpoints at **`/api/admin/`** (the `adminapi` app — note the route is not `/adminapi/`) require `IsAuthenticated` **and** `IsAdminUser`.
- [ ] Object-level scoping is intact on user-owned querysets — `owner=request.user` for analyses, `analysis__owner=request.user` for chat, `integration=request.user.github_integration` / `repository__integration__user=request.user` for GitHub. Covered by the backend suite; re-verify after adding any endpoint.
- [ ] Password validators are active, including the Have I Been Pwned breach check. `PWNED_PASSWORDS_ENABLED` must be `True` in production (it is False only under `manage.py test`), and outbound HTTPS to `api.pwnedpasswords.com` must be reachable — if it is blocked, the check silently degrades to Django's local common-password list.

## 4. Data Protection & Secret Handling

- [ ] `DATABASE_URL_PROD` is injected by the platform's secret store, not baked into an image, a repo file, or a build arg.
- [ ] Database connections require TLS, and the database is not publicly reachable.
- [ ] `GITHUB_TOKEN_ENCRYPTION_KEY` is set to a real Fernet key, **distinct from `SECRET_KEY`** and from every other secret. Rotating it invalidates all stored GitHub tokens and forces affected users to reconnect — see the backlog note in `SECURITY.md` §5.2.
- [ ] No `.env` file is committed. `git log --all -- .env` returns nothing.
- [ ] Every secret in this checklist is different from the values used in development, staging, and CI.
- [ ] Structured logging (`core/logging_formatters.py`) is configured and log level is `INFO` or higher — not `DEBUG`.
- [ ] Spot-check recent production logs for leaked secrets: no passwords, OTP codes, JWTs, API keys, or `Authorization` header values. Log calls pass identifiers (user ID, email, request path) as context, never credentials.

## 5. Release Verification

Run from a clean checkout of the exact commit being deployed.

- [ ] `python manage.py check` — no issues.
- [ ] `python manage.py check --deploy` — reviewed. `security.W021` (HSTS preload) is the only expected warning.
- [ ] `python manage.py makemigrations --check --dry-run` — reports no changes, i.e. no model change is missing a migration.
- [ ] `python manage.py migrate` — applied against production, with a verified database backup taken **before** the run.
- [ ] `python manage.py test` — backend suite passes.
- [ ] `npm run lint && npm test && npm run build` — frontend clean.
- [ ] `pip-audit -r requirements.txt` and `npm audit --audit-level=high` — reviewed. Both run in CI as advisory (non-gating) steps, so read their output rather than trusting a green build; see `SECURITY.md` §5.1 for the accepted backlog.
- [ ] Frontend built with production `VITE_API_BASE_URL` and `VITE_GOOGLE_CLIENT_ID` — these are compiled in at build time and cannot be changed afterward without rebuilding.
- [ ] A Celery worker and Redis are running, or GitHub PR review will silently never execute.

## 6. Post-Deploy Smoke Test

- [ ] Register a throwaway account end to end: OTP email arrives, the code verifies, login succeeds.
- [ ] A mutating request from the real frontend succeeds — proves the CSRF cookie and CORS configuration actually work together in production.
- [ ] Token refresh works after the 15-minute access-token lifetime, or is verified by expiring one deliberately.
- [ ] Password reset delivers a link pointing at the production `FRONTEND_URL`, and completing it invalidates existing sessions.
- [ ] Security headers confirmed on a live response (section 2).
- [ ] Error responses return JSON, not Django's HTML debug page — the clearest single signal that `DEBUG=False` really took effect.
