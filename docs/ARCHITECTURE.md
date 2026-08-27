# Architecture

The technical architecture of Code Analyzer: a React single-page application backed by a Django REST Framework API. This document is about **how the system fits together**; the security rationale behind individual decisions lives in [SECURITY.md](SECURITY.md), and the pre-deploy verification steps in [SECURITY_CHECKLIST.md](SECURITY_CHECKLIST.md).

---

## 1. System Overview

Two independently deployed halves communicating exclusively over a JSON REST API:

- **Frontend** — React SPA built with Vite, served as static assets (Vercel in production; nginx in the Docker Compose stack).
- **Backend** — Django REST Framework API under gunicorn (Render), backed by PostgreSQL and Redis.

They are **separate origins at all times**, including local development (`localhost:5173` vs `localhost:8000`). Every cross-cutting concern — CORS, cookies, CSRF — is designed around that rather than assuming a shared origin. The one exception is the Docker Compose stack, where nginx reverse-proxies `/api/` to the backend so both appear on one origin.

```
┌──────────────────────┐      HTTPS / JSON       ┌───────────────────────────┐
│   React SPA (Vite)   │ ◄─────────────────────► │  Django REST Framework    │
│   Vercel / nginx     │    httpOnly JWT cookies │  gunicorn (Render)        │
└──────────────────────┘                         └─────────────┬─────────────┘
                                                               │
                        ┌──────────────────┬───────────────────┼────────────────────┐
                        │                  │                   │                    │
                 ┌──────▼──────┐   ┌───────▼───────┐   ┌───────▼────────┐  ┌────────▼─────────┐
                 │ PostgreSQL  │   │ Redis         │   │ Celery worker  │  │ External APIs    │
                 │ (Supabase)  │   │ broker/result │   │ (same image)   │  │ Brevo · Groq ·   │
                 └─────────────┘   └───────────────┘   └────────────────┘  │ Gemini · OpenR.  │
                                                                            │ Google · GitHub  │
                                                                            └──────────────────┘
```

The backend serves **no server-rendered pages** — it is a pure JSON API. Even unhandled exceptions are converted to JSON (`core/exceptions.py`) rather than falling back to Django's HTML error pages, since no client in the system consumes HTML. The exceptions are Django admin, DRF's browsable API, and the downloadable HTML analysis report, which is why the CSP middleware distinguishes HTML from JSON responses (§6).

### Third-party integration model

Every external HTTP integration follows the same hand-rolled pattern deliberately: a thin class wrapping `requests`, a dedicated exception type carrying `status_code`/`response_body`, structured logging on failure, and **lazy configuration validation** — a missing key raises `ImproperlyConfigured` on first real use, surfacing as a `503` from the endpoint that needed it, rather than preventing the whole app from booting. A deployment with no GitHub credentials still serves every other feature.

| Service | Purpose | Integration point | Sync/async |
|---|---|---|---|
| **Groq** | Primary LLM for suggestions, explanations, refactors, chat | `ai/client.py` | Synchronous, in-request |
| **Gemini** → **OpenRouter** | Fallback LLM providers, tried in that order | `ai/client.py` (`_call_with_fallback`) | Synchronous, in-request |
| **Brevo** | Transactional email — OTP codes, password-reset links | `accounts/brevo_client.py` (`POST /v3/smtp/email`) | Synchronous, in-request |
| **Google OAuth** | "Sign in with Google" | `accounts/google_auth.py` (`tokeninfo` + `userinfo`) | Synchronous, in-request |
| **GitHub REST API** | OAuth, repo browsing, PR review, webhooks, indexing | `github_integration/services/github_client.py` | Mixed — see below |

**On "asynchronous":** only GitHub webhook-driven PR analysis and repository indexing run off the request path, via Celery + Redis (`github_integration/tasks.py`). That is a hard requirement, not an optimization — GitHub retries a webhook delivery that isn't acknowledged within seconds, so the view persists a `WebhookEvent`, queues a task, and returns immediately.

