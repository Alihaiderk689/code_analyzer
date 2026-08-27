# Error Handling

How Code Analyzer handles failure, as actually implemented. Every behavior below was verified against the source; anything missing is marked **Gap** rather than described as if it existed.

Companion docs: [ARCHITECTURE.md](ARCHITECTURE.md) for system design, [SECURITY.md](SECURITY.md) for the security model.

---

## 1. The Global Exception Handler

**`backend/core/exceptions.py`** — wired in as DRF's `EXCEPTION_HANDLER`. Every API error passes through it.

It delegates to DRF's own handler first, then does two things DRF does not:

1. **Rewrites throttle responses.** A `Throttled` exception's body is replaced with `{"detail": "Too many requests. Try again in N second(s)."}`, or `"Too many requests. Please slow down."` when `wait` is unknown.
2. **Catches everything DRF returns `None` for** — bugs, third-party client errors, database failures. Without this they propagate to Django, which renders an **HTML** 500 page. That is doubly wrong here: `safeJson()` in the frontend cannot parse it, and under `DEBUG` it would expose a full traceback.

Unhandled exceptions are logged via `logger.exception('unhandled_exception', extra={'path', 'method'})` — full traceback server-side — and the client gets:

| | Response body | Status |
|---|---|---|
| `DEBUG=False` (production) | `{"detail": "An unexpected error occurred. Please try again later."}` | 500 |
| `DEBUG=True` (development) | `{"detail": "<str(exc)>", "exception": "<ClassName>"}` | 500 |

The **contract this establishes: every response from this API is JSON.** Nothing in the system returns HTML errors. Code that adds a new failure mode should preserve that.

> **Gap — no error correlation ID.** Nothing ties the generic production message to the logged traceback. A user reporting "it said an unexpected error occurred" cannot be matched to a specific log line without timestamp guesswork. Attaching a short random ID to both the log record and the response body would close this.

## 2. Expected vs Unexpected Exceptions

The codebase draws a consistent line, and it is worth preserving:

**Expected** — a domain outcome modeled as a typed exception, caught at a known boundary, converted to a specific status and message. `GitHubAuthError`, `GitHubRateLimitError`, `BrevoAPIError`, `GoogleTokenError`, `TokenDecryptionError`, `WebhookVerificationError`, `OAuthStateError`, DRF `ValidationError`. These never reach the global handler.

**Unexpected** — anything else. Reaches `custom_exception_handler`, is logged with a traceback, and returns the generic 500.

Two places deliberately catch broadly, both with a comment explaining why:

- `analyses/analysis_views.py::_run_analysis` — `except Exception` marks the `Analysis` row `FAILED` rather than leaving it stuck in `PENDING`/`RUNNING` or 500ing.
- `github_integration/tasks.py` — `except Exception` marks the `PullRequestAnalysis` failed and the `WebhookEvent` processed, so an unexpected bug cannot leave a permanently "running" row plus an un-retried webhook.

Broad catches are acceptable **when they exist to leave clean persistent state**, not to silence errors. Both still log with `logger.exception`.

## 3. Authentication, Authorization & CSRF

| Failure | Where | Client receives |
|---|---|---|
| No/invalid access token | `accounts/authentication.py` (`CookieJWTAuthentication`) | `401` |
| Cookie auth, missing/bad CSRF token | same, `_enforce_csrf` → `PermissionDenied` | **`403`** `{"detail": "CSRF Failed: ..."}` |
| Authenticated but not staff on `/api/admin/` | `IsAdminUser` | `403` |
| Object owned by another user | `get_object_or_404(..., owner=request.user)` | **`404`, not 403** |
| Missing refresh cookie on `/api/auth/refresh/` | `accounts/views.py::RefreshView` | `401` `{"detail": "Refresh token missing."}` |
| Invalid/expired/blacklisted refresh token | same | `401` + **auth cookies cleared** |

Two deliberate choices worth knowing:

