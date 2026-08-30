# Security Policy

This document describes how to report a vulnerability in Code Analyzer, and summarizes the security-relevant design decisions currently implemented in the codebase.

## 1. Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it privately rather than opening a public issue.

- **Contact**: `security@codeanalyzer.example` *(placeholder — replace with a real, monitored address before this policy is published)*
- **Alternative**: `<maintainer GitHub handle>` *(placeholder — add a direct contact if no dedicated security inbox exists yet)*

When reporting, please include:

- A description of the vulnerability and its potential impact.
- Steps to reproduce it (a minimal example is ideal).
- The affected component (frontend route, API endpoint, etc.) and, if known, the relevant file.

**Response targets** *(placeholder — adjust to match actual maintainer capacity)*:

| Stage | Target |
|---|---|
| Acknowledgment of report | Within 3 business days |
| Initial assessment (severity, affected scope) | Within 7 business days |
| Fix or mitigation, for confirmed high/critical issues | Best effort, prioritized over feature work |

Please do not test for vulnerabilities against production data or other users' accounts. Use a local development environment or your own test account.

## 2. Authentication & Authorization

### 2.1 Session model

Authentication is JWT-based (`djangorestframework-simplejwt`), but the tokens are **never exposed to frontend JavaScript**. Both the access token (15-minute lifetime) and refresh token (7-day lifetime, rotated and blacklisted-after-rotation on every use) are set as **httpOnly cookies** by the backend. This means an XSS bug in the frontend cannot exfiltrate a session token by reading `localStorage`/`document.cookie`, since the token is never placed anywhere JavaScript can reach it.

Completing a **password change or password reset revokes every refresh token the account holds** (`accounts/tokens.py`'s `revoke_all_refresh_tokens`, which blacklists each of the user's `OutstandingToken` rows). A password change is the standard way to boot an attacker — or a lost/forgotten device — out of an account, and that only means something if their still-unexpired refresh token stops being accepted. The two paths differ in what happens next: the reset endpoint is unauthenticated and issues nothing back (the user logs in again), whereas the change-password endpoint immediately mints a fresh pair for the calling browser, so the caller isn't silently logged out minutes later when their old access token expires. Access tokens already issued elsewhere remain valid for the remainder of their 15-minute lifetime — revocation acts on refresh tokens, which is what bounds a hostile session to minutes instead of a week.

Because the browser now attaches the cookie to requests automatically, cookie-sourced authentication additionally enforces Django's **CSRF double-submit check** (`accounts/authentication.py`) on every unsafe method. Header-sourced authentication (`Authorization: Bearer ...`, used by non-browser clients) does not need this, since no other origin can read or set an `Authorization` header for this API.

### 2.2 Registration OTP lifecycle

Registration creates the account immediately but **inactive** (`User.is_active = False`) until a 6-digit one-time code is verified — the account cannot be logged into at all until then, enforced by the same `is_active` check every login path already uses (`EmailLoginSerializer`).

- **Generation**: `secrets.randbelow(1_000_000)` (cryptographically secure, not `random`), zero-padded to 6 digits.
- **Storage**: only an **HMAC-SHA256** of the code is persisted (`Profile.otp_code_hash`), keyed by a server-side secret (`OTP_PEPPER_KEY`, falling back to `SECRET_KEY`) — the plaintext code exists only transiently, in memory, for the single call that emails it. The key matters more than the digest here: with only 10⁶ possible codes, a bare SHA-256 column is exhaustively reversible from a database dump in under a second, so the pepper is what makes a dump of the database *alone* insufficient to recover a usable code.
- **Expiry**: 10 minutes (`Profile.otp_expires_at`), checked server-side on every verification attempt.
- **Attempt lockout**: a persisted counter (`Profile.otp_attempts`) rejects further attempts against a code after **5 incorrect guesses**, independent of rate limiting (see §3.1) — this holds even if a single IP address distributes its guesses across many requests slowly enough to stay under the throttle.
- **Concurrency**: the whole read-check-increment cycle runs in one transaction with the `Profile` row locked (`SELECT ... FOR UPDATE`). Without the lock, the counter is a lost-update race: N simultaneous verification requests all read the same `otp_attempts`, all write back the same value + 1, and N guesses cost a single attempt — which is the lockout above being bypassed by nothing more sophisticated than firing the guesses in parallel. `accounts.tests.OtpConcurrencyTests` exercises this against a real database with concurrent threads.
- **Comparison**: `hmac.compare_digest`, not `==` (see §3.2).
- A resend re-issues a **new** code, hash, and expiry, and resets the attempt counter — the previous code stops being valid.
- Google and GitHub OAuth signups **bypass OTP entirely**, since both providers independently verify the account owner controls the associated email before this application ever sees the sign-in.