AI and email calls are **synchronous and block the request**. The Groq→Gemini→OpenRouter chain means a slow provider can add its full timeout to a user-facing request before the next is tried, which is why nginx raises `proxy_read_timeout` to 120s for `/api/`.

## 2. Backend Architecture

**Stack:** Django 4.2, Django REST Framework, `djangorestframework-simplejwt`, Celery + Redis, WhiteNoise, `django-cors-headers`, PostgreSQL.

### 2.1 Core apps

| App | Responsibility |
|---|---|
| `accounts` | Registration, login/logout, OTP email verification, password reset and change, Google/GitHub OAuth login, profile and avatar management. |
| `analyses` | The code-analysis pipeline and the `Analysis` model — static analysis, sandboxed runtime checks, AI-assisted views, security scanning, report export. |
| `ai` | The floating chat assistant, plus `ai/client.py` — the single Groq/Gemini/OpenRouter fallback chain shared by every AI call site. |
| `chat` | Persisted per-analysis "chat about this code" conversations, distinct from the floating assistant. |
| `github_integration` | GitHub OAuth App integration: account linking, webhook-driven PR review, on-demand file analysis, dependency-graph indexing. |
| `adminapi` | Read-only cross-user admin endpoints, gated on `IsAdminUser`. |
| `core` | Cross-cutting infrastructure: throttle classes, security-headers middleware, the DRF exception handler, structured logging, settings validation, health check. |

**The analysis pipeline** (`analyses/engine.py`) runs `ast` + `pyflakes` for real syntax and undefined-name checks, plus textual checks for TODOs and long lines. For Python it additionally executes the submitted code under macOS's Seatbelt sandbox (`analyses/sandbox.py`) to catch runtime errors static analysis cannot predict — network denied, writes confined to a scratch directory, project and home directories unreadable. The sandbox is macOS-only and **self-disclosing**: `is_available()` returns `False` elsewhere, and that surfaces as a zero-penalty informational issue rather than degrading silently.

**Security Analysis Mode** (`analyses/security_views.py`, `analyses/services/`) is a deliberately separate pipeline — Bandit plus a custom rules engine, AI-enriched remediation, cached in its own `Analysis.security_report` field rather than mixed into `issues`.

### 2.2 Data models

```
User (stock django.contrib.auth)
 └─1:1─ Profile              is_verified, avatar, google_id, github_id,
                             otp_code_hash, otp_expires_at, otp_attempts
 └─1:N─ Analysis             source_code, language, issues, quality_score,
        │                    ai_suggestions/explanation/refactored_code,
        │                    security_report, repo_context, status
        └─1:1─ Conversation ──1:N── ChatMessage
 └─1:1─ GitHubIntegration     encrypted access_token, token_invalid
        └─1:N─ GitHubRepository
               ├─1:N─ PullRequestAnalysis ──1:N── FileAnalysis
               ├─1:1─ RepositoryIndex ──1:N── RepositoryFileNode
               └─1:N─ WebhookEvent
```

- **`User`** is Django's stock model — there is no custom user model. `email` is the effective login identifier (validated unique case-insensitively in serializers); `username` is synthesised from the email's local part purely to satisfy Django's non-null unique constraint.
- **`Profile`** (one-to-one, auto-created by a `post_save` signal) holds everything stock `User` doesn't model, including **all registration-OTP state**: `otp_code_hash`, `otp_expires_at`, `otp_attempts`. Keeping these on a row rather than in a cache is what makes attempt-lockout survive process restarts and be enforceable under a row lock (§3.1).
- **`Analysis`** is the hub: one row per pasted, uploaded, or GitHub-file analysis. AI output is cached on the row, so repeat views cost nothing until `?regenerate=true`.
- **`WebhookEvent`** is a durable log of every delivery, used to de-duplicate GitHub's retries.

### 2.3 Authentication & cookie architecture