- **CSRF failure is 403, not 401.** The frontend's `apiFetch` only attempts a token refresh on `401`, so a CSRF failure correctly does *not* trigger a refresh-and-retry loop — retrying would fail identically.
- **Ownership violations return 404.** Scoping the queryset (`owner=request.user`) rather than fetching-then-checking means a foreign object is indistinguishable from a nonexistent one, so object IDs cannot be enumerated. Do not "improve" this to a 403.

**Logout never fails.** `LogoutView` blacklists the refresh token if one is present, swallows `TokenError` if it is expired or already used, and always clears cookies and returns 200 — the client-side goal holds either way.

## 4. OTP & Registration Errors

`accounts/otp.py::verify_otp` returns `(success, error_code)` rather than raising. `accounts/serializers.py` maps the code to a message:

| Internal code | Message | Status |
|---|---|---|
| `expired` | "This code has expired, request a new one." | 400 |
| `too_many_attempts` | "Too many incorrect attempts, request a new one." | 400 |
| `incorrect` | "Incorrect code." | 400 |

`incorrect` covers three distinct internal states — no OTP on file, a wrong code, and an unknown email address — deliberately, so the endpoint cannot be used to test which addresses have a pending registration.

**Registration email failure is transactional.** `RegisterView` wraps user creation, OTP issuance, and the Brevo send in one `transaction.atomic()`. A `BrevoAPIError` rolls back the `User` row entirely and returns `503` `{"detail": "Email service is currently unavailable. Please try again."}` — rather than stranding an account whose code was never delivered. `ResendVerificationView` does the same, minus the rollback (the account already exists).

**Concurrency.** `verify_otp` runs the whole read-check-write cycle inside a transaction with `select_for_update()` on the `Profile` row. Without it, concurrent wrong guesses lose increments and the 5-attempt lockout is bypassed. Covered by `accounts.tests.OtpConcurrencyTests`.

## 5. OAuth Errors

**Google** (`accounts/google_auth.py`) — every failure path raises `GoogleTokenError`: network failure to `tokeninfo` or `userinfo`, a non-OK response, or an `aud` claim that doesn't match `GOOGLE_CLIENT_ID`. `GoogleLoginSerializer` catches it and returns a single generic `400` "Invalid Google credential." Timeout is 10s per call, so a worst case is ~20s.

Note the collapse: "Google is down" and "your token is invalid" produce the same client-facing message. Deliberate for the invalid-token case; arguably wrong for an outage, where `503` would be more accurate.

**GitHub** (`github_integration/oauth_views.py`) — the callback is a top-level browser redirect, so it **never returns JSON**. Every outcome redirects into the SPA with an error query parameter:

| Condition | Redirect |
|---|---|
| User denied consent | `?error=access_denied` |
| No `code` parameter | `?error=missing_code` |
| Bad/expired `state`, or nonce mismatch | `?error=invalid_state` |
| GitHub OAuth not configured | `?error=not_configured` |
| GitHub API failure | `?error=github_error` |
| Email not verified at GitHub (login only) | `?error=email_not_verified` |
| Email already linked to another identity | `?error=account_conflict` |

The nonce cookie is cleared **on every outcome**, success or failure, so a stale nonce can never be replayed.

`exchange_code_for_token` handles a GitHub quirk explicitly: a bad or expired code returns **HTTP 200 with an `{"error": ...}` body**, not a non-2xx status, so the body is checked separately.

## 6. Validation Errors

DRF serializers are the validation boundary; `serializer.is_valid(raise_exception=True)` produces a `400` with a **field-keyed** body:

```json
{ "email": ["This email is already registered."], "password2": ["Passwords do not match."] }
```

The frontend's `extractMessage` (`lib/api.js`) handles both shapes: it prefers `data.detail` when present, otherwise takes the first key and renders `"<field>: <first message>"`.

> **Gap — only the first field error surfaces.** A submission failing three fields shows one message. Forms in `Register.jsx`/`Settings.jsx` compensate with client-side per-field validation, so this is rarely visible, but a backend-only rule (e.g. the Have I Been Pwned check) that fails alongside another field will have its message hidden.

### Query and body parameter hardening