### 2.3 OAuth verification

Both third-party login paths verify identity **server-side against the provider's own API on every login** — the frontend is never trusted to assert who a user is.

- **Google**: the frontend obtains an OAuth **access token** (not an ID token) via the classic consent-popup flow. The backend independently calls Google's `tokeninfo` endpoint to confirm the token's `aud` claim matches this application's registered `GOOGLE_CLIENT_ID` (preventing an access token issued to an unrelated Google Cloud project from being replayed against this API), then calls Google's `userinfo` endpoint for the actual profile claims. Account auto-linking by email only occurs when Google reports `email_verified: true`.
- **GitHub**: a standard OAuth Authorization Code exchange. The redirect's `state` parameter is a Django-signed, time-limited payload (10-minute max age) — for the login path specifically, it is additionally bound to the **initiating browser** via a short-lived httpOnly nonce cookie, checked at the callback. Without this binding, an attacker could complete their own OAuth authorization, capture the resulting `code`/`state` before their own client consumes it, and hand that URL to a victim — whose browser would then be logged into the *attacker's* account (a login-CSRF class of vulnerability). Account auto-linking by email only occurs when GitHub reports the matched email as `verified: true` via its `/user/emails` endpoint.
- In both flows, if the resolved account's email is already linked to a *different* provider identity, the request is rejected rather than silently overwriting the link.

### 2.4 Authorization

- Every object-owning endpoint (analyses, chat conversations, GitHub integrations/repositories, profile) scopes its queryset to the requesting user — there is no endpoint that returns another user's data given only an object ID. The scoping predicate differs by how far the object sits from its owner, but is always present and always derived from `request.user`, never from a client-supplied ID:

  | Area | Scoping predicate |
  |---|---|
  | Analyses (detail, delete, re-analyze, cancel, status, search, reports, AI views, security scan) | `owner=request.user` |
  | Dashboard stats / language / score aggregates | `Analysis.objects.filter(owner=request.user)` before aggregation |
  | Chat conversations, history, message send | `analysis__owner=request.user` |
  | Chat daily quota | `conversation__analysis__owner=user` |
  | GitHub repositories (tree, file content, index, re-index, deselect, analyze) | `integration=request.user.github_integration` |
  | GitHub PR analyses + dashboard metrics/trends | `repository__integration__user=request.user` |
  | Profile / avatar / delete account | operates on `request.user` directly, never a path ID |

  Analyses created on a user's behalf by the GitHub file-check flow are stamped with `owner=user` at creation (`github_integration/repository_views.py`), so they inherit the same scoping everywhere downstream. The one place querysets are deliberately *not* user-scoped is `adminapi`, which is gated on `IsAuthenticated` **and** `IsAdminUser` and exists precisely to read across accounts.
- Admin endpoints (`adminapi`) require `IsAuthenticated` **and** `IsAdminUser` (`request.user.is_staff`); DRF's global default permission is `IsAuthenticated`, so any view that omits an explicit permission class fails closed (rejects anonymous requests) rather than failing open.
- GitHub access tokens are encrypted at rest with **Fernet** (`github_integration/services/encryption.py`) before being stored, using a key (`GITHUB_TOKEN_ENCRYPTION_KEY`) that is never derived from `SECRET_KEY` or any other in-code value.

### 2.5 Password policy

Every path that sets a password — registration, change-password, reset-password — runs the same two layers, so the rules can't drift between them:

1. **Composition rules** (`accounts/validators.py`'s `validate_password_strength`): 8–128 characters, with at least one uppercase letter, lowercase letter, digit, and non-alphanumeric character. `frontend/src/lib/validation.js` mirrors these for inline feedback; the frontend copy is a convenience, and the backend enforces independently.
2. **Django's `AUTH_PASSWORD_VALIDATORS`**: `UserAttributeSimilarityValidator`, `MinimumLengthValidator`, `CommonPasswordValidator`, `NumericPasswordValidator`, plus a **Have I Been Pwned breach check** (`accounts/password_validation.py`, wrapping `pwned-passwords-django`).

The breach check covers the wide gap between "obviously bad" and "actually unique": a password can satisfy every composition rule and every list above and still be sitting in an attacker's wordlist because it leaked from some other site. The lookup is **k-anonymous** — only the first 5 characters of the password's SHA-1 are sent to `api.pwnedpasswords.com`, never the password or its full hash, and the API returns a bucket of candidate suffixes matched locally. It has a 1-second timeout, and if the API can't be reached it **falls back to Django's own common-password list** rather than letting the password through unchecked.

`PWNED_PASSWORDS_ENABLED` turns the network call off; it is off under `manage.py test` only, so the suite doesn't put a live HTTPS request on the path of every test that sets a password. The validator itself stays in `AUTH_PASSWORD_VALIDATORS` unconditionally and is covered by `accounts.tests.PwnedPasswordValidatorTests` with a stubbed API client.

## 3. Threat Mitigation

### 3.1 Brute-force / rate limiting

Two independent layers exist, deliberately kept separate:

1. **Per-endpoint request-rate throttling** (`core/throttling.py`, DRF `SimpleRateThrottle` subclasses keyed by user ID or IP), configured per scope in `settings.py`:

   | Scope | Rate | Applies to |
   |---|---|---|
   | `login` | 10/min | Email/password login |
   | `register` | 5/hour | Registration |
   | `password_reset` | 20/hour | Forgot-password requests |
   | `otp_verify` | 20/hour | OTP verification attempts, keyed by IP |
   | `otp_verify_account` | 10/hour | OTP verification attempts, keyed by IP **+ target email** |
   | `otp_resend` | 5/hour | OTP resend requests |
   | `ai` | 30/min | AI suggestion/explanation/refactor/chat calls |
   | `analysis_create` | 30/min | New analysis submissions |
   | `anon` / `user` | 60/min / 300/min | Blanket defaults applied to every endpoint |

   The OTP-check endpoint carries **two** throttles, applied together, because neither key subsumes the other:

   - `otp_verify` (IP) bounds the total volume one source can send across every account it touches.
   - `otp_verify_account` (IP + target email, the email hashed before it becomes a cache key) bounds how much of that volume can be aimed at *one* account. Without it, a single IP could spend its entire hourly allowance guessing at one victim's code. The rate sits above `OTP_MAX_ATTEMPTS` (5) rather than equal to it so that a user who mistypes a code, requests a resend, and mistypes again is not throttled out of their own signup.

   Neither is the primary defense against guessing a 6-digit code — that role belongs to the per-account expiry and attempt lockout described in §2.2, since an IP-keyed limit alone cannot stop a distributed or slow-and-low guessing attempt.

   **Known limitation — shared-IP collateral throttling.** Both throttles key on IP, so users behind one NAT (an office, a campus, carrier-grade NAT) draw on the same `otp_verify` budget. `otp_verify_account` reduces how much of that shared budget any single target can consume, but it does not remove the shared ceiling: enough simultaneous signups from one egress IP can still exhaust `otp_verify` and cause verification failures for unrelated users behind it. Raising the `otp_verify` rate is now safer than it was — the per-target cap bounds what the extra headroom could be aimed at — but that change has not been made, and this document does not claim the shared-IP case is solved.

2. **Domain-specific daily quotas** (e.g. chat messages, GitHub on-demand file checks) — derived live from timestamped database rows, not a separate counter, and reset at the *user's own local midnight* rather than server time, since the server has no other way to learn the client's timezone.

### 3.2 Timing attacks

Every secret-comparison in the authentication paths uses a constant-time comparison rather than a standard `==`/string-equality check:

- OTP code verification: `hmac.compare_digest(stored_hash, hash_of_submitted_code)`.
- The GitHub OAuth login-state nonce check (§2.3) uses the same `hmac.compare_digest` pattern.
- Password verification goes through Django's own `User.check_password`, which is already constant-time and salted (PBKDF2).

A naive `==` comparison on a secret value leaks timing information proportional to the number of matching leading characters, which can (in principle) let an attacker recover a secret significantly faster than brute-forcing the full keyspace; every comparison against a stored secret in this codebase is written to avoid that.

### 3.3 Enumeration prevention

Endpoints that could otherwise reveal whether a given email address has an account return **the same response regardless of outcome**:

- `POST /api/auth/forgot-password/` — identical `{"detail": "If an account with that email exists, a reset link has been sent."}` whether or not the account exists.
- `POST /api/auth/resend-verification/` — identical response whether the account doesn't exist, is already verified, or a new code was actually sent.
- `EmailLoginSerializer` — a nonexistent email, a wrong password, and an inactive (unverified) account all return the same generic `"No active account found with the given credentials."`, so a failed login attempt cannot be used to distinguish "wrong password" from "account doesn't exist."

### 3.4 Cross-Site Request Forgery (CSRF)

Handled via Django's double-submit cookie pattern: the frontend reads a JS-readable `csrftoken` cookie and echoes it back as an `X-CSRFToken` header on every mutating request; the backend validates the two match. This is enforced specifically for cookie-authenticated requests (§2.1) — bearer-token requests don't need it, since no cross-origin page can attach an `Authorization` header on this API's behalf. `CSRF_TRUSTED_ORIGINS` is derived directly from `CORS_ALLOWED_ORIGINS`, so the two never drift out of sync.

### 3.5 HTTP security headers

Set in every environment, so that CI and local development exercise the same header set that ships — except the two below, which are production-only: `SECURE_SSL_REDIRECT` (it would make plain-`http://` local development unreachable) and HSTS (see below the table).

| Header | Value | Source |
|---|---|---|
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` — **production only** | `SECURE_HSTS_*` |
| `X-Content-Type-Options` | `nosniff` | `SECURE_CONTENT_TYPE_NOSNIFF` |
| `X-Frame-Options` | `DENY` | `X_FRAME_OPTIONS` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | `SECURE_REFERRER_POLICY` |
| `Content-Security-Policy` | see below | `core/middleware.py` |
| `Permissions-Policy` | `accelerometer`, `autoplay`, `camera`, `display-capture`, `encrypted-media`, `fullscreen`, `geolocation`, `gyroscope`, `magnetometer`, `microphone`, `midi`, `payment`, `usb`, `xr-spatial-tracking` all set to `()` | `core/middleware.py` |

**HSTS is scoped to `ENVIRONMENT == 'production'`.** `SecurityMiddleware` emits it on any request it considers secure, which includes a local https dev server or a TLS-terminating tunnel (ngrok and similar) in front of `runserver` — and the header is sticky, so once a browser has cached a year of HSTS for `localhost`, every later plain-http project on that hostname is forced to https until the developer clears it manually. Scoping it keeps that failure mode out of development entirely.

`preload` is deliberately **not** set. Submitting a domain to the browser preload list bakes https-only into shipped browser binaries and removal takes months to propagate, so it belongs to a deliberate decision about one specific domain rather than being a default carried by every deployment of this codebase. `manage.py check --deploy` flags its absence as W021; that warning is knowingly accepted.

Django 4.2 has no setting for CSP or Permissions-Policy, so `core/middleware.py`'s `SecurityHeadersMiddleware` adds those. It is the **outermost** middleware, so its response phase runs last and covers requests short-circuited before they ever reach a view (redirects, 404s, CSRF rejections, WhiteNoise static hits). It sends one of two policies, chosen by response content type:

- **Non-HTML responses** — i.e. every `/api/` response, all of which are JSON (`core/exceptions.py` guarantees this even for unhandled errors) — get `default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'`. A JSON document loads no subresources, so the strictest possible policy costs nothing.
- **HTML responses** — Django admin, DRF's browsable API, and the analysis HTML report — get a same-origin policy with `style-src`/`script-src 'unsafe-inline'`, which those three genuinely require (admin ships an inline theme-toggle script; the report template carries an inline `<style>` block). `frame-ancestors 'none'`, `object-src 'none'` and a locked-down `base-uri`/`form-action` still apply.

`frame-ancestors 'none'` is the reason this has to be an HTTP header rather than a `<meta>` tag: browsers ignore that directive in `<meta>`. It intentionally duplicates `X-Frame-Options: DENY` — the two are redundant only in browsers that support both.

Separately, the **frontend** injects its own CSP `<meta>` tag at build time (`frontend/vite.config.js`), restricting script/style/connect/frame sources to `'self'` plus the specific third-party origins actually required (the deployed API origin, Google's Identity Services script/style/frame endpoints for the OAuth popup), with `object-src 'none'` and a locked-down `form-action`/`base-uri`. That tag governs the SPA's own document; the headers above govern everything the backend serves.

### 3.6 Cross-Origin Resource Sharing

`CORS_ALLOWED_ORIGINS` is an explicit allowlist (no wildcard), read from environment configuration, with `CORS_ALLOW_CREDENTIALS = True` required for cookies to flow on cross-origin requests between the frontend and API origins.

## 4. AI / LLM Trust Boundaries

Five endpoints call an AI provider: per-analysis Suggestions/Explanation/Refactor (`analyses/ai_views.py`), the floating assistant (`ai/views.ChatView`), the persisted "Chat with Your Code" panel (`chat/views.py`), and security-finding enrichment (`analyses/services/ai_security_service.py`, called from `SecurityAnalysisView`). All five route through `ai/client.py`'s `generate_text`/`generate_chat_reply`, which fan out to a three-provider fallback chain (Groq → Gemini → OpenRouter).

**The operating assumption for everything below is that the model itself can be fully compromised or manipulated** (via prompt injection in submitted source code, GitHub repository content, or PR data) **and the application must remain secure regardless.** Nothing in this section treats a prompt instruction as a security boundary — every control listed is enforced in code, not requested from the model.

### 4.1 Where each kind of control actually lives

| Layer | What it actually stops | Example |
|---|---|---|
| **Kernel-enforced** | Code execution escaping its sandbox | The Linux/macOS sandbox (`analyses/sandbox.py`, documented separately, not in this file) — but AI output is never passed to it at all (see §4.7); this row exists for completeness, not because AI output reaches it. |
| **Application-enforced** | What actually determines whether an unwanted outcome can happen | Ownership scoping on every AI endpoint; secret redaction before a provider request is built; output sanitization before a GitHub comment is posted; per-user/global AI concurrency limits; type/length validation on parsed AI responses. |
| **Provider-enforced** | Best-effort, outside this app's control | Each provider's own content-safety filtering, if any; API keys sent via headers, never query strings, specifically so they can't leak into this app's own exception-message logging. |
| **Model/prompt-enforced** | **Not a security boundary — informational only** | `UNTRUSTED_DATA_WARNING` and `wrap_untrusted()`'s `--- BEGIN/END ---` delimiters (`ai/prompts.py`), which ask the model not to treat submitted content as instructions. Treated throughout this codebase as a best-effort signal that reduces the *likelihood* of a successful injection, never as the reason a specific bad outcome can't happen — the rows above are what actually prevent it. |

### 4.2 Untrusted-content prompt handling

Every prompt-building call site wraps submitted content (source code, GitHub repository context, security-scanner code snippets) with `ai.prompts.wrap_untrusted(label, content)` before it enters a prompt, and every system/base instruction includes `ai.prompts.UNTRUSTED_DATA_WARNING` once. Untrusted content is kept in the **user**-role message, never concatenated into a system-role instruction — including in the two chat surfaces (`ai/views.ChatView`, `chat/views.SendMessageView`), where the analysis being discussed is passed as a separate `context` argument to `generate_chat_reply()` rather than appended to `system_instruction`. `system_instruction` is reserved exclusively for fixed, server-authored text.

As stated above, this delimiting is a hardening measure, not a guarantee — the controls in §4.3–§4.6 are what hold even if a delimiter is ever defeated.

### 4.3 Chat-history trust model

Replayed conversation history — whether client-supplied per-request (the floating assistant) or persisted per-analysis (the "Chat with Your Code" panel) — is treated as untrusted data, not verified conversation state:

- `generate_chat_reply()` restricts every history turn's role to `{'user', 'assistant'}`; a `'system'` role is dropped, never honored, regardless of what upstream validation allowed through (defense in depth on top of the request serializers, which already reject it).
- Each turn's content is wrapped with `ai.prompts.wrap_history_turn()` — the same untrusted-data framing fresh source code gets — **including a prior `assistant` turn**: if an earlier injection attempt ever partially shaped a reply, replaying it unmarked would let it compound turn over turn instead of being re-flagged every time.
- Non-string or empty turn content is dropped rather than passed through.
- The floating assistant's history is additionally per-request size-bounded (`ai/serializers.py`: 8,000 chars/entry, last 20 entries); the persisted chat's history is server-controlled (`HISTORY_LIMIT = 20` most recent `ChatMessage` rows).

### 4.4 Repository-secret redaction before any provider request

`ai/redaction.py`'s `redact_secrets()` runs inside `generate_text`/`generate_chat_reply` — the last point before a request leaves this application — over every piece of content reaching a provider call: prompt, system instruction, chat message, chat context, and every history turn. It pattern-matches common secret shapes (assignment-style `KEY = "value"`, GitHub/Slack/AWS/OpenAI-style token prefixes, PEM private-key blocks, bearer tokens) and replaces matches with `[REDACTED_SECRET]`. This exists specifically because repository content reaching a prompt (a connected repo's own files, or a security finding's code snippet) can legitimately contain a real committed secret — the app does not ask the user's permission per-request before that content reaches a third-party provider, so redaction is unconditional, not opt-in. It is deliberately independent of `analyses/services/custom_rules_service.py`'s scanning rules (see that module's own docstring on why): scanning optimizes for reporting accuracy, redaction optimizes for never letting a plausible secret leave — over-redacting a harmless string is an acceptable cost, under-redacting a real one is not.

### 4.5 AI output validation before it is trusted further

A parsed AI response is not assumed well-formed. `ai/validation.py` provides two checks, applied wherever a response is about to be persisted or displayed:

- `clean_ai_prose()` — type-checks (rejects non-strings) and length-caps free text (suggestions, explanations, remediations, chat replies). Truncation is safe for prose.
- `is_valid_ai_code()` — type/size-checks a refactor's rewritten code, **never truncates it** (silently cutting off code could return something that looks valid but doesn't parse, which is worse than the existing "couldn't parse a refactor, fall back to raw text" path already handles).