JWTs are minted by `rest_framework_simplejwt` and moved into **httpOnly cookies** by `accounts/cookies.py` — the frontend never sees a raw token, so an XSS bug cannot read one out of `localStorage`.

| | Access token | Refresh token |
|---|---|---|
| Lifetime | 15 minutes | 7 days, rotated + blacklisted on use |
| Cookie | `access_token`, path `/` | `refresh_token`, path `/api/auth/` |
| Flags | `httpOnly`, `Secure` (prod) | `httpOnly`, `Secure` (prod) |

`SameSite` differs by environment, and the production value is the non-obvious one:

- **Production: `SameSite=None; Secure`.** Frontend and backend are genuinely different origins, and `Lax` would stop the browser attaching auth cookies to cross-site requests. `None` requires `Secure`, which is why it cannot be used in plain-http development.
- **Development: `SameSite=Lax`.** Sufficient there, since `localhost:5173` and `localhost:8000` differ only by port and are the same site.

`accounts/authentication.py`'s `CookieJWTAuthentication` is the sole default authentication class. It reads an `Authorization: Bearer` header when present (non-browser clients), otherwise the cookie — and **enforces Django's CSRF double-submit check whenever the cookie is the source**, reusing DRF's own `CSRFCheck`. A bearer header cannot be attached cross-origin by another site, so header-sourced auth needs no CSRF check; a cookie is attached automatically, so it does.

The double-submit contract: the frontend calls `GET /api/auth/csrf/` once on boot to obtain the **deliberately non-httpOnly** `csrftoken` cookie, reads it with JS, and echoes it as `X-CSRFToken` on every mutating request. This requires frontend and backend to share a registrable domain (`app.example.com` / `api.example.com`), configured via `COOKIE_DOMAIN` — cookies are not readable across unrelated domains.

## 3. Authentication Flows

Whichever method is used, the **outcome is identical**: an access/refresh pair minted and placed in cookies by the same `set_auth_cookies()` helper.

### 3.1 Email + OTP registration

`POST /api/auth/register/` creates the `User` row immediately but with **`is_active=False`**. That single flag gates login on its own — `EmailLoginSerializer` already rejects inactive users with a generic "no active account found", so no login-side change was needed.

1. `RegisterSerializer` validates email uniqueness and password strength, then creates the inactive `User`.
2. `accounts/otp.py` generates a 6-digit code with `secrets.randbelow(1_000_000)` — cryptographically secure, not `random`.
3. It stores an **HMAC-SHA256 of the code**, keyed by `OTP_PEPPER_KEY` (falling back to `SECRET_KEY`), plus a 10-minute expiry. The key, not the digest, is what matters: with only 10⁶ possible codes a bare SHA-256 column is exhaustively reversible from a database dump in under a second, so the pepper is what makes the dump alone insufficient. The plaintext is returned to the caller and never persisted.
4. `accounts/emails.py` sends it via `BrevoClient`. **Steps 1–4 run in one `transaction.atomic()` block** — if Brevo fails, the `User` is rolled back and the client gets a `503`, rather than stranding an account whose code was never delivered.
5. `POST /api/auth/verify-email/` verifies `{email, code}`. The entire read-check-write cycle runs inside a transaction with the `Profile` row locked via **`select_for_update()`**. Without that lock the attempt counter is a lost-update race — N concurrent requests all read the same `otp_attempts` and all write back the same value + 1, so N parallel guesses cost one attempt and the 5-attempt lockout is bypassed outright.
6. Comparison uses `hmac.compare_digest`, not `==`. On success: `is_active=True`, `is_verified=True`, OTP fields cleared.
7. `POST /api/auth/resend-verification/` re-issues a fresh code and resets the counter, returning an identical response whether or not the account exists.

Verifying does **not** log the user in — they proceed to `/login`.

### 3.2 OAuth login (Google & GitHub)