Not everything goes through a serializer. Where raw parameters are read, the handling is uneven:

| Input | Handling | Verdict |
|---|---|---|
| `tz_offset_minutes` (chat, file check, context check quotas) | `_clamp_offset()` catches `TypeError`/`ValueError` → 0, then clamps to UTC-14..UTC+12 | **Robust.** Implemented identically in all three of `chat/rate_limit.py`, `github_integration/services/file_check_rate_limit.py`, and `context_check_rate_limit.py`, so a malformed or hostile offset cannot shift a day boundary |
| `limit` (`RecentAnalysesView`) | `try/except ValueError` → 10, clamped to 1..50 | **Robust** |
| `q` (`SearchView`) | Empty → `400`; otherwise used in `icontains` (ORM-parameterized) | **Safe** |
| `status` / `language` filters (`SearchView`) | Passed straight to `.filter()` with no validation | **Gap — see below** |
| `path` (`RepositoryFileAnalyzeView`) | Empty → `400`, then `classify_path()` | **Safe** |

> **Gap — unvalidated filter values return an empty list, not a 400.** `analyses/search_views.py::SearchView` passes `status` directly to `.filter(status=...)`. A typo or an invalid choice matches nothing, so the client gets `{"count": 0, "results": []}` — indistinguishable from "you have no analyses matching that." No injection risk (the ORM parameterizes), but a client bug is silently invisible. Validating against `Analysis.Status.values` and returning `400` would surface it.

**File uploads** are validated at two levels: `UploadRequestSerializer` enforces a 2MB cap, and `AvatarUploadSerializer` enforces 5MB plus DRF's `ImageField`, which runs the file through Pillow and raises a `400` for anything that isn't a decodable image. A corrupt or spoofed-extension image is therefore rejected cleanly rather than crashing.

`lib/validation.js` mirrors `accounts/validators.py` for inline feedback. **The backend is the only enforcement that counts**, and it applies checks the client cannot — Django's validators and the HIBP breach lookup — so a password can pass client-side and still be rejected with a 400.

## 7. Database & Transaction Errors

Handled narrowly and specifically, where a race is actually expected:

| Location | Caught | Why |
|---|---|---|
| `accounts/serializers.py::GoogleLoginSerializer` | `IntegrityError` | Concurrent first-login (double-click) racing the `google_id` unique constraint → generic 400 |
| `accounts/github_auth.py` | `IntegrityError` | Same race on `github_id` |
| `github_integration/services/webhook_service.py` | `IntegrityError` | Duplicate webhook `delivery_id` — a GitHub retry, treated as already-received |
| `accounts/tokens.py` | avoided via `bulk_create(ignore_conflicts=True)` | A concurrent logout blacklisting the same token |

`transaction.atomic()` is used where partial state would be wrong: registration + email send, OTP verification (with the row lock), and Google user creation.

> **Gap — no handling for database unavailability.** `OperationalError` / `InterfaceError` are caught nowhere. A dropped connection or an exhausted Supabase pooler surfaces as a generic 500 through the global handler. That is safe (it fails closed and logs a traceback) but there is no retry, no connection-health check, and no distinct status — a transient blip looks identical to a bug. The health-check endpoint in `core/` does not verify database reachability either.

## 8. AI Provider Failures

**`backend/ai/client.py`** is the single AI call path. `_call_with_fallback` tries **Groq → Gemini → OpenRouter** in order, catching `Exception` from each, logging `logger.warning('AI provider %s failed...', exc_info=True, extra={'provider': name})`, and moving on. Any failure demotes a provider — missing key, rate limit, outage, malformed response. If all three fail, the **last** exception is re-raised.

All three call sites catch that re-raise identically and return `503`:

| Call site | Message |
|---|---|
| `analyses/ai_views.py` (suggestions, explanation, refactor) | "AI service is currently unavailable." |
| `chat/views.py` | "AI service is currently unavailable. Your message was saved - try again shortly." |
| `ai/views.py` (floating chat) | 503 |

**Chat saves the user's message before calling the LLM**, so a provider failure never loses the question — hence the different wording.

