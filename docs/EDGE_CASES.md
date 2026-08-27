# Edge Cases

Boundary conditions, races, and unusual scenarios in Code Analyzer that are worth knowing before you change something near them.

**Scope.** This is not a failure-handling reference — [ERROR_HANDLING.md](ERROR_HANDLING.md) covers what happens when things throw, [OPERATIONS.md](OPERATIONS.md) covers diagnosing production incidents, [SECURITY.md](SECURITY.md) covers the threat model. This document covers the *inputs and timings* that produce surprising behavior, including cases where the surprising behavior is correct.

Every entry is classified:

| | Meaning |
|---|---|
| ✅ **Handled** | Deliberately addressed; the behavior is correct |
| 🟡 **Partially handled** | Addressed for the common path, with a real hole |
| 🔵 **Known limitation** | Understood consequence of a deliberate design choice |
| 🔴 **Gap** | Not addressed; a reader might reasonably assume it is |

Every claim was verified against source. Where something could not be verified from the repository, it says so.

---

## 1. Authentication & Session

### 1.1 Two tabs refresh at the same time 🔴 Gap

`frontend/src/lib/api.js::refreshAccessToken` dedupes concurrent refreshes with a module-level `refreshPromise` — **within one tab only**. `SIMPLE_JWT` has `ROTATE_REFRESH_TOKENS: True` and `BLACKLIST_AFTER_ROTATION: True` (verified in `config/settings.py`).

Two tabs whose access tokens expire together both POST `/api/auth/refresh/`. The first rotates the refresh token and blacklists the old one. The second presents the now-blacklisted token, gets `401`, and `apiFetch` surfaces `"Session expired. Please sign in again."`

Nothing coordinates across tabs — no `BroadcastChannel`, no `storage` event listener (verified absent in `api.js` and `AuthContext.jsx`).

**Why it matters:** a multi-tab user can be logged out of one tab for no reason they can perceive. The cookie is shared, so the surviving tab keeps working — which makes it look arbitrary.

### 1.2 CSRF failure does not trigger a refresh loop ✅ Handled

`accounts/authentication.py::_enforce_csrf` raises `PermissionDenied` → **`403`**, not `401`. `apiFetch` only refreshes on `401`, so a CSRF failure fails once instead of looping through refresh-and-retry. Worth preserving if you touch either side.

### 1.3 Logging out with an already-blacklisted token ✅ Handled

`accounts/views.py::LogoutView` catches `TokenError` and always clears cookies, returning `200`. A double logout, an expired cookie, or a replayed token all succeed. The client-side goal holds either way.

### 1.4 Refresh cookie is path-scoped ✅ Handled

`accounts/cookies.py` scopes `refresh_token` to `/api/auth/` while `access_token` is `/`. The browser never attaches the longer-lived credential to ordinary API calls. Note the operational consequence: `delete_cookie` silently no-ops on a path mismatch, so any new endpoint reading the refresh cookie must live under `/api/auth/`.

### 1.5 Password change keeps the calling session alive 🟡 Partially handled

`ChangePasswordView` revokes every refresh token, then immediately mints a fresh pair for the caller. Other devices are logged out on their next refresh; the caller continues seamlessly.

The hole: **access tokens already issued elsewhere stay valid for up to their 15-minute lifetime** (`ACCESS_TOKEN_LIFETIME: timedelta(minutes=15)`). Revocation acts on refresh tokens only, so booting an attacker is bounded by minutes, not instant.

### 1.6 Changing your email re-runs verification ✅ Handled *(fixed)*

`accounts/serializers.py::ProfileSerializer` exposes `email` as writable (`read_only_fields = ['id', 'date_joined']`), and `is_verified` is read-only — verified by instantiating the serializer: `email writable: True`, `is_verified read_only: True`.

**Fixed.** `ProfileView.patch` now detects an email change, clears `is_verified`, issues a fresh OTP, and emails it — all inside one `transaction.atomic()`, so a Brevo failure rolls the address change back rather than leaving an account pointing at an unverified address with no code on its way. `is_active` is untouched, so the user keeps full access during re-verification.

Previously the address moved and carried the verified flag with it, and future password-reset mail followed it to an address nobody had proved control of.

---

## 2. Registration & OTP

### 2.1 Concurrent verification attempts ✅ Handled

`accounts/otp.py::verify_otp` runs the whole read-check-increment cycle in `transaction.atomic()` with `Profile.objects.select_for_update()`.

Without the lock this is a lost-update race: N parallel guesses all read the same `otp_attempts` and all write back the same value + 1, so N guesses cost one attempt and `OTP_MAX_ATTEMPTS = 5` is bypassed. Verified by removing the lock in a scratch run: **5 concurrent wrong guesses cost 2 attempts, and the correct code still verified afterward.** Covered by `accounts.tests.OtpConcurrencyTests`.

### 2.2 Email send fails after the user row is created ✅ Handled

`RegisterView` wraps user creation, OTP issuance, and the Brevo send in one `transaction.atomic()`. A `BrevoAPIError` rolls the `User` back and returns `503` — no account is stranded holding a code that was never delivered.

### 2.3 Resend invalidates the previous code ✅ Handled