Both paths verify identity **server-side against the provider's own API on every login**; the frontend is never trusted to assert who a user is. In both, auto-linking to an existing account by email happens **only when the provider reports that email as verified**, and a resolved account already linked to a *different* provider identity is rejected rather than silently overwritten. OAuth signups set `is_verified=True` and bypass OTP entirely.

**Google** — classic OAuth popup flow (`useGoogleLogin`, implicit grant), chosen over Google Identity Services' rendered button because GIS silently auto-selects an already-signed-in account instead of showing the chooser. The frontend obtains an **access token** (not an ID token) and POSTs it to `/api/auth/google/`; the backend calls Google's `tokeninfo` to confirm the `aud` claim matches this app's `GOOGLE_CLIENT_ID` — without which a token minted for an unrelated project could be replayed here — then `userinfo` for the profile claims.

**GitHub** — standard Authorization Code redirect, sharing one OAuth App and callback URL with the repo-connect integration (GitHub allows only one registered callback), disambiguated by a `purpose` field inside a Django-signed, 10-minute `state` payload.

For the login path specifically, `state` also carries a **nonce bound to the initiating browser** via a short-lived httpOnly cookie, checked at the callback. A valid signature alone only proves *we* issued the `state`, not that the browser completing the callback is the one that started the flow — without the nonce, an attacker could complete their own authorization, capture the resulting `code`/`state`, and hand that URL to a victim, silently logging the victim into the **attacker's** account. That is the OAuth login-CSRF class of bug.

The login flow requests a deliberately narrower scope (`read:user user:email`) than repo-connect (`repo admin:repo_hook read:user user:email`), since scope is a per-request parameter — a user who just wants to log in never sees a repo-access consent screen.

### 3.3 Password reset, change, and session revocation

`POST /api/auth/forgot-password/` returns the same response whether or not the account exists, and when it does, emails a link built with Django's `default_token_generator` (signed, time-limited, no DB storage) through Brevo. `POST /api/auth/reset-password/` validates the `uid`/`token` pair and sets the new password.

**Both reset and change revoke every refresh token the account holds** — `accounts/tokens.py`'s `revoke_all_refresh_tokens()` blacklists each of the user's `OutstandingToken` rows. A password change is how a user boots an attacker or a lost device out of their account, and that only means something if the other session's refresh token stops being accepted.

The two paths diverge afterward:

- **Reset** issues nothing back. The endpoint is unauthenticated and the user proceeds to log in.
- **Change** immediately mints a fresh pair for the calling browser. Revoking everything and returning nothing would leave the caller with a valid access token for up to 15 minutes and then log them out silently, minutes after an action that appeared to succeed.

Already-issued access tokens stay valid until they expire; revocation acts on refresh tokens, which is what bounds a hostile session to minutes rather than a week.

## 4. Frontend Architecture

**Stack:** React + Vite, `react-router-dom`, no external state library. Auth and theme state live in React Context.

### 4.1 Pages and routing

Routes are declared centrally in `src/App.jsx` and composed with layout and guard wrappers rather than per-page checks:

| Route group | Wrapper | Purpose |
|---|---|---|
| `/`, `/login`, `/register` | `MarketingLayout` + `GuestRoute` | Public; redirects an authenticated user away. |
| `/verify-email`, `/reset-password` | *(none)* | Part of the auth flow itself; reachable either way. |
| `/dashboard`, `/analyze`, `/history`, `/report/:id`, `/github/*` | `ProtectedRoute` → `UserRoute` | Authenticated session required. |
| `/settings` | `ProtectedRoute` | Authenticated, any role. |
| `/admin` | `ProtectedRoute` → `AdminRoute` | Authenticated **and** staff. |

Guards live in `src/components/ProtectedRoute.jsx` and read from `AuthContext`. They are a **UX affordance, not a security boundary** — every protected route's data comes from an endpoint that independently enforces authentication and ownership.

### 4.2 State management