**Degraded-but-successful paths.** Two places treat AI failure as non-fatal rather than a 503:

- `analyses/services/ai_security_service.py` — if enrichment fails, findings fall back to scanner-provided explanation/remediation text. The security report is still returned.
- `analyses/ai_views.py::_parse_suggestions` / `_parse_refactor_response` — if the model ignores the requested JSON shape, output is salvaged (each non-empty line becomes a suggestion; the whole response becomes raw code). Malformed AI output never 500s.

### Timeout behavior — a real gap

| Provider | Timeout | Retries |
|---|---|---|
| Gemini | `timeout=30` explicit | none |
| OpenRouter | `timeout=30` explicit | none |
| **Groq** | **none set** — SDK default: 60s read, connect 5s | **SDK default: 2 retries** |

> **Gap — worst-case AI latency exceeds the proxy timeout.** Groq alone can consume roughly 3 × 60s before failing over (60s read × 1 initial + 2 SDK retries), then Gemini adds up to 30s and OpenRouter another 30s — on the order of **four minutes**, against nginx's `proxy_read_timeout 120s`. The user gets a proxy timeout, not the intended `503`, and the request keeps running server-side. Passing an explicit `timeout=` and `max_retries=0` to the `Groq(...)` constructor, and/or capping total chain time, would bound this.

> **Gap — no circuit breaker.** A hard-down primary provider is retried on every single request; there is no memory of recent failures.

## 9. GitHub Integration Failures

**`github_integration/services/github_client.py`** normalizes every failure into one exception family, raised by `_raise_for_response`:

| Exception | Trigger | Carries |
|---|---|---|
| `GitHubAuthError` | HTTP 401 | — |
| `GitHubRateLimitError` | 403/429 **with `X-RateLimit-Remaining: 0`** | `reset_at` (unix ts) |
| `GitHubAPIError` | any other non-2xx, or a `requests.RequestException` | `status_code`, `response_body` |

Timeout is 15s on every call. Note the rate-limit discrimination: a 403 *without* an exhausted-remaining header is a permissions problem, not a rate limit, and is correctly raised as a plain `GitHubAPIError`.

### Synchronous request paths

`repository_views.py::_handle_github_error` is the single translation point:

| Exception | Status | Side effect |
|---|---|---|
| `GitHubAuthError` | `401` "Your GitHub connection has expired or was revoked. Please reconnect." | Sets `integration.token_invalid = True` |
| `GitHubRateLimitError` | `429` + `reset_at` in the body | — |
| `GitHubAPIError` | `503` "Could not reach GitHub. Please try again shortly." | Logs with `status_code` |

Setting `token_invalid` on a 401 is what lets the UI prompt a reconnect instead of failing silently on every subsequent call.

A user with no linked account gets `400` "Connect your GitHub account first." from `_get_integration_or_error` — not a 401, since they *are* authenticated.

### Asynchronous (Celery) paths

`github_integration/tasks.py`, `max_retries = 3`:

| Condition | Behavior |
|---|---|
| `GitHubAuthError` | **Not retried** — a revoked token won't start working. Marks integration invalid, marks the analysis permanently failed. |
| `GitHubRateLimitError` | Retried with `countdown = reset_at - now` — waits exactly until the limit resets rather than guessing. Falls back to 60s if no `reset_at`. |
| `GitHubAPIError` | Retried with exponential backoff: 30s, 60s, 120s. |
| Retries exhausted | `_mark_permanently_failed` — sets `PullRequestAnalysis.status = FAILED`, stores the message in `.error` (truncated to 4000 chars), marks the `WebhookEvent` processed. |
| Any other `Exception` | Logged with traceback, marked permanently failed. Never left running. |
| `WebhookEvent` row missing | Logged as an error, task returns — nothing to do. |
| Repository deselected mid-flight | Logged at **info**, marked processed. Not an error. |
| Commit already analyzed | Logged at info, marked processed. Idempotency for redelivered webhooks. |