This closes a real gap: `RefactorView`'s parser previously trusted a parsed `code` field's type without checking it, so a malformed response (`{"code": {...}}`) would have reached a `.save()` call on a text-only DB field uncaught.

### 4.6 AI output → GitHub: a centralized, code-level sanitization boundary

`github_integration/services/comment_service.py`'s `_sanitize_for_github()` is the single point every free-text field reaching a GitHub review/comment payload passes through before becoming real, externally-visible GitHub content — per-issue fields (`message`, and especially the AI-written `explanation`/`remediation` from `ai_security_service`) as well as the review's overall `pr_analysis.summary` text. No free-text field is exempted, even one (like `summary`) that is currently built only from numeric counts and so has no attacker-influenced content today. It:

- strips raw HTML tags outright,
- defangs `@mentions` (a zero-width space after `@` breaks GitHub's mention detection while leaving the text readable),
- wraps URLs (bare, or as a markdown link's target) in a code span, which GitHub does not auto-link or parse markdown inside,
- caps each field's length,
- and fails closed to an empty string for anything that isn't a usable, non-empty string — never coerces or raises.

Ordinary markdown formatting (bold/italic/lists/code spans) is left alone; only the constructs that would make GitHub treat the text as *live* content are neutralized. This exists because the automatic PR-review pipeline (webhook → Celery task → AI enrichment → posted comment) has no human-approval step between AI output and a real write using the connecting user's own GitHub credentials — the sanitization boundary is what stands in for that missing review, not the prompt-level `UNTRUSTED_DATA_WARNING`.

### 4.7 AI output can never execute code or invoke a privileged action

There is no tool/function-calling anywhere in this codebase — every AI response is parsed for display fields only (`text`, `explanation`, `remediation`, `code`, `changes`) and never branches into a control-flow decision. Concretely:

- `analyses/sandbox.py`'s `run_python()` is only ever called with user-submitted or GitHub-fetched source code (`analyses/engine.py`, `github_integration/services/pr_analysis_service.py`) — never with `ai_refactored_code` or any other AI-generated text. AI output cannot reach the sandbox, and the sandbox's own kernel-enforced isolation is documented separately (see the sandbox implementation notes in this repository).
- No AI-derived value is ever used to build a raw SQL query, a shell command, or a filesystem path.
- No AI response field is ever read as an action/command that the app then executes — deleting/modifying data, GitHub operations beyond posting the one sanitized review comment described in §4.6, sending email, or any admin-level operation are all triggered exclusively by explicit user HTTP actions, never by parsed AI content.

### 4.8 Rate and concurrency controls

Two independent mechanisms, for two different failure modes:

- **`AIRateThrottle`** (`core/throttling.py`, scope `ai`, 30/min per user) bounds request *rate over time*, same mechanism as every other throttle in §3.1. `SecurityAnalysisView` carries both `AnalysisCreateRateThrottle` and `AIRateThrottle` together, so it draws on the same per-user AI budget as the other four AI-triggering endpoints rather than a separate one.
- **`ai/concurrency.py`'s `ai_concurrency_slot()`** bounds *concurrent in-flight AI requests*, which a rate-over-time throttle does not: every AI-triggering endpoint calls its provider synchronously, in-request, occupying a gunicorn worker for up to the full fallback-chain duration (§4.9). A per-user slot (`cache.add`, atomic) caps one in-flight AI request per user; a small global slot pool caps total concurrent AI requests app-wide, below the total worker count, so at least one worker always stays available for non-AI traffic even if every AI slot is in use. Built on the same cache backend (`core.cache.ResilientRedisCache`/Redis in production, degrading to a cache-miss on outage) that already backs the rate throttles — no new infrastructure. Capacity exhaustion returns an immediate `429`, never a queued wait (queuing would tie up the very worker this exists to protect).

### 4.9 Provider fallback and timeouts

`ai/client.py`'s `_call_with_fallback` tries Groq, then Gemini, then OpenRouter, each bounded by `AI_REQUEST_TIMEOUT_SECONDS` (20s) with no SDK-level retries; if all three fail, the original exception from the last provider is re-raised (`raise last_error`) — **fail-closed**, never a silent default/empty "success" that a caller could mistake for a real response. Worst case per request: 3 × 20s = 60s, which is why `gunicorn --timeout 120` (`backend/Dockerfile`) sits comfortably above it.

### 4.10 Remaining limitations / assumptions

- The `UNTRUSTED_DATA_WARNING`/`wrap_untrusted()` framing (§4.2) has not been red-teamed against a live model with adversarial payloads in this codebase — it is documented here as a hardening measure precisely because its effectiveness against a specific attack is not proven, and every control in §4.4–§4.6 is designed to hold regardless of whether it succeeds or fails.
- `_sanitize_for_github()`'s HTML-tag/mention/URL neutralization is pattern-based, not a full HTML/markdown parser — it targets the specific constructs known to make GitHub treat text as live content, not an exhaustive allowlist.
- `ai/redaction.py`'s secret patterns are heuristic (assignment-style secrets, known vendor token prefixes, PEM blocks, bearer tokens) — a secret in a shape none of those patterns cover would not be redacted. This is a real residual risk for repository content sent to third-party AI providers, disclosed rather than implied to be fully solved.
- The AI concurrency slot's global cap is a fixed pool size tuned to this app's current gunicorn worker count (3, `backend/Dockerfile`); if the worker count changes, the pool size should be reviewed alongside it.

## 5. Data & Environment

### 5.1 No hardcoded secrets

Every credential, API key, and secret value in this codebase is read from an environment variable via `os.environ.get(...)` — there are **no hardcoded fallback values for secrets** anywhere in `config/settings.py`. `SECRET_KEY` in particular has no default at all (`os.environ['SECRET_KEY']`, not `.get()`) — a missing value fails Django startup loudly rather than silently falling back to a key that might already be sitting in git history.

Real `.env` files are gitignored and have never been committed; `.env.example` (both at the repository root and in `backend/`) documents every required and optional variable with placeholder/blank values only.

**`ALLOWED_HOSTS` is an explicit allowlist read from the environment, and production refuses to start without one.** `core/settings_validation.py`'s `validate_allowed_hosts()` runs while settings load and raises `ImproperlyConfigured` when `ENVIRONMENT=production` and the list is either empty or contains `'*'`:

- `'*'` disables Django's Host header validation outright, which is what stands between this app and Host-header poisoning — a request carrying an attacker-controlled `Host` reaches Django, and anything built from it (most importantly the password-reset links in `accounts/emails.py`) can be pointed at the attacker's domain. It is a tempting thing to set when a deploy starts returning 400s, so it fails the boot rather than relying on review to catch it.
- An empty list under `DEBUG=False` rejects every request, so the app would boot cleanly and then 400 on all traffic; failing at startup names the actual cause.

Development is left alone (Django's own `DEBUG=True` behavior implicitly allows localhost), so no local configuration is needed to run the server. The leading-dot form `.example.com` — a domain and all its subdomains — remains allowed, since it is still an explicit allowlist entry rather than a wildcard.

### 5.2 Lazy configuration validation

Third-party integrations that are optional at deploy time (Brevo, GitHub OAuth/webhooks, Google OAuth, each AI provider) validate their own configuration **on first actual use**, not at Django process startup — a missing key surfaces as a clear `503`/`ImproperlyConfigured` error from the specific endpoint that needed it, rather than preventing the entire application from booting. This is deliberate: it means, for example, a deployment without GitHub integration configured still serves every other feature correctly.

### 5.3 API key inventory

| Variable | Purpose | Required? |
|---|---|---|
| `SECRET_KEY` | Django's cryptographic signing key (sessions, password reset tokens, CSRF). | **Yes**, no default. |
| `DATABASE_URL_DEV` / `DATABASE_URL_PROD` | PostgreSQL connection string, selected by `ENVIRONMENT`. | **Yes**. |
| `OTP_PEPPER_KEY` | Keys the HMAC that hashes registration OTP codes at rest (§2.2). | Optional — falls back to `SECRET_KEY`. |
| `BREVO_API_KEY`, `BREVO_SENDER_EMAIL`, `BREVO_SENDER_NAME` | Transactional email (OTP, password reset). | Optional at boot; required for any email to actually send. |
| `GOOGLE_CLIENT_ID` | Google OAuth login verification. | Optional — Google login disabled if unset. |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_TOKEN_ENCRYPTION_KEY` | GitHub OAuth login + repo integration + webhook signature verification + at-rest token encryption. | Optional — GitHub features disabled if unset. |
| `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY` | AI provider fallback chain. | Optional individually — AI features unavailable only if *all three* are unset. |
| `COOKIE_DOMAIN` | Shares the CSRF cookie across frontend/backend subdomains in production. | Optional — required only when frontend and backend are deployed on different subdomains of one domain. |

Rotating any of these values requires only an environment-variable update and a redeploy — none are baked into build artifacts on the backend. (Frontend-side public values — `VITE_API_BASE_URL`, `VITE_GOOGLE_CLIENT_ID` — are compiled into the static JS bundle at build time, as is standard for Vite; OAuth client IDs are not treated as secret, since they are inherently public once shipped to a browser.)

### 5.4 Data at rest

- Passwords: Django's default PBKDF2 hashing, never stored or logged in plaintext.
- OTP codes: keyed HMAC-SHA256 hash only, never plaintext (§2.2).
- GitHub access tokens: Fernet-encrypted (§2.4), single-key — see §6.2 for the key-rotation backlog item.
- No sensitive value (password, OTP code, JWT, API key) is ever written to application logs — structured log calls (`core/logging_formatters.py`) pass identifiers (user ID, email, request path) as context, not credentials.

## 6. Backlog

### 6.1 Dependency Auditing

Both CI jobs (`.github/workflows/ci.yml`) run a dependency vulnerability scan on every push and pull request to `main`/`dev`:

- **Backend**: `pip-audit -r requirements.txt`, checking installed pins against the Python Packaging Advisory Database.
- **Frontend**: `npm audit --audit-level=high`, which reports only high/critical advisories — the level at which an out-of-band dependency bump is actually warranted.

Both steps are currently **advisory (`continue-on-error: true`), not gating.** This is a deliberate, temporary state, for two reasons: the advisory feed moves independently of this repository, so a hard failure would red-line builds over a CVE published in a transitive dependency that the pull request under test never touched; and there is an existing backlog that a gate would block every build on until it is cleared. As of this writing that backlog is:

| Package | Advisories | Fix |
|---|---|---|
| `Django 4.2.30` | 7 | Requires a major upgrade to 5.2.x — 4.2 LTS no longer receives these fixes. |
| `sqlparse 0.5.5` | 4 | `0.6.0` |
| `pypdf 6.14.2` | 2 | `6.15.0` |
| `cryptography 49.0.0` | 1 | `50.0.0` |
| `react-router` / `react-router-dom` | 1 high | In-range `7.x` bump |
| `postcss` | 1 moderate (below the `--audit-level=high` threshold) | In-range bump |

Removing `continue-on-error` from both steps is a one-line change per job, and should be done as soon as that list is cleared — at which point these become real gates rather than reports.

### 6.2 GitHub encryption-key rotation

**Status: not implemented, and not currently necessary.** Stored GitHub access tokens are encrypted with a single Fernet key (`GITHUB_TOKEN_ENCRYPTION_KEY`, `github_integration/services/encryption.py`). Rotating that key today makes every stored ciphertext undecryptable — the code handles this cleanly rather than crashing (`TokenDecryptionError`, surfaced to the user as "reconnect your GitHub account"), and the tokens are re-obtainable through the OAuth flow at any time, so the blast radius is a reconnect prompt, not data loss. Nothing else in the system is encrypted with this key.

Supporting rotation without that reconnect would mean `MultiFernet` (decrypt against an ordered list of keys, encrypt with the first) plus a management command to re-encrypt existing rows, and a key-*list* configuration surface in place of the current single value. That has deliberately not been built: it adds configuration complexity to solve a problem this deployment does not have.

Revisit if any of the following becomes true:

- Stored tokens stop being cheaply reissuable (e.g. a move to GitHub App installation tokens with a different re-auth cost).
- A compliance requirement mandates scheduled key rotation.
- The connected-user population grows large enough that mass reconnection is a meaningful operational event rather than a handful of prompts.