- **`lib/AuthContext.jsx`** — single source of truth for the current user, admin status, and auth actions. On mount it primes the CSRF cookie and attempts to fetch the profile. Because JWTs are httpOnly, this fetch-and-see is the *only* way the frontend can learn whether a session exists.
- **`lib/ThemeContext.jsx`** — light/dark, persisted independently of auth.
- No global fetch cache (no React Query/SWR); pages call resource functions and hold their own loading/error state.

### 4.3 HTTP client

`lib/api.js` exports one `apiFetch` wrapper used by every network call:

- Always sends `credentials: 'include'` so httpOnly cookies are attached.
- Reads the non-httpOnly `csrftoken` cookie and attaches it as `X-CSRFToken` on mutating requests. `primeCsrf()` fetches it once on boot; without it every POST/PATCH/DELETE fails with "CSRF token missing".
- Wraps network-level failures (offline, DNS, CORS rejection) into a typed `ApiError` with `status: 0`, so callers can distinguish "never reached the server" from a real HTTP status.
- On a `401`, transparently calls `/auth/refresh/` once and retries the original request.

`lib/resources.js` is the **single place every backend endpoint is called from** — one exported function per endpoint. Pages never call `apiFetch` directly; new endpoints go here.

### 4.4 Validation mirroring

`lib/validation.js` mirrors the rules in `backend/accounts/validators.py` — name, username, email, password composition, OTP format — so the user gets inline feedback without a round trip. **The two must be kept in sync when either changes.** The frontend copy is purely a convenience: the backend validates independently and is the only enforcement that counts. The backend additionally applies checks the frontend cannot, notably Django's validators and the Have I Been Pwned breach lookup, so a password can pass client-side and still be rejected.

## 5. Request Pipeline & Security

### 5.1 Middleware order

Order matters; the list is in `config/settings.py`:

| # | Middleware | Role |
|---|---|---|
| 1 | `core.middleware.SecurityHeadersMiddleware` | CSP + Permissions-Policy |
| 2 | `django.middleware.security.SecurityMiddleware` | HSTS, nosniff, referrer policy, SSL redirect |
| 3 | `whitenoise.middleware.WhiteNoiseMiddleware` | Serves collected static files |
| 4 | `corsheaders.middleware.CorsMiddleware` | CORS headers |
| 5–8 | Session, Common, CSRF, Authentication, Messages | Django defaults |
| 9 | `django.middleware.clickjacking.XFrameOptionsMiddleware` | `X-Frame-Options: DENY` |

`SecurityHeadersMiddleware` is **outermost deliberately**. Response phases run in reverse, so being first means it runs *last* and sees every response — including ones short-circuited before reaching a view: SSL/`APPEND_SLASH` redirects, 404s, CSRF rejections, WhiteNoise static hits. Placed last it would miss all of them, and those responses would ship with no CSP.

It emits one of two policies, chosen by response content type, because Django 4.2 has no CSP setting:

- **Non-HTML** (every `/api/` response) → `default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'`. A JSON document loads no subresources, so the strictest policy costs nothing.
- **HTML** (Django admin, DRF browsable API, the analysis report) → a same-origin policy with `style-src`/`script-src 'unsafe-inline'`, which those three genuinely need. `frame-ancestors 'none'` still applies.

`frame-ancestors 'none'` is why this must be a header: browsers ignore that directive in a `<meta>` tag, so the frontend's build-time CSP (`vite.config.js`) cannot express it. It intentionally duplicates `X-Frame-Options: DENY`.

**HSTS is production-only** (`max-age=31536000; includeSubDomains`, no `preload`). `SecurityMiddleware` emits it on any request it considers secure — including an https dev server or a TLS-terminating tunnel in front of `runserver` — and the header is sticky, so an unscoped setting can pin a developer's `localhost` to https for a year.

### 5.2 Rate limiting

Two independent systems, deliberately kept separate:

**DRF throttle scopes** (`core/throttling.py`) — named `ScopedIdentityRateThrottle` subclasses, keyed by user ID when authenticated and IP otherwise. Built on `SimpleRateThrottle` rather than DRF's `ScopedRateThrottle`, which reads its scope from a *view* attribute that is easy to forget and then silently never throttles.

| Scope | Rate | Applies to |
|---|---|---|
| `login` | 10/min | Email/password and OAuth login |
| `register` | 5/hour | Registration |
| `password_reset` | 20/hour | Forgot/reset password |
| `otp_verify` | 20/hour | OTP verification, keyed by IP |
| `otp_verify_account` | 10/hour | OTP verification, keyed by IP **+ target email** |
| `otp_resend` | 5/hour | OTP resend (each costs a real Brevo send) |
| `ai` | 30/min | AI suggestion/explanation/refactor/chat |
| `analysis_create` | 30/min | New analyses |
| `anon` / `user` | 60/min / 300/min | Blanket defaults on every endpoint |

The OTP endpoint carries **both** OTP throttles, because neither key subsumes the other: the IP bucket bounds one source's total volume, while the IP+email bucket bounds how much of it can be aimed at a single account. Neither is the primary defense — that is the per-code expiry and attempt lockout in §3.1.

> **Operational caveat:** throttle counters live in Django's cache, and there is **no `CACHES` setting**, so the default per-process `LocMemCache` applies. Under multiple gunicorn workers each keeps its own counters and the effective limit is roughly `rate × worker_count`. Configure a shared Redis cache to make the published rates real.

**Domain-specific daily quotas** — chat messages (`chat/rate_limit.py`) and GitHub file-check quotas are derived live from timestamped rows, not counters, and reset at the **user's own local midnight**. The client reports `tz_offset_minutes` on quota-relevant requests, since the server has no other way to know its timezone.

### 5.3 User-scoped querysets

Every object-owning endpoint scopes its queryset to the requesting user. The predicate varies with how far the object sits from its owner, but is always derived from `request.user` and never from a client-supplied ID:

| Area | Predicate |
|---|---|
| Analyses — detail, delete, re-analyze, cancel, status, search, reports, AI views, security scan | `owner=request.user` |
| Dashboard aggregates | `Analysis.objects.filter(owner=request.user)` before aggregation |
| Chat conversations, history, send | `analysis__owner=request.user` |
| Chat daily quota | `conversation__analysis__owner=user` |
| GitHub repositories and files | `integration=request.user.github_integration` |
| GitHub PR analyses, metrics, trends | `repository__integration__user=request.user` |
| Profile, avatar, delete account | operates on `request.user` directly |

Analyses created by the GitHub file-check flow are stamped `owner=user` at creation, inheriting the same scoping downstream. `adminapi` is the sole deliberate exception — gated on `IsAuthenticated` **and** `IsAdminUser`, it exists to read across accounts. DRF's global default permission is `IsAuthenticated`, so a view that forgets a permission class fails closed.

**When adding an endpoint, the queryset scope is the thing to get right.** Nothing in the framework enforces it; it is a convention held by review and tests.

### 5.4 Startup configuration validation

`config/settings.py` reads one `.env`, and `ENVIRONMENT` (`development` | `production`, validated against a fixed set) derives `DEBUG`, the active `DATABASE_URL_*`, cookie flags, and production hardening. A typo like `prod` fails startup rather than silently running as development.

`core/settings_validation.py` enforces the `ALLOWED_HOSTS` allowlist: in production it refuses to boot on `'*'` (which disables Host-header validation entirely, opening Host-header poisoning of the password-reset links built from it) or on an empty list (which under `DEBUG=False` rejects every request, producing a healthy-looking process that 400s all traffic). Development is left alone so no local configuration is required.

`_RUNNING_TESTS` (`'test' in sys.argv`) separately relaxes throttle rates, quiets logging, and disables the Have I Been Pwned network lookup during `manage.py test`.