**Webhook receipt** (`webhook_views.py`): missing `X-GitHub-Event`/`X-GitHub-Delivery` → `400`. `WebhookVerificationError` (bad HMAC signature) → `401` "Invalid signature.", logged with the delivery ID. Everything valid → **`202 Accepted` immediately**, before any analysis runs, because GitHub times out and redelivers if made to wait.

## 10. Code Analysis, Upload & Report Errors

**Upload** (`analyses/analysis_views.py::UploadView`): files over 2MB are rejected by the serializer with a 400. A `UnicodeDecodeError` on a non-UTF-8 file creates an `Analysis` row with `status=FAILED` and returns **`201 Created`**.

> **Gap — a failed upload returns 201 with no reason.** The status code says "created" and the payload says `FAILED`, with nothing indicating *why*. Worse, **the `Analysis` model has no `error` field** (unlike `PullRequestAnalysis.error` and `RepositoryIndex.error`), so neither this case nor an engine crash can tell the user what went wrong. Adding an `error` field, and returning `400` for an undecodable upload, would fix both.

Pasted code is validated for emptiness and a 200,000-character cap (400 on violation).

**Analysis engine** (`analyses/engine.py`): `_run_analysis` catches any exception, logs `analysis_run_failed` with the analysis ID, and marks the row `FAILED`. Syntax errors in *submitted* code are not failures — they are analysis **results**, reported as issues via `pyflakes`, with cascading syntax errors de-duplicated.

**Sandbox** (`analyses/sandbox.py`) — returns a status dict, never raises:

| Result | Meaning |
|---|---|
| `{'status': 'error', 'exception_type', 'message', 'line'}` | Submitted code raised — a finding, not a system failure |
| `{'status': 'timeout'}` | Exceeded the 5s wall clock |
| unavailable | Non-macOS host; surfaces as a zero-penalty `runtime_check_unavailable` informational issue |

The unavailable case is **self-disclosing by design** — it degrades visibly rather than silently skipping runtime checks. Unparseable stderr falls back to `{'exception_type': 'Error', 'message': <tail of stderr, 300 chars>}`.

**Security scanning** is failure-tolerant at three levels:

1. `security_service.py::_run_scanners` catches per scanner — one crashing tool doesn't take down the report; the others still run and a partial report is returned.
2. `bandit_service.py` returns `[]` on `FileNotFoundError` (Bandit not on PATH), `TimeoutExpired`, or unparseable JSON — each logged at the appropriate level.
3. Bandit's own `errors` array (e.g. a syntax error preventing AST construction) is logged at info; other scanners still run.

> **Note:** because a missing Bandit binary returns `[]` and logs an error, a security report can come back **clean-looking when the primary scanner never ran**. Nothing in the API response distinguishes "no vulnerabilities found" from "the scanner was unavailable." Surfacing scanner availability in the report — the way `runtime_check_unavailable` does for the sandbox — would close this.

**PDF reports** (`analyses/report_views.py`): if `pisa.CreatePDF` sets `result.err`, returns `500` "Failed to generate PDF report." No traceback is logged for this path, since it isn't an exception.

## 11. Rate Limiting & Throttling

DRF throttles raise `Throttled`, rewritten by the global handler into `429` with `{"detail": "Too many requests. Try again in N second(s)."}`.

**Domain quotas** are separate and return their own bodies, not the generic throttle message:

- Chat daily limit (`chat/views.py`): `429` with `{"detail": "You've used today's chat messages. Try again after the reset.", ...limit_status}` — including `reset_at` so the UI can count down.
- GitHub file check (`repository_views.py`): `429` with quota status. Re-requesting **the same path** already checked today returns the cached result instead of a 429; skip-eligible files (binary, lock, generated) never consume quota at all.

> **Gap — throttle counters are per-process.** There is no `CACHES` setting, so Django's default `LocMemCache` applies and each gunicorn worker keeps its own counters. The effective limit is roughly `rate × worker_count`, and a user can be throttled by one worker and not another. Configuring a shared Redis cache would make the published rates real. The domain quotas above are unaffected — they derive from database rows.

## 12. Logging & Sensitive Data