`issue_otp` overwrites `otp_code_hash`, resets `otp_expires_at`, and zeroes `otp_attempts`. A user who requests a second code cannot use the first — and a user locked out after 5 attempts gets a clean slate, which is why the account-scoped throttle (§9.2) is set above `OTP_MAX_ATTEMPTS`.

### 2.4 Verifying does not log you in 🔵 Known limitation

`VerifyOtpView` returns `{"detail": "Email verified successfully."}` and issues no tokens. The user is sent to `/login`. Deliberate — it matches the previous link-based flow.

### 2.5 Three different failures return one message ✅ Handled

`VerifyOtpSerializer` maps a nonexistent email, no OTP on file, and a wrong code all to `"Incorrect code."` (`400`). The endpoint cannot be used to discover which addresses have a pending registration. Expiry and lockout return distinct messages, which is safe — they only reveal state about a code the caller already had to name.

### 2.6 A code expiring mid-attempt ✅ Handled

Expiry is checked inside the locked transaction, before the attempt counter increments. A code that expires between page load and submit returns `expired`, and the attempt is not charged.

### 2.7 Rotating `OTP_PEPPER_KEY` invalidates in-flight codes 🔵 Known limitation

`_otp_pepper()` reads `settings.OTP_PEPPER_KEY or settings.SECRET_KEY` per call. Rotating either makes every outstanding `otp_code_hash` unmatchable — users mid-signup get "Incorrect code." Bounded by the 10-minute expiry; documented in the setting's own comment.

---

## 3. OAuth & GitHub Integration

### 3.1 Duplicate webhook delivery ✅ Handled — two layers