`core/logging_formatters.py::StructuredFormatter` appends any `extra={...}` as trailing JSON. **Without it, Python's default formatter silently drops `extra` entirely** — so a log call using `extra` outside this formatter's configuration produces no structured output at all.

Levels in use: `logger.exception` for unexpected failures needing a traceback; `logger.error` for expected-but-serious (auth failures, network errors, Bandit missing); `logger.warning` for recoverable (AI provider fallback, rate limits, invalid signatures); `logger.info` for non-error outcomes (repo deselected, already analyzed).

Root level is `INFO` in production, `WARNING` under `manage.py test`.

### What is logged

Identifiers and context only — `user_id`, `analysis_id`, `pr_analysis_id`, `repository_id`, `delivery_id`, `provider`, `status_code`, `path`, `method`, `url`.

### What must never be logged

Verified absent from every current log call, and must stay that way:

- **Passwords** — plaintext or hashed, from any source.
- **OTP codes** — `accounts/views.py` logs the event name `accounts.otp_email_failed` with `exc_info`, never the code.
- **JWTs** — access or refresh, raw or decoded.
- **GitHub access tokens** — note that `exchange_code_for_token` only logs on *failure*, where the body contains no token; success is not logged at all.
- **API keys** — `SECRET_KEY`, `OTP_PEPPER_KEY`, `GITHUB_TOKEN_ENCRYPTION_KEY`, Brevo/Groq/Gemini/OpenRouter keys.
- **`Authorization` header values.**

> **Watch item — response bodies are logged verbatim.** `github_client._raise_for_response` and `BrevoClient.send_email` both log the full error `body` on failure. Today those bodies are provider error messages and contain no secrets. This is safe by circumstance, not by construction: a provider that started echoing request content into an error body would put it in the logs. Truncating, or allowlisting fields, would make it safe by design.

> **Gap — submitted source code in tracebacks.** `_run_analysis` logs with `logger.exception`, so a crash inside the engine can put fragments of user-submitted code into log output via the traceback's local variables (depending on the formatter). Worth knowing if analysis logs are shipped to a third-party aggregator.

## 13. HTTP Status Conventions

Observed consistently across the codebase; follow these when adding endpoints.

| Status | Used for |
|---|---|
| `200` | Success with a body |
| `201` | Resource created (analysis, security scan, file check) |
| `202` | Webhook accepted, work queued |
| `204` | Success, no body (CSRF prime, account deletion) |
| `400` | Validation failure; also "connect GitHub first"; also wrong-state operations ("analysis must be completed first") |
| `401` | Not authenticated, or an expired/revoked credential — **including GitHub's stored token** |
| `403` | Authenticated but not permitted; **CSRF failure** |
| `404` | Not found **or not owned** — deliberately indistinguishable |
| `429` | Throttled or daily quota exhausted |
| `500` | Unexpected error (generic message in production); PDF generation failure |
| `503` | A configured external dependency is unavailable — AI providers, Brevo, GitHub network failure, unconfigured integration |

The `400`-vs-`503` line: **`400` means the client got it wrong; `503` means we or a dependency did.** A missing API key is a `503`, not a `400`, even though it's arguably a configuration error, because the client cannot fix it.

## 14. Frontend Error Handling

**`frontend/src/lib/api.js`** normalizes everything into a single `ApiError { status, data, message }`:

- **`safeFetch`** wraps `fetch`, which rejects only on network-level failure. Those become `ApiError` with **`status: 0`** and "Unable to reach the server." — so `err.status === 0` reliably means "never reached the server," distinct from any real HTTP status.
- **`safeJson`** returns `null` on unparseable bodies rather than throwing — so an unexpected HTML response degrades to a generic message instead of a second exception.
- **401 → refresh-and-retry, once.** On a 401 with `auth: true`, `apiFetch` calls `/auth/refresh/` and replays the original request. `refreshPromise` de-duplicates concurrent refreshes, so ten parallel 401s trigger one refresh. If refresh fails, the caller gets `ApiError(401, "Session expired. Please sign in again.")` — unless the failure was a network error, which is re-thrown as-is so "you're offline" isn't misreported as "you're logged out."

**`ErrorBoundary.jsx`** (mounted in `main.jsx`) catches render and lifecycle errors, logs to `console.error`, and shows a "Something went wrong" screen with a reload button — preventing the blank white screen a React tree unmount would otherwise produce.

> **Gap — the error boundary is client-side only.** `componentDidCatch` writes to the browser console; nothing is reported to the backend or any monitoring service, so frontend crashes in production are invisible to the team unless a user reports one.

> **Gap — error boundaries don't catch async errors.** React error boundaries only catch errors thrown during render/lifecycle. A rejected promise in an event handler or `useEffect` is unhandled, so pages must catch their own `apiFetch` rejections. Most do; there is no lint rule enforcing it.

**`AuthContext.jsx`** restores the session on boot by attempting `fetchProfile()` and treating any failure as "not logged in". Two consequences of that being a bare `catch`:

> **Gap — a network failure at boot is indistinguishable from being logged out.** `AuthContext.jsx`'s `restore()` catches everything, so a user who loads the app while briefly offline (or during a backend restart) is shown the logged-out UI despite holding valid cookies. Checking for `err.status === 0` and rendering a "can't reach the server" state instead would separate the two.

> **Gap — `checkIsAdmin()` fails closed on *any* error.** `AuthContext.jsx::checkIsAdmin` probes an admin-only endpoint and returns `false` for a 403, a 401, a 500, *and* a network error. A transient blip during boot silently downgrades a real admin to the non-admin UI for the whole session, with no error shown and no retry. Failing closed is the right direction for a permission check; doing it silently is what makes it hard to diagnose.

**Pages** catch `ApiError` and render `err.message` when it is an `ApiError`, falling back to a page-specific generic message otherwise (`Settings.jsx`: "Could not change password."). Each page owns its own loading/error state — there is no global toast or notification system.

## 15. Production vs Development

| | Development | Production |
|---|---|---|
| Unhandled exception body | `str(exc)` + exception class name | Generic message only |
| Root log level | `INFO` (`WARNING` under tests) | `INFO` |
| HSTS | Not sent | `max-age=31536000; includeSubDomains` |
| Django debug page | Never — the global handler intercepts first | Never |
| DRF browsable API | Available | **Available** — see below |

**`DEBUG` is derived from `ENVIRONMENT`**, which is validated against `{development, production}` at startup; a typo like `prod` raises `ImproperlyConfigured` rather than silently defaulting to development. `ALLOWED_HOSTS` is validated the same way (`core/settings_validation.py`).

> **Gap — the DRF browsable API is enabled in production.** `DEFAULT_RENDERER_CLASSES` is unset, so `BrowsableAPIRenderer` is active in every environment. A browser hitting an API URL directly gets a rendered HTML page including a form for any method it is allowed to use. It does not bypass authentication or permissions, so this is a surface-area and information-disclosure concern rather than a vulnerability. Setting `DEFAULT_RENDERER_CLASSES` to `JSONRenderer` only in production would remove it — and would also make the HTML CSP branch in `core/middleware.py` apply to admin only.

## 16. Edge Cases Worth Knowing

- **Analysis rows can be left `PENDING`** if the process dies mid-analysis. `_run_analysis` is synchronous and in-request, so there is no reaper — nothing ever transitions an abandoned row to `FAILED`.
- **Access tokens survive refresh-token revocation** for up to their 15-minute lifetime. Password change and reset blacklist refresh tokens (`accounts/tokens.py`), which bounds a hostile session to minutes, but does not sever it instantly.
- **Rotating `GITHUB_TOKEN_ENCRYPTION_KEY` invalidates every stored token.** `decrypt_token` raises `TokenDecryptionError`, and users must reconnect. Handled cleanly, not a crash — see `SECURITY.md` §5.2.
- **An unconfigured integration fails at first use, not at boot.** Deliberate: a deployment without GitHub credentials still serves every other feature. The cost is that a missing key is invisible until a user hits the endpoint and gets a `503`.
- **Chat messages are persisted before the AI call**, so a `503` never loses the user's question — but the conversation is then left with a user message and no reply.
- **Webhook redelivery is handled twice over**: by `delivery_id` uniqueness (`IntegrityError` → already received) and by a commit-SHA check (already-analyzed → no-op).
- **A `404` on an owned-object endpoint is ambiguous by design** — nonexistent and not-yours are the same response.
- **`ALLOWED_HOSTS` misconfiguration in production fails at startup**, not per-request, so it cannot manifest as confusing 400s under load.