1. `WebhookEvent.delivery_id` is `unique`. `webhook_service.receive` catches `IntegrityError` inside a **nested** `transaction.atomic()` (a savepoint, so it doesn't poison an outer transaction), logs `github_webhook.duplicate_delivery`, and returns `should_process=False`.
2. A genuine redelivery can arrive with a *different* `delivery_id`. `tasks.py` then finds an existing `PullRequestAnalysis` for the same `(repository, pull_request_number, commit_sha)` already `completed`, logs `github_task.already_analyzed`, and stops.

### 3.2 Webhook received but nothing happens ✅ Handled (looks like a bug, isn't)

`webhook_service.HANDLED_PR_ACTIONS = {'opened', 'reopened', 'synchronize', 'edited'}`. Any other action — `labeled`, `closed`, `assigned` — sets `should_process=False`. Pushes are filtered too: deletions (`{"deleted": true}`) and non-default-branch pushes are skipped, because they cannot change what HEAD-of-default-branch analysis sees.

The event is stored, marked processed, and `202` is returned. `github_webhook.received` logs `should_process` so you can tell a deliberate skip from a failure.

**Why it matters:** "webhook delivered, no review appeared" is expected for most PR activity. Check `should_process` before hunting a bug.

### 3.3 Repository deselected while a task is in flight ✅ Handled

Between webhook receipt and task execution the repo may no longer be monitored. All three tasks catch `GitHubRepository.DoesNotExist`, log at **info** (`github_task.repository_not_monitored*`), mark the event processed, and return. Not treated as an error.

### 3.4 Access token revoked mid-pipeline ✅ Handled

`GitHubAuthError` (401) is **not retried** — a revoked token will not start working. The task sets `integration.token_invalid = True`, marks the analysis permanently failed, and returns. The flag is what lets the UI prompt a reconnect instead of failing silently forever.

### 3.5 Rate limit hit during analysis ✅ Handled

`GitHubRateLimitError` carries `reset_at` from `X-RateLimit-Reset`. The task retries at `max(1, reset_at - now)` — waiting exactly until reset rather than guessing — falling back to `DEFAULT_RETRY_COUNTDOWN_SECONDS = 60`. After `MAX_RETRIES = 3` it fails permanently with a message in `.error`.

Note the discrimination in `github_client._raise_for_response`: a 403 is only a rate limit when `X-RateLimit-Remaining == '0'`. A 403 without it is a permissions problem and raises plain `GitHubAPIError` (retried with 30/60/120s backoff).

### 3.6 Selecting a second repository silently deselects the first 🔵 Known limitation

`repository_service.select_repository` deselects every other active repo for the integration before selecting the new one — one monitored repo per integration, and the old webhook is deleted from GitHub. Re-selecting an already-monitored repo with a live webhook is a no-op, not an error, and does not create a duplicate webhook.

**Why it matters:** a user selecting a second repo loses monitoring on the first with no confirmation step.

### 3.7 Indexing a large repository truncates ✅ Handled, disclosed

`GITHUB_MAX_INDEXED_FILES = 300`. Beyond that, candidates are sliced and `RepositoryIndex.truncated = True`. GitHub's own tree truncation sets the same flag (`github_client.tree_truncated`). The index completes with a partial graph rather than failing.

**`truncated=True` is not an error.** Indexing costs one API call per file, which makes it the heaviest GitHub consumer in the system.

### 3.8 Webhooks created before `push` support 🔵 Known limitation

GitHub does not retroactively change an existing webhook's event list. Repos selected before `push` joined `WEBHOOK_EVENTS` never trigger re-indexing on push. `python manage.py backfill_webhook_push_events` PATCHes them; safe to re-run.

### 3.9 OAuth login-CSRF ✅ Handled

The GitHub login `state` carries a nonce bound to the initiating browser via a short-lived httpOnly cookie, checked at the callback. Without it, a signed `state` proves only that *we* issued it — an attacker could complete their own authorization and hand the resulting URL to a victim, logging the victim into the attacker's account. The nonce cookie is cleared **on every outcome**, so a stale nonce is never replayable.

### 3.10 OAuth callback never returns JSON ✅ Handled

`GitHubCallbackView` is a top-level browser redirect. Every outcome redirects into the SPA with `?error=<reason>` (`access_denied`, `missing_code`, `invalid_state`, `not_configured`, `github_error`, `email_not_verified`, `account_conflict`). A DRF JSON body would strand the user on a bare API URL.

### 3.11 Bad OAuth code returns HTTP 200 ✅ Handled

GitHub returns **200 with an `{"error": ...}` body** for an expired or reused code rather than a non-2xx. `exchange_code_for_token` checks the body separately, and also rejects a 200 response with no `access_token`.

### 3.12 Repository-context fetching runs out of time ✅ Handled *(fixed)*

One context check (`RepositoryFileContextAnalyzeView` → `PRAnalysisService.analyze_file_with_context`) makes up to **11** GitHub calls: the target file, a repo tree + `settings.py` lookup, up to 6 dependency-graph neighbors, and a second lazy `settings.py` lookup. Each carried `REQUEST_TIMEOUT_SECONDS = 15` and nothing bounded the sum — **165s**, above gunicorn's 120s, so a degraded GitHub produced a `502` with no partial result.

**Fixed** by `github_integration/services/fetch_budget.py`. A single `FetchBudget` per context analysis holds a **`time.monotonic()` deadline** of `settings.GITHUB_CONTEXT_FETCH_BUDGET_SECONDS` (default **45s**) and is shared by every `GitHubClient` in the phase. Unlike the per-call timeouts, this *is* a total: the service checks it before each neighbor fetch, and `GitHubClient._request` clamps each call's timeout to `min(15s, remaining)`, so no number of calls can carry the phase past the deadline. (The trickle caveat in §5.5 still applies to any single response arriving in slow chunks; what is eliminated is the *accumulation* across calls.)

**Exhaustion degrades, it does not fail.** The fully-analyzed primary file and every neighbor already collected are kept, analyzed and persisted. The response and the `RepositoryContextCheck` row both carry `context_truncated: true` / `context_truncated_reason: "fetch_budget_exhausted"` — stored, not derived, so the free same-path-again-today cached response reports it too. `github_context_check.truncated` is logged with the neighbor count reached.

**Four outcomes stay distinct**, which is the point of `FetchBudgetExceeded` *not* being a `GitHubAPIError` subclass — the existing `except GitHubAPIError` handlers physically cannot swallow it:

| Outcome | Signal | Effect |
|---|---|---|
| Budget exhausted | `context_truncated_reason = fetch_budget_exhausted`, `github_context_check.truncated` | Partial context, `201` |
| Token revoked (401) | `GitHubAuthError` → `integration.token_invalid = True` | `401`, reconnect prompt (§3.4) |
| Rate limit (403/429 + `X-RateLimit-Remaining: 0`) | `GitHubRateLimitError.reset_at` | `429` with `reset_at` (§3.5) |
| One neighbor unfetchable | `github_context_check.related_fetch_failed` | Neighbor omitted, **not** truncation |

The budget only applies where it was added: PR review, OAuth, repo listing and index building build `GitHubClient` with no budget and are unchanged.

🔵 **What this does not bound.** Only the GitHub fetching. The security/AI pipeline then runs once per fetched file — bounded separately in §3.13.


### 3.13 Repository-context analysis runs out of time ✅ Handled *(fixed)*

§3.12 bounded the GitHub fetching. It did not bound what happens after each fetch, which is the bigger cost: `analyze_file_with_context` runs the whole per-file pipeline on the primary file **and each of up to 6 neighbors**, and each run has three wall-clock stages.

| Stage | Ceiling | Runs for |
|---|---|---|
| `sandbox.run_python` via `analyze_code` → `_python_issues` → `_python_runtime_issues` | `sandbox.TIMEOUT_SECONDS` = 5s | Python, macOS hosts (`sandbox.is_available()`) |
| `BanditScanner.scan` | `BANDIT_TIMEOUT_SECONDS` = 20s | Python |
| `AISecurityService.enrich` → `ai.client._call_with_fallback` | 3 × `AI_REQUEST_TIMEOUT_SECONDS` = 90s | any file with findings |

**115s per file × 7 files = 805s**, with per-stage timeouts and no total. The sandbox stage is the easily-missed one: it sits three frames below `analyze_code` and costs nothing on Linux, so it is invisible in production traces but real on a macOS host.

**Fixed** by `core/execution_budget.py`. One `RequestBudget` per context analysis holds a **`time.monotonic()` deadline** of `settings.GITHUB_CONTEXT_REQUEST_BUDGET_SECONDS` (default **90s**) and is threaded to all four expensive stages, GitHub fetching included. `FetchBudget` (§3.12) is now a subclass of the same `ExecutionBudget` and is built as `min(fetch_budget_setting, request_budget.remaining())` — a share of the 90s, never added to it. 90s leaves a **30s margin** under gunicorn's 120s for the DB writes and CPU-bound static analysis that follow.

🔵 **Stages are skipped, not squeezed.** Each has a minimum viable slice (5s sandbox, `MIN_BANDIT_SLICE_SECONDS` 5s, `MIN_AI_SLICE_SECONDS` 8s, `MIN_RELATED_FILE_BUDGET_SECONDS` 12s). Below it the stage does not start; above it, it runs with its timeout clamped to what remains. This is deliberate: Bandit squeezed into half a second is killed and reports `reason='timeout'`, which blames the scanner for our deadline — the same mistake §3.12 avoided for network errors.

**Degradation preserves work.** The primary file keeps its static analysis and every scanner finding. Neighbors already analyzed are kept. Findings whose AI prose was skipped keep the scanner's own explanation/remediation — the fallback `enrich` has always used, so no finding is ever dropped or left blank. The response and the persisted `RepositoryContextCheck` carry `context_truncated_reason` and a new `degraded_stages` list; a check can be *not* truncated (every neighbor covered) and still degraded (AI skipped), so the two are separate fields.

**Five outcomes stay distinct.** `BudgetExceeded` is its own exception type, unrelated to `GitHubAPIError` or to any provider/scanner error, and is re-raised past every broad `except Exception` on the way out:

| Outcome | Signal | Effect |
|---|---|---|
| Request budget exhausted | `request_budget.exhausted`, `context_truncated_reason=request_budget_exhausted` | Partial analysis, `201` |
| Fetch budget exhausted | `github_fetch_budget.exhausted`, `context_truncated_reason=fetch_budget_exhausted` | Partial context, `201` (§3.12) |
| AI provider down | `AI provider X failed, falling back…` | Next provider, **same order**; scanner text if all fail (§5.5) |
| Bandit genuinely hung | `ScannerUnavailable(reason='timeout')` | Report says `scan_complete: false` |
| Bandit not started / cut short by the budget | `ScannerUnavailable(reason='budget_exhausted')` | Distinct from the row above |

Every consumer takes `budget=None` and is unchanged without one, so PR review, the plain single-file check, paste/upload analysis, the security-scan endpoint and both chat surfaces are untouched — the AI fallback order in particular is never reordered, only stopped early.

🔵 **The cost.** A degraded run returns *less* without the UI saying so: the fields are on the response, but no frontend component renders them yet. See OPERATIONS.md §16.

---

## 4. Code Analysis & Upload

### 4.1 Whitespace-only submission is rejected ✅ Handled *(fixed)*

`engine.analyze_code` counts `lines_of_code` as non-blank lines. `_score` returns `0.0` when `lines_of_code == 0`.

**Fixed.** `UploadView` now rejects a whitespace-only decoded file with `400`, matching `AnalyzeRequestSerializer.validate_code` on the paste path. Previously only the paste path was guarded, so a blank upload completed with **quality score 0.0 and zero issues** — reading as "catastrophically bad code" when it meant "no code".

### 4.2 Non-UTF-8 upload is rejected with 400 ✅ Handled *(fixed)*

**Fixed (contract change).** `UploadView` now returns `400` with an explanatory `file` error and persists nothing. Previously it created an `Analysis` with `status=FAILED` and returned **`201 Created`** — "created" for something that never analysed, with no field to say why (`Analysis` still has no `error` field, unlike `PullRequestAnalysis` and `RepositoryIndex`).

This is a deliberate contract change: the old response made a client error look like a successful submission.

### 4.3 Syntax errors are results, not failures ✅ Handled

Broken code produces `pyflakes` issues on a `completed` analysis. `_dedupe_cascading_syntax_errors` collapses the cascade a single unbalanced bracket produces, so one mistake yields one issue.

### 4.4 Only the first runtime error is ever reported 🔵 Known limitation

`_python_runtime_issues` reports at most one `runtime_error` — sandboxed execution stops at the first uncaught exception, exactly as running the script would. Fixing it and re-running can surface a second, previously invisible error.

### 4.5 Missing third-party import is silently not an error ✅ Handled (deliberate)

The sandbox runs bare system Python. A script importing `requests` fails with `ImportError`, which `run_python` classifies as `import_error` and `_python_runtime_issues` maps to **`[]`** — no issue reported. Reporting it would blame the user for the sandbox's missing packages.

**Why it matters:** for import-heavy code, runtime checking effectively does nothing, and the report gives no indication.

### 4.6 Cancel is almost always a no-op 🔵 Known limitation

`_run_analysis` is **synchronous and in-request**, so by the time `POST /api/analysis/<id>/` returns, the row is already `completed`. `CancelView` rejects anything not `pending`/`running` with `400`.

Cancel is therefore only reachable for rows abandoned by a process death mid-analysis — which is also the only way a row gets stuck. There is no reaper; Cancel is the manual cleanup.

### 4.7 Concurrent re-analysis of one row 🔴 Gap

`ReanalyzeView` sets `status=PENDING`, then calls `_run_analysis`, which writes results and sets `completed`. No lock, no state guard. Two simultaneous re-analyses of the same `Analysis` interleave their writes; last writer wins. Both run the engine, doubling the work.

Harmless today because analysis is deterministic and the row ends `completed` either way — but it is an unguarded read-modify-write, unlike the OTP path (§2.1).

### 4.8 Language detection can guess wrong ✅ Handled honestly

`detect_language_from_code` scores keyword signatures and adds a weak +2 for parsing as valid Python. It returns `'Unknown'` when nothing scores rather than guessing — for a trivial snippet, "Unknown" is the honest answer. Uploads use the filename extension instead (`detect_language`), which is more reliable.

A snippet misdetected as Python gets pyflakes and sandbox execution run against it; both fail gracefully rather than crashing.

### 4.9 Clearing history keeps your GitHub quota spent ✅ Handled

`ClearHistoryView` deletes all of a user's analyses, cascading to `Conversation` and `ChatMessage`. `RepositoryFileCheck.analysis` is `SET_NULL` (verified), so the quota row **survives** with `analysis=None`.

Correct: the daily file-check quota counts `RepositoryFileCheck` rows, so deleting analyses cannot be used to reset it.

---

## 5. AI Providers

### 5.1 One provider down ✅ Handled

`ai/client.py::_call_with_fallback` tries Groq → Gemini → OpenRouter, catching **any** exception per provider — missing key, rate limit, outage, malformed response — logging `AI provider %s failed` and continuing.

### 5.2 All three down ✅ Handled

The last exception is re-raised; all three call sites return `503`. `chat/views.py` persists the user's message **before** calling the LLM, so the question is never lost — hence its distinct wording ("Your message was saved").

### 5.3 Model ignores the requested JSON shape ✅ Handled

`_parse_suggestions` falls back to treating each non-empty line as an uncategorized suggestion. `_parse_refactor_response` falls back to treating the whole response as raw code with no explanation. Malformed AI output never 500s.

### 5.4 AI enrichment fails during a security scan ✅ Handled

`ai_security_service.enrich` catches everything and substitutes scanner-provided explanation/remediation text. The security report is still returned — degraded, not failed.

### 5.5 The chain is bounded below the request timeout ✅ Handled *(fixed)*

**Fixed.** All three providers now share `settings.AI_REQUEST_TIMEOUT_SECONDS` (default 30s), and the Groq client is constructed with `max_retries=0` so the SDK cannot silently multiply its own timeout. The AI-only path budgets 3 × 30 = **90s**, and gunicorn now runs with `--timeout 120`.

🔵 **Two things "bounded" does not mean here.** `requests` and `httpx` timeouts apply per I/O phase (connect, and the gap between received chunks), not to total wall-clock duration — so a trickling server can exceed its budget without tripping the timeout; for the AI chain, gunicorn's `--timeout` is the only hard bound. And the AI chain is not the longest synchronous path: a security scan runs Bandit (20s) *then* the full chain, for a **110s** budget, and repo-context analyze runs that whole pipeline once per file it fetched — see §3.12 and OPERATIONS.md §12.

Previously Groq had no explicit timeout (SDK default 60s read × 2 retries), making the chain's worst case ≈ 4 minutes against gunicorn's 30s default — so the request died before the fallback could finish being resilient, and the client got a `502` rather than the intended `503`.

The two values must move together: raising the gunicorn timeout without bounding providers just lets one request hold 1 of 3 workers for minutes.

### 5.6 No memory of recent failures 🔴 Gap

No circuit breaker. A hard-down Groq is retried on every single request, paying its full timeout each time before falling through.

---

## 6. Security Scanner & Sandbox

### 6.1 Unavailable scanners are reported, not hidden ✅ Handled *(fixed)*

**Fixed, in two parts.**

1. *Reporting.* Scanners now report why they could not run (`ScannerUnavailable` in `analyses/services/types.py`), and the report carries `scan_complete: false` plus `scanners_unavailable[]`. The score is deliberately unchanged — a scanner failing is not evidence of vulnerabilities, so the honest signal is the flag, not an invented penalty. Partial results are preserved: other scanners still run and their findings are still returned. Consumers that take a flat issue list rather than the whole report (PR review, the single-file check, the context check — all via `pr_analysis_service._analyze_file_content`) get each unavailable scanner folded in as a zero-penalty `scanner_unavailable` issue at severity `info`; without that they read only `vulnerabilities` and an unavailable scanner rendered as a clean scan on those three paths.

2. *Root cause.* `bandit_service` invoked a bare `'bandit'`, which is not on `PATH` when Python is run by absolute path (an unactivated venv, some process managers) — so `subprocess` raised `FileNotFoundError` and the scan silently returned nothing. It now runs `sys.executable -m bandit`, binding the scanner to the same environment Django is running in. **This was live in the local test environment**, where it had been producing empty scans and 9 failing tests.

Older cached reports predating these fields deserialize with `scan_complete: true`, which is correct — they were produced when the scanner was running.

### 6.2 Bandit can't parse the submitted code ✅ Handled

A syntax error prevents AST construction. Bandit reports it in its `errors` array; the service logs at **info** and continues. Other scanners still run, so a partial report is produced.

### 6.3 Sandbox unavailable off macOS ✅ Handled — self-disclosing

`sandbox.is_available()` returns `False` on any non-macOS host, so **every deployed environment** (Linux) skips runtime checks. This surfaces as a `runtime_check_unavailable` issue with a **zero penalty** (`ISSUE_PENALTIES['runtime_check_unavailable'] = 0`) and an explicit message.

Visible degradation with no score impact. Compare §6.1.

### 6.4 Infinite loop in submitted code ✅ Handled

`TIMEOUT_SECONDS = 5` wall clock, plus `CPU_TIME_SECONDS = 5`, `MAX_PROCESSES = 16`, and `MAX_OUTPUT_FILE_BYTES` rlimits. Timeout returns `execution_timeout` with a "possible infinite loop" message.

🔵 **Known limitation, disclosed in the module docstring:** memory limits (`RLIMIT_AS`/`RLIMIT_DATA`) cannot be lowered on macOS, so the wall clock is the only bound on memory exhaustion.

### 6.5 Duplicate findings across scanners ✅ Handled

`security_service._deduplicate` keys on `(vulnerability_type, line_number)` and keeps the first — two scanners flagging one line don't double-count or double-score.

### 6.6 Unparseable sandbox stderr ✅ Handled

Falls back to `{'exception_type': 'Error', 'message': <tail of stderr, 300 chars>}` rather than dropping the result.

---

## 7. Database, Concurrency & Transactions

### 7.1 Simultaneous first OAuth login ✅ Handled

Double-clicking "Sign in with Google" can race two user creations against the `google_id` unique constraint. `GoogleLoginSerializer` wraps creation in `transaction.atomic()` and catches `IntegrityError`, returning a generic `400`. `accounts/github_auth.py` does the same for `github_id`.

### 7.2 Concurrent token revocation ✅ Handled

`accounts/tokens.py` uses `bulk_create(..., ignore_conflicts=True)`. A concurrent logout blacklisting the same token between the query and the insert is the outcome we wanted anyway, so it must not raise.

### 7.3 Nested transaction on duplicate webhook ✅ Handled

`webhook_service` uses a **nested** `atomic()` so the `IntegrityError` rolls back only its savepoint — without it, the failed insert would poison any outer transaction (tests wrap each test in one).

### 7.4 Database unavailable returns 503 ✅ Handled *(fixed)*

**Fixed.** `core/exceptions.py` now maps `OperationalError`/`InterfaceError` to **`503`** with a retryable message, logged as `database_unavailable`. Previously these surfaced as a generic `500`, which implies "we broke, retrying won't help".

**No automatic retry was added, deliberately** — retrying into an exhausted pooler is what turns a blip into an outage. `conn_max_age=600` still holds connections for ten minutes.

Readiness is now observable too: `/api/health/ready/` probes the database and returns `503` when it is unreachable (§9.3).

### 7.5 `WebhookEvent` is never cascaded away 🔵 Known limitation

Verified: `WebhookEvent` has **no foreign key** to any other model. Deleting a user, integration, or repository leaves every webhook row — each holding a full JSON payload — behind forever. Nothing prunes them.

Deliberate as an audit log; the consequence is unbounded growth.

---

## 8. Rate Limiting & Quotas

### 8.1 Malformed timezone offset ✅ Handled

All three quota modules (`chat/rate_limit.py`, `file_check_rate_limit.py`, `context_check_rate_limit.py`) implement identical `_clamp_offset`: `int()` inside `try/except (TypeError, ValueError)` → `0`, then clamped to UTC-14…UTC+12.

A hostile client cannot send `tz_offset_minutes=999999` to shift the day boundary and reset its quota early.

### 8.2 Both OTP throttles apply together ✅ Handled

`VerifyOtpView.throttle_classes = [OtpVerifyRateThrottle, OtpVerifyAccountRateThrottle]`. The IP bucket (`otp_verify`, 20/hour) bounds total volume from a source; the IP+email bucket (`otp_verify_account`, 10/hour) bounds how much can be aimed at one account. Neither subsumes the other.

The account bucket hashes the lowercased email before it becomes a cache key — so varying capitalization cannot reset the counter, and the cache keyspace is not enumerable into recently-attempted addresses.

### 8.3 Verify request with no email ✅ Handled

`OtpVerifyAccountRateThrottle.get_cache_key` returns `None` when the body has no usable email — the throttle abstains rather than lumping every such request into one shared bucket. The IP throttle still counts it, and the request fails validation anyway.

### 8.4 Shared IP behind NAT 🔵 Known limitation

Both OTP throttles key on IP, so an office or CGNAT egress shares one `otp_verify` budget. The account-scoped throttle limits per-target damage but does not remove the shared ceiling — enough simultaneous signups from one IP can still exhaust it for unrelated users.

### 8.5 Throttle counters are shared via Redis ✅ Handled *(fixed)*

**Fixed.** `CACHES` now points at the Redis already provisioned for Celery (via `CACHE_REDIS_URL`, defaulting to `CELERY_BROKER_URL`), so throttle counters are shared across workers. Previously the default per-process `LocMemCache` meant published rates were roughly 3× looser with `--workers 3`.

🔵 **Deliberate trade-off:** the backend is `core.cache.ResilientRedisCache`, which treats a Redis outage as a cache miss — so while Redis is down, **DRF throttles fail open** rather than 500ing every endpoint. This is safe because the protections that actually stop brute force do not use this cache: OTP attempt lockout is a database row under `select_for_update` (§2.1), and daily quotas derive from database rows (§8.7). Degradation is logged and reported by `/api/health/ready/`.

Falls back to `LocMemCache` when no Redis URL is configured and always under `manage.py test`, so the suite needs no running Redis.

### 8.6 Re-checking the same file is free ✅ Handled

`RepositoryFileAnalyzeView` returns today's stored result when the requested path matches `today_check` — no new GitHub or AI call, and no quota spent. A *different* path returns `429`.

Skip-eligible files (binary, lock, generated, unsupported language) are classified by `classify_path` **before** the quota gate and never consume quota — a misclick on a lockfile doesn't burn the day's one check.

### 8.7 Quota resets at the user's local midnight, not a rolling window ✅ Handled

`_local_midnight_boundary` computes the UTC instant of the most recent local midnight. Using your last message at 9pm returns the full allowance at midnight, not 9pm the next day. A user who travels across timezones between requests sees the boundary move — a consequence of the client reporting its own offset, which is the only signal the server has.

---

## 9. Production & Deployment

Full operational detail is in [OPERATIONS.md](OPERATIONS.md); these are the boundary conditions.

### 9.1 Celery worker boots on Render ✅ Handled *(fixed)*

**Fixed in deployment config, not by weakening validation.** `ALLOWED_HOSTS` moved into `render.yaml`'s `code-analyzer-shared` env group, so both the web service and the worker inherit it. `validate_allowed_hosts` remains strict.

Previously it was set only on the web service, so the worker started with an empty list, failed validation, and crash-looped — while webhooks kept returning `202`, making the failure externally invisible. Guarded by `core.tests.RenderDeploymentConfigTests`.

### 9.2 Avatars persist and are served ✅ Handled *(fixed)*

**Fixed, both halves.** `render.yaml` mounts a 1GB disk at `/app/media` on the web service, so uploads survive redeploys; and `config/urls.py` registers the `/media/` route unconditionally.

Note the subtlety in the second half: `django.conf.urls.static.static()` **returns `[]` when `DEBUG` is False** — that helper *is* the DEBUG gate — so simply removing the surrounding `if settings.DEBUG:` would have changed nothing. The pattern is now registered directly with `re_path(..., django.views.static.serve)`, which is safe (it uses `safe_join`, so path traversal does not apply) if not the fastest way to serve a file.

🔵 **Remaining limitation:** `django.views.static.serve` has no caching layer and resolves the path per request — acceptable for small, rarely-fetched avatars, and the route should be removed again if object storage or a CDN is introduced. `AvatarUploadView` still overwrites `profile.avatar` without deleting the previous file, so orphans now genuinely accumulate on the persistent disk.

### 9.3 Readiness reports dependency outages ✅ Handled *(fixed)*

**Fixed by splitting liveness from readiness**, rather than by making the existing endpoint probe dependencies.

- `/api/health/` (liveness) still returns a static `{'status': 'ok'}` and is what `render.yaml`'s `healthCheckPath` points at. **This is deliberate:** if the platform's restart trigger probed the database, a transient Postgres blip would restart every container — which cannot fix a database problem and turns a brief outage into a restart loop on top of it.
- `/api/health/ready/` (readiness) probes the database and returns **`503`** when it is unreachable. Cache failure reports `degraded` but stays `200`, because throttles fail open (§8.5) and requests still succeed. Nothing restarts on this endpoint; it exists so monitoring and operators get a truthful answer.

### 9.4 Startup fails closed on misconfiguration ✅ Handled

`ENVIRONMENT` is validated against `{development, production}` — `prod` raises rather than silently running as development. `SECRET_KEY` uses `os.environ[...]` with no default. `ALLOWED_HOSTS` rejects `'*'` and empty in production. Verified by simulation: explicit hosts boot; `*` and empty are both refused with actionable messages.

### 9.5 Missing third-party config fails at first use, not boot ✅ Handled (with a cost)

Every external integration validates lazily — a deployment with no GitHub credentials still serves everything else. The cost: a missing key is invisible until a user hits the endpoint and gets a `503`.

### 9.6 Only the web container migrates ✅ Handled

`entrypoint.sh` branches on `$1 = "gunicorn"`, so the Celery worker (same image, `celery` command) skips `migrate`/`collectstatic` — two containers starting together cannot race the same migration.

🔴 **Gap on Render:** there is no `depends_on`, so the worker can start before the web container finishes migrating. Currently masked by §9.1.

---

## 10. Frontend & Network

### 10.1 Network failure is distinguished from being logged out ✅ Handled *(fixed)*

**Fixed.** `AuthContext.restore` now inspects the error: `ApiError` with `status === 0` (the sentinel `apiFetch` already set for network failures) sets an `offline` flag exposed on the context and reports through `reportClientError`. Any other failure still means "not logged in".

Previously a bare `catch` treated both identically, so a user loading the app while briefly offline was shown the logged-out UI — and sent to a login form that could not work either — despite holding valid cookies.

### 10.2 `checkIsAdmin()` fails closed but distinguishes why ✅ Handled *(fixed)*

**Fixed.** `checkIsAdmin` now returns `{ isAdmin, indeterminate }`. **Both failure modes still deny admin** — a permission check must fail closed — but they are no longer the same event: a `401`/`403` is a real answer, while a network failure or `5xx` means we never found out. The indeterminate case is reported through `reportClientError` and exposed as `adminCheckFailed` on the context.

Previously a single transient blip silently downgraded a genuine admin for their whole session, with nothing logged.

### 10.3 Non-JSON response ✅ Handled

`safeJson` returns `null` on an unparseable body instead of throwing, so an unexpected HTML response degrades to a generic message rather than a second exception on top of the first.

### 10.4 Network failure during refresh is not misreported ✅ Handled

In `apiFetch`'s 401 branch, a refresh failure with `err.status === 0` is re-thrown as-is; anything else becomes `"Session expired."` An offline user is not told they were logged out.

### 10.5 ErrorBoundary does not catch async errors 🔵 Known limitation

React error boundaries only catch render and lifecycle errors. A rejected promise in an event handler or `useEffect` is unhandled. Most pages catch their own `apiFetch` rejections; nothing enforces it.

### 10.6 Frontend errors route through one reporter 🟡 Partially handled *(improved)*

**Partially addressed.** Client-side failures now route through a single `reportClientError` in `lib/api.js` — `ErrorBoundary.componentDidCatch`, the offline case (§10.1), and the indeterminate admin check (§10.2) all report through it.

🔴 **Still a gap:** that function writes to `console.error`. There is no error-tracking service configured in this project, so production frontend crashes remain invisible unless a user reports one. What changed is that wiring one up is now a one-file change instead of a hunt through every `catch` block, and the deliberate reporting sites are greppable.

### 10.7 Route guards are not a security boundary ✅ Handled (by the backend)

`ProtectedRoute`/`AdminRoute`/`UserRoute` read `AuthContext` and redirect. They are a UX affordance — every protected route's data comes from an endpoint that independently enforces authentication and ownership. Bypassing a guard reveals an empty shell, not data.

### 10.8 Only the first field error is shown 🟡 Partially handled

`extractMessage` prefers `data.detail`, else renders `"<first key>: <first message>"`. A submission failing three fields shows one. Client-side validation (`lib/validation.js`) usually surfaces the rest first — but a backend-only rule, such as the Have I Been Pwned check, can be hidden behind another field's error.

---

## 11. Rules for Developers

1. **Any read-modify-write on a counter or state field needs a row lock.** `verify_otp` shows the pattern (`select_for_update` inside `atomic`). §4.7 shows what unguarded looks like.
2. **Catch `IntegrityError` wherever a unique constraint can be raced** — first OAuth login, duplicate webhook. Use a nested `atomic()` if you might be inside an outer transaction.
3. **Set an explicit timeout on every outbound HTTP call.** `requests` has none by default; the project convention is 10–30s. §5.5 is what omitting it costs.
4. **Degrade visibly, not silently.** Compare §6.3 (sandbox announces itself with a zero-penalty issue) against §6.1 (missing Bandit yields a clean-looking report). Prefer the former.
5. **Validate raw query and body params.** Quota code clamps `tz_offset_minutes`; `RecentAnalysesView` clamps `limit`. Unvalidated filters silently return empty results instead of `400`.
6. **Never widen an enumeration-safe response.** The three OTP failures collapsing to one message (§2.5) is deliberate.
7. **Preserve status-code semantics.** CSRF is `403` on purpose (§1.2) — making it `401` creates a refresh loop. Ownership failures are `404`, not `403`.
8. **New quota or throttle state belongs in the database, not the cache**, until `CACHES` points at Redis (§8.5).
9. **Assume the process can die mid-request.** Synchronous work leaves rows in their pre-completion state and nothing reaps them (§4.6).

---

## Appendix: Open Gaps, Ranked by Impact

Twelve of the nineteen gaps previously listed here have been fixed (and one new one added by §3.13); each is
marked *(fixed)* in its section above. What remains:

| # | Gap | § | Why it matters |
|---|---|---|---|
| 1 | Frontend errors reach only `console.error` — no error-tracking service exists | 10.6 | Production UI crashes invisible unless a user reports one |
| 2 | Concurrent tab refresh logs a tab out | 1.1 | Unexplained logout for multi-tab users (session stays valid; a reload recovers) |
| 3 | Concurrent re-analysis is unguarded | 4.7 | Duplicated work; last writer wins. Harmless while analysis is deterministic |
| 4 | No AI circuit breaker | 5.6 | A down provider still costs its (now bounded) timeout on every request |
| 5 | No web/worker startup ordering on Render | 9.6 | Brief schema mismatch after a migrating deploy |
| 6 | `WebhookEvent` never pruned or cascaded | 7.5 | Unbounded growth of full JSON payloads |
| 7 | Only the first field error surfaces | 10.8 | Backend-only rules can be hidden behind another field's error |
| 8 | Budget degradation is not surfaced in the UI | 3.13 | A repo-context result can be partial with nothing on screen saying so |

Related limitations introduced or left behind by the fixes, documented in place
rather than listed as gaps:

- `/media/` is served by `django.views.static.serve` — safe but uncached (§9.2).
- Replaced avatars are never deleted, and now accumulate on a persistent disk (§9.2).
- Throttles fail open while Redis is unavailable, by design (§8.5).
- `Analysis` still has no `error` field; the upload path avoids needing one by
  rejecting bad input up front rather than persisting a failed row (§4.2).
- The repository-context request is bounded end to end by a monotonic budget
  (§3.12, §3.13), at the cost of returning quietly thinner results under load —
  `context_truncated` / `degraded_stages` are on the response but not yet
  rendered by the frontend.