## 17. Error Handling Rules for Developers

1. **Never let an exception escape as HTML.** The global handler covers it, but a 500 is a last resort, not a design.
2. **Raise a typed exception at the boundary; translate it in the view.** Follow `GitHubAPIError` / `BrevoAPIError`: a dedicated class carrying `status_code` and `response_body`, and one translation point per app.
3. **Scope every queryset to `request.user`** and let `get_object_or_404` produce the 404. Never fetch-then-check.
4. **Set an explicit timeout on every outbound HTTP call.** The existing convention is 10–30s. `requests` has **no default timeout** — omitting it means the request can hang indefinitely.
5. **Use the right status:** `400` client error, `401` bad credential, `403` authenticated-but-forbidden, `404` missing-or-not-owned, `429` throttled, `503` dependency down, `500` bug.
6. **Log with `extra={...}`**, never by interpolating secrets into the message. Include an identifier (`user_id`, `analysis_id`) so the entry is traceable.
7. **Catch broadly only to leave clean persistent state**, and always log with `logger.exception` when you do.
8. **Prefer degraded output over a hard failure** where the user still gets value — the pattern in `ai_security_service` (fall back to scanner text) and `_parse_suggestions` (salvage malformed output).
9. **Add the endpoint to `frontend/src/lib/resources.js`**, never call `apiFetch` from a page — that is what keeps 401-refresh and network-error normalization uniform.
10. **Mirror any new validation rule** in both `accounts/validators.py` and `lib/validation.js`, remembering the backend is the enforcement.

## Appendix: Open Gaps

Collected from above, ranked roughly by impact.

| # | Gap | Location |
|---|---|---|
| 1 | Groq has no explicit timeout; worst-case AI chain (~4 min) exceeds nginx's 120s proxy timeout | `ai/client.py` |
| 2 | Throttle counters are per-worker (`LocMemCache`, no `CACHES` setting) | `config/settings.py` |
| 3 | `Analysis` has no `error` field — a `FAILED` analysis cannot say why | `analyses/models.py` |
| 4 | Undecodable upload returns `201` with `FAILED` status instead of `400` | `analyses/analysis_views.py` |
| 5 | A missing Bandit binary yields a clean-looking report; scanner availability isn't surfaced | `analyses/services/` |
| 6 | No database-unavailability handling; health check doesn't verify DB reachability | project-wide, `core/views.py` |
| 7 | DRF browsable API enabled in production | `config/settings.py` |
| 8 | No error correlation ID linking a 500 response to its logged traceback | `core/exceptions.py` |
| 9 | Frontend errors are console-only; no reporting to backend or monitoring | `ErrorBoundary.jsx` |
| 10 | Only the first field validation error reaches the user | `lib/api.js` |
| 11 | Provider error bodies logged verbatim — safe today, not by construction | `github_client.py`, `brevo_client.py` |
| 12 | No circuit breaker; a down AI provider is retried on every request | `ai/client.py` |
| 13 | Abandoned `PENDING` analyses are never reaped | `analyses/` |
| 14 | `checkIsAdmin()` fails closed on any error, silently downgrading a real admin for the session | `AuthContext.jsx::checkIsAdmin` |
| 15 | A network failure at boot is shown as "logged out" | `AuthContext.jsx::restore` |
| 16 | Google outage and invalid token both return the same `400` | `accounts/google_auth.py` |
| 17 | Invalid `status`/`language` filter values return an empty list instead of `400` | `analyses/search_views.py::SearchView` |
