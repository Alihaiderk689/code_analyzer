# Operations Runbook

Operating, deploying, and recovering Code Analyzer in production. Written to be usable during an incident without prior knowledge of the codebase.

Every statement was verified against the current source. Three labels are used throughout and never mixed:

- **Implemented** — exists in the repository today.
- **Limitation** — a real constraint of the current implementation.
- **Gap** — something a reader might reasonably assume exists but does not. Never described as if it were implemented.

Companion docs: [ARCHITECTURE.md](ARCHITECTURE.md) · [ERROR_HANDLING.md](ERROR_HANDLING.md) · [SECURITY.md](SECURITY.md) · [SECURITY_CHECKLIST.md](SECURITY_CHECKLIST.md)

---

## 1. Production Readiness

| Area | Status | Detail |
|---|---|---|
| Application deploy pipeline | 🟢 Implemented | CI-gated, manual approval, `.github/workflows/ci.yml` |
| Automated tests in CI | 🟢 Implemented | Backend + frontend, blocking |
| Schema migrations on deploy | 🟡 Automatic, unguarded | `backend/entrypoint.sh`; no backup, no rollback (§5) |
| Startup config validation | 🟢 Implemented | `ENVIRONMENT`, `SECRET_KEY`, `ALLOWED_HOSTS` fail closed |
| Secret management | 🟢 Implemented | Dashboard env vars, `sync: false`, `.dockerignore` excludes `.env` |
| Health checks | 🟢 Implemented | `/api/health/` liveness + `/api/health/ready/` readiness probing the DB (§10) |
| Celery worker on Render | 🟢 Implemented | `ALLOWED_HOSTS` in the shared env group; worker boot verified (§13.1) |
| Long AI requests | 🟢 Implemented | Chain budget 3×30s, gunicorn `--timeout 120` (§13.2); repo-context capped at 90s total (§13.8) |
| Avatar storage in production | 🟢 Implemented | 1GB Render disk + unconditional `/media/` route (§13.3) |
| Rate-limit accuracy | 🟢 Implemented | Shared Redis cache; fails open if Redis is down (§13.4) |
| Database outage response | 🟢 Implemented | `OperationalError` → `503`, no blind retries (§13.5) |
| Security scan completeness | 🟢 Implemented | `scan_complete` / `scanners_unavailable` in the report (§13.6) |
| Monitoring / alerting | 🔴 None | No APM, error tracking, uptime check, or alerts (§11) |
| Backups | 🔴 None in repo | Supabase-managed only; unverified here (§6) |
| Rollback | 🟡 Manual only | Dashboard redeploy; migrations do not roll back (§5) |
| Log retention / search | 🔴 Platform only | No aggregation; retention = plan default (§10) |
| Worker/queue visibility | 🔴 None | No Flower, queue metric, DLQ, or failure alert (§7) |

**Bottom line:** the core web application is production-deployable, and the functional defects previously listed here are fixed and test-covered. What remains missing is the operational safety net — no monitoring, no alerting, no verified backups, no automated rollback. Failures are still discovered by users reporting them (§11).

## 2. Architecture & Required Services

Three environments. They differ in ways that matter during an incident.

| | Local dev | Docker Compose | Render + Vercel (production) |
|---|---|---|---|
| Frontend | Vite `:5173` | nginx, `${FRONTEND_PORT:-80}` | Vercel static |
| Backend | `runserver :8000` | gunicorn, **no host port** | Render web (Docker) |
| Database | Local PG (`DATABASE_URL_DEV`) | `postgres:16-alpine` | **External Supabase** (`DATABASE_URL_PROD`) |
| Redis | Optional | `redis:7-alpine` | Render `keyvalue` |
| Celery | Manual | `celery_worker` service | Render worker (**broken**, §13.1) |
| Origins | Two | **One** (nginx proxies `/api/`) | Two |
| `/media/` | Django (DEBUG only) | nginx from shared volume | **Nothing** (§13.3) |

`render.yaml` defines three services: `code-analyzer-backend` (web), `code-analyzer-celery-worker` (`dockerCommand: celery -A config worker --loglevel=info`), `code-analyzer-redis` (`type: keyvalue`). All `plan: starter`. **No database service** — Postgres is external.

`render.yaml`'s own header warns that Render's Blueprint schema (particularly `keyvalue` field names) has changed over time and this file was never validated against Render's live API.

### Service dependency matrix

| Service | Required for | If it fails | Detection |
|---|---|---|---|
| PostgreSQL (Supabase) | Everything | Total outage | ❌ Health check stays green (§10) |
| Backend web (Render) | Everything | Total outage | ✅ Render restarts on health-check failure |
| Frontend (Vercel) | UI only | API still reachable | ❌ None |
| Redis | Celery transport only | Webhooks queue and never run | ❌ None |
| Celery worker | PR review, repo indexing | Same — silent, `202` still returned | ❌ None |
| Brevo | **OTP + password reset** | **Signup fully blocked** | ❌ User reports |
| Groq → Gemini → OpenRouter | AI features | `503` only if all three fail | ❌ None |
| Google OAuth | Google sign-in | That path `400`s | ❌ None |
| GitHub API | PR review, repo browsing | `401`/`429`/`503` | ❌ None |

**Redis and Celery are not required for auth, analysis, AI, or chat.** They serve GitHub webhook processing and repository indexing (§7). Email does not use them — Brevo is called synchronously in-request (§8) and does not replace them.

**Brevo is the highest-impact dependency after the database**, because registration cannot complete without it.

## 3. Startup & Shutdown

`backend/entrypoint.sh` is shared by every container built from the backend image:

```sh
if [ "$1" = "gunicorn" ]; then
    python manage.py migrate --noinput
    python manage.py collectstatic --noinput
fi
exec "$@"
```

**Only the web container migrates.** The worker runs `celery ...`, so `$1` is not `gunicorn` and it skips both — which prevents two containers racing the same migration.

Compose enforces ordering: `db`/`redis` healthchecks → `backend` → `celery_worker` (gates on `backend`'s healthcheck, which passes only after migrations).

> **Gap — no ordering on Render.** Render has no `depends_on`. The worker can start before the web container finishes migrating, so just after a schema-changing deploy it may briefly run against the old schema. Currently masked by §13.1.

Shutdown relies entirely on Gunicorn's and Celery's own `SIGTERM` handling; there is no custom shutdown code. `CELERY_TASK_TIME_LIMIT = 300` caps any task at five minutes.

## 4. Deployment Procedure

### Pipeline (`.github/workflows/ci.yml`)

Triggers: `pull_request` on `main`/`dev`, `push` on `main` only. `push` excludes `dev` on purpose — with it listed, a `dev`→`main` PR ran every job twice (once for the branch push, once for the PR), showing eight checks for three jobs. A `concurrency` group cancels superseded in-flight runs per branch/PR, but never on `main`, where cancelling mid-rollout is worse than letting a superseded deploy finish.

1. **`backend`** — Postgres 16 service container; `pip install`, `manage.py check`, `makemigrations --check --dry-run`, `manage.py test`, `pip-audit` *(advisory, `continue-on-error: true`)*.
2. **`frontend`** — `npm ci`, `npm run lint`, `npm test`, `npm run build`, `npm audit --audit-level=high` *(advisory)*.
3. **`deploy`** — `needs: [backend, frontend]`, `if: github.ref == 'refs/heads/main' && github.event_name == 'push'`, `environment: production`. It fires all three deploy hooks, then **polls each provider until its deploy reaches a terminal state**:

> **`environment: production` is not a gate today.** It pauses for a human only when that GitHub Environment has required reviewers, and it has none — deploys run automatically once tests pass, which is the intent. The key is kept for production-scoped secrets and the deploy audit trail. Do not read its presence as proof that something is waiting for approval; a run that reached the trigger steps was never waiting.

| Step | Waits on | Terminal states |
| --- | --- | --- |
| Trigger Vercel deploy | — | bare POST — runs **first** so nothing on the Render side can block the frontend |
| Trigger Render deploys | — | POSTs the web **and** worker hooks; reads `.deploy.id` from each, fails if absent |
| Wait for Render deploys | `GET /v1/services/{srv}/deploys/{dep}`, per service | pass `live`; fail `build_failed`, `update_failed`, `pre_deploy_failed`, `canceled`, `deactivated` |
| Wait for Vercel deploy | `GET /v6/deployments`, matched on `meta.githubCommitSha == $GITHUB_SHA` | pass `READY`; fail `ERROR`, `CANCELED` |

All three hooks fire before any wait, so the builds run concurrently. Each wait polls every 15s with a 15-minute deadline and tolerates a failed poll (a blip is not a failed deploy). The Vercel wait carries `if: !cancelled() && steps.vercel.outcome == 'success'` so a bad Render deploy does not hide the frontend's outcome.

> **Why the polling exists.** A deploy hook returns as soon as the build is *queued*. The job previously ended at the two `curl`s, so it went green on a build that subsequently failed — production could sit frozen behind a wall of green checkmarks with nothing in CI to say so.

> **Why the worker gets its own hook.** `autoDeploy: false` applies to both Render services, and one hook only deploys one service. Without a second hook the worker would never be redeployed — it would drift onto an older commit than the web container it shares an image with. The job treats a missing `RENDER_WORKER_DEPLOY_HOOK_URL` as a hard error rather than skipping it, because silently not deploying a service is the failure this whole job exists to prevent.

Actions secrets required beyond the two hook URLs: `RENDER_WORKER_DEPLOY_HOOK_URL`, `RENDER_API_KEY`, `VERCEL_TOKEN`, `VERCEL_PROJECT_ID`, and `VERCEL_TEAM_ID` (leave unset unless the Vercel project sits under a team). **Until they are set the deploy job fails**, at the Render trigger or the first wait.

### Deploys are gated, not automatic

`render.yaml` sets `autoDeploy: false` on both the web service and the worker; Vercel's Git auto-deploy is off in its dashboard. **The `deploy` job is the only path to production.**

> **This was not always true, and the asymmetry caused a real outage-shaped bug.** Render's `autoDeploy` defaults to *true* and was never overridden, so the backend redeployed on every push to `main` — on push, not on "CI passed", meaning a merge that broke the test suite shipped anyway and the red X arrived minutes later. Vercel's auto-deploy had been turned off as instructed, so the frontend depended entirely on an approval nobody was giving and sat months behind. The result was a current backend serving a stale frontend against an API contract that had moved on under it. If you ever re-enable auto-deploy on one provider, enable it on both or neither.

`autoDeploy: false` binds only services Render manages through the Blueprint. A service created by hand in the dashboard also needs **Settings → Auto-Deploy → No**; treat the key as the statement of intent.

Migrations run inside the Render container start — after CI, with no further gate.

### Pre-deployment checklist

- [ ] CI green on the branch (backend `check`/`makemigrations --check`/`test`; frontend `lint`/`test`/`build`).
- [ ] `pip-audit` and `npm audit` output **read** — they are advisory, so green CI ≠ clean audit.
- [ ] New migration reviewed for backward compatibility — it runs unattended with no backup (§5).
- [ ] Supabase backup confirmed to exist if the release migrates.
- [ ] Any new env var added to **both** the Render dashboard and `render.yaml`'s shared group — and confirmed present on **both** web and worker (§13.1).
- [ ] Frontend `VITE_API_BASE_URL` / `VITE_GOOGLE_CLIENT_ID` correct — compiled in at build time, unchangeable at runtime.

### Post-deployment verification

- [ ] Render web logs show `Applying database migrations...` then a clean gunicorn start.
- [ ] Render **worker** logs — confirm it actually started (§13.1) and is consuming.
- [ ] `curl -s https://<backend-host>/api/health/` → `{"status": "ok"}` — proves only that Django is up (§10).
- [ ] Log in via the real frontend and perform a mutating action (proves cookies + CORS + CSRF together).
- [ ] Register a throwaway account: OTP email arrives, code verifies.
- [ ] Run one analysis and one AI action — watch for a `502` past ~30s (§13.2).
- [ ] Deep-link a client-side route on Vercel (proves the `vercel.json` rewrite).
- [ ] `grep unhandled_exception` in web logs.
- [ ] If GitHub changed: open a test PR, confirm a review comment appears.

## 5. Rollback & Migrations

> **Gap — no automated rollback exists.** No versioned image pinning, no blue/green, no automated revert. Rollback means the Render or Vercel dashboard's "redeploy previous deploy", or reverting the commit on `main` and re-running the pipeline.

**Migrations are forward-only in practice.** `entrypoint.sh` runs `migrate` on every web start; nothing reverses it. Rolling application code back while the database stays migrated can leave the two incompatible.

Manual downgrade exists but is untested in this project and destructive for anything that dropped a column:

```bash
python manage.py migrate <app> <previous_migration>
```

> **Gap — no migration safety net.** No pre-deploy snapshot, no dry run, no automatic rollback on failure. A failed migration aborts `entrypoint.sh` under `set -e` before `exec`, so the container crash-loops — it fails closed rather than serving a half-migrated schema, but recovery is entirely manual.

**Frontend and backend deploy independently** from one workflow. If one hook succeeds and the other fails, they are mismatched with no detection.

**Safer migration practice for this setup** (convention, not enforced): make schema changes additive and deploy in two releases — add the column/table first, ship code that uses it second — so a code rollback never faces a schema it cannot read.

## 6. Backups & Data Loss

> **Gap — no backup or recovery mechanism exists in this repository.** No backup script, no scheduled dump, no restore procedure, no recovery test. Any protection comes from Supabase's managed backups, configured outside this repo and unverified here.

| Data | Store | Recoverable? |
|---|---|---|
| Users, analyses, chat, GitHub records | Supabase Postgres | Only via Supabase's own backups (external) |
| Uploaded avatars | Container filesystem | **No** — ephemeral, §13.3 |
| Celery task state | Redis | Not needed — outcomes are DB rows |
| Static files | Rebuilt by `collectstatic` each deploy | Yes, automatically |

**Losing `GITHUB_TOKEN_ENCRYPTION_KEY`** makes every stored GitHub token permanently undecryptable (`TokenDecryptionError`). Not fatal — tokens are reissuable — but every connected user must reconnect. See SECURITY.md §5.2.

## 7. Celery & Redis Operations

**Role, precisely.** Redis is Celery's broker and result backend — nothing else. Celery runs work that cannot fit in a request:

| Task (`github_integration/tasks.py`) | Queued from | Why it must be async |
|---|---|---|
| `process_pull_request_webhook` | `webhook_views.py:53` | GitHub redelivers if not acked in seconds; analysis is many API calls long |
| `process_push_webhook` | `webhook_views.py:55` | Same deadline; re-queues indexing |
| `build_repository_index` | `repository_views.py:230`, `repository_service.py:62`, `tasks.py:212` | One GitHub API call **per file**, capped at `GITHUB_MAX_INDEXED_FILES` (300) |

**There is no Celery Beat and no scheduled tasks** — verified: no `beat_schedule`, no beat service in `render.yaml` or `docker-compose.yml`. All work is event-driven.

Task outcomes persist as `WebhookEvent`, `PullRequestAnalysis`, and `RepositoryIndex` rows — those are the source of truth, not Celery's result backend.

### Retry policy (implemented)

`max_retries = 3`, per exception type:

| Condition | Behavior |
|---|---|
| `GitHubAuthError` | **Not retried.** Sets `integration.token_invalid = True`, marks analysis `FAILED` |
| `GitHubRateLimitError` | Retries at `countdown = reset_at - now` (exact reset), else 60s |
| `GitHubAPIError` | Exponential backoff: 30s, 60s, 120s |
| Retries exhausted | `_mark_permanently_failed` → status `FAILED`, message in `.error`, event marked processed |
| Any other exception | Logged with traceback, marked permanently failed — never left `RUNNING` |

### Diagnosing

```bash
# Is the worker alive and consuming?
#   Render: dashboard -> code-analyzer-celery-worker -> Logs
docker compose logs -f celery_worker      # Compose
```

```python
# manage.py shell — queued but never processed (worker down or not consuming)
WebhookEvent.objects.filter(processed=False).order_by('-created_at')[:20]

# Ran and gave up — .error holds the reason
PullRequestAnalysis.objects.filter(status='failed').order_by('-updated_at')[:20]

# Indexing failures
RepositoryIndex.objects.filter(status='failed').values('repository__full_name', 'error')
```

> **Gap — no queue visibility.** No Flower, no queue-depth metric, no dead-letter queue, no alert on task failure. The database queries above are the only diagnostic.

> **Gap — no worker liveness signal.** Render worker services support no `healthCheckPath`, and `/api/health/` does not check whether a worker is consuming. A dead worker is invisible until someone notices reviews stopped.

> **Limitation — Redis is not wired to Django's cache.** There is no `CACHES` setting, so throttle counters use per-process `LocMemCache` (§13.4). Redis is already provisioned; pointing `CACHES` at it is the fix.

## 8. Brevo / Email Operations

**Synchronous and in-request** — email does *not* use Celery. `accounts/views.py:76` calls `send_otp_email()` directly inside `transaction.atomic()`; `accounts/brevo_client.py` posts to `https://api.brevo.com/v3/smtp/email` with `REQUEST_TIMEOUT_SECONDS = 15`.

**Registration is transactional.** If Brevo fails, `BrevoAPIError` propagates, the `User` row is rolled back, and the client gets `503 "Email service is currently unavailable. Please try again."` — no account is stranded with an undelivered code.

**Consequence: while Brevo is down, nobody can sign up.** Password reset also fails, but `forgot-password` returns its generic success message regardless (enumeration protection), so a user sees no error and simply never receives the email.

> **Gap — no retry and no queue for email.** A transient Brevo blip is a failed signup the user must retry manually. This is a direct consequence of the transactional design, which is deliberate — queueing would decouple delivery from account creation and require handling "account exists, code never arrived."

**Diagnosing:**

```bash
grep 'brevo_api' <logs>          # brevo_api.network_error | brevo_api.error_response
grep 'accounts.otp_email_failed' <logs>
```

`brevo_api.error_response` logs `status_code` and the response body. Then check the Brevo dashboard for quota exhaustion and sender verification — `BREVO_SENDER_EMAIL` must be a verified sender.

**"User got 201 but no email"** is not a backend failure — the send succeeded. Check spam, then Brevo's delivery log.

## 9. AI Provider Operations

`ai/client.py::_call_with_fallback` tries **Groq → Gemini → OpenRouter**, catching any exception per provider, logging, and continuing. If all three fail, the last exception is re-raised and the call site returns `503`.

| Provider | Timeout | Retries |
|---|---|---|
| Groq | **none set** — SDK default 60s read | **SDK default: 2** |
| Gemini | `timeout=30` | none |
| OpenRouter | `timeout=30` | none |

**Degraded-but-successful paths (implemented):** `ai_security_service` falls back to scanner-provided text if enrichment fails — the security report still returns. `_parse_suggestions` / `_parse_refactor_response` salvage malformed model output rather than erroring.

**Diagnosing:**

```bash
grep 'AI provider' <logs>     # "AI provider groq failed, falling back to next provider."
grep 'WORKER TIMEOUT' <logs>  # NOT a provider failure — this is §13.2
```

If users report AI failures but the logs show no `AI provider ... failed` lines, the request is being killed by gunicorn before the chain finishes (§13.2), not failing at the provider.

> **Limitation — worst-case chain latency exceeds every timeout above it.** Groq (~3×60s with SDK retries) + Gemini (30s) + OpenRouter (30s) ≈ four minutes, against gunicorn's 30s.

> **Gap — no circuit breaker.** A hard-down primary is retried on every request; no memory of recent failures.

## 10. Health Checks & Logs

| Where | Check |
|---|---|
| Render web | `healthCheckPath: /api/health/` |
| Compose `backend` | `urllib.request.urlopen('http://localhost:8000/api/health/')`, 10s interval, 15s start period |
| Compose `db` | `pg_isready` |
| Compose `redis` | `redis-cli ping` |

There are **two** endpoints, and the distinction matters during an incident:

| Endpoint | Checks | Returns | Wired to |
|---|---|---|---|
| `/api/health/` | Nothing — process liveness only | Always `200` | `render.yaml` `healthCheckPath` |
| `/api/health/ready/` | Database (`SELECT 1`) + cache | `503` if DB is down; `200 degraded` if only the cache is down | Nothing — monitoring and manual use |

**Liveness is deliberately dumb.** It is the platform's restart trigger, and a restart cannot fix an unreachable database — probing dependencies there would turn a brief Postgres blip into a restart loop on top of the outage.

**Readiness is the truthful one.** During an incident, `curl https://<host>/api/health/ready/` and read `checks.database.ok`. A `200` from `/api/health/` alone still means only "the Python process is alive".

Cache failure reports `degraded` rather than unready: throttles fail open (§13.4), so requests still succeed without Redis.

### Log locations

| Environment | Where |
|---|---|
| Render web | Dashboard → `code-analyzer-backend` → Logs |
| Render worker | Dashboard → `code-analyzer-celery-worker` → Logs |
| Vercel | Dashboard → Deployments → Build logs |
| Compose | `docker compose logs -f backend` / `celery_worker` |
| Local | `runserver` terminal |

Config: `config/settings.py::LOGGING` — one `console` handler, `core.logging_formatters.StructuredFormatter` (appends `extra={...}` as trailing JSON). Root `INFO`; `django` pinned `WARNING`; `github_integration` `INFO`. stdout/stderr only; `PYTHONUNBUFFERED=1`.

### Searchable event names (complete inventory)

Stable identifiers — grep these directly.

**Core:** `unhandled_exception` · `analysis_run_failed`
**Email:** `accounts.otp_email_failed` · `brevo_api.network_error` · `brevo_api.error_response`
**GitHub API:** `github_api.network_error` · `github_api.error_response` · `github_api.request_failed` · `github_client.tree_truncated`
**Webhooks:** `github_webhook.invalid_signature` · `github_webhook.duplicate_delivery` · `github_webhook.received`
**OAuth:** `github_oauth.invalid_state` · `github_oauth.callback_failed` · `github_oauth.not_configured` · `github_oauth.user_denied_access` · `github_oauth.authorize_url_issued` · `github_oauth.login_authorize_url_issued` · `github_oauth.disconnected`
**Tasks:** `github_task.auth_failed` · `github_task.rate_limited` · `github_task.rate_limit_retries_exhausted` · `github_task.github_api_error` · `github_task.unexpected_error` · `github_task.webhook_event_missing` · `github_task.already_analyzed` · `github_task.repository_not_monitored` · `github_task.repository_not_monitored_for_indexing` · `github_task.repository_not_monitored_for_push` · `github_task.index_auth_failed` · `github_task.index_github_api_error` · `github_task.index_rate_limit_retries_exhausted` · `github_task.index_unexpected_error`
**Analysis/indexing:** `github_pr_analysis.file_fetch_failed` · `github_pr_analysis.file_skipped` · `repository_index.build_failed` · `repository_index.file_fetch_failed` · `github_repository.deselected`

> **Gap — no log aggregation or retention policy.** Logs live only in the Render/Vercel dashboards under plan-default retention. No shipping, no persistent search. **Capture logs during an incident before they age out.**

> **Gap — no correlation ID.** A production `500` returns a generic message with nothing linking it to its traceback. Match on timestamp and path.

## 11. Monitoring & Alerting

> **Gap — there is no monitoring or alerting of any kind.** Verified absent across the whole repository: no APM, no error tracking (no Sentry or equivalent), no uptime monitoring, no metrics, no dashboards, no log alerts, no on-call routing, no deploy-failure notification.

What exists is passive:

- Render restarts the web service when `/api/health/` fails — but that check verifies nothing (§10).
- Compose healthchecks gate startup ordering only; they never alert.
- `frontend/src/components/ErrorBoundary.jsx` writes frontend crashes to the browser console, where nobody sees them.

**In practice, production problems are discovered when users report them.**

## 12. Gunicorn & Render Runtime

```dockerfile
# backend/Dockerfile
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
```

`--timeout 120`, no `--worker-class`, so workers are **sync**. The timeout sits above the bounded AI chain (3 × `AI_REQUEST_TIMEOUT_SECONDS`, default 90s) with headroom — the two must be changed together (§13.2).

**Concurrency is 3.** Sync workers serve one request each. Because AI calls are synchronous and in-request, three concurrent AI requests saturate the service, and a slow AI call starves unrelated traffic — including registration.

`WORKER TIMEOUT` in the logs means a request exceeded **120s** and its worker was killed; the client saw a `502`.

**Worst-case synchronous request paths**, measured against that 120s:

| Path | Budget | Fits? |
|---|---|---|
| AI only (suggestions / explanation / refactor / chat) | 3 × 30s = **90s** | yes |
| Security scan (Bandit 20s **then** the full AI chain) | **110s** | yes, with 10s to spare |
| Repo-context analyze (whole request) | `GITHUB_CONTEXT_REQUEST_BUDGET_SECONDS` = **90s**, a true total | yes, with 30s to spare (§13.8) |
| ├ its GitHub fetch phase | `GITHUB_CONTEXT_FETCH_BUDGET_SECONDS` = **45s**, a share of the 90s, not an addition | yes (§13.7) |
| └ its per-file analysis (sandbox 5s + Bandit 20s + AI 90s, ×7 files) | unbounded before §13.8 at **805s**; now inside the 90s | yes (§13.8) |

Two caveats on "budget". First, `requests` and `httpx` timeouts bound each I/O
phase (connect, and the gap between received chunks), **not** total wall-clock
duration — a server trickling bytes slowly can exceed its budget without ever
stalling long enough to trip the timeout. Second, most rows above are sums of
independent per-call worst cases; all of them maxing out at once is unlikely.

**The exception is the repo-context request**, the only path in the codebase
enforcing real totals: `core/execution_budget.py` takes a `time.monotonic()`
deadline once per request and every expensive stage — GitHub fetches, the
sandboxed runtime check, Bandit, and each leg of the AI fallback chain — is
checked against it *and* has its own timeout clamped to what remains (§13.7,
§13.8). Even that is not immune to the trickle caveat: a single HTTP response
arriving in slow chunks keeps resetting its read timeout. What is eliminated is
the *accumulation* — no number of calls, files or providers can push the request
past its total. Everywhere else, **gunicorn's `--timeout` remains the only hard
wall-clock bound on a request.**

Under Compose, `frontend/nginx.conf` sets `proxy_read_timeout 120s`, now matching gunicorn's. On Render there is no nginx; Render applies its own platform timeout.

Containers run as non-root (`appuser`, `backend/Dockerfile`). The Compose backend publishes **no host port** — nginx is the only ingress.

## 13. Previously Known Issues — Now Fixed

All six are fixed and test-covered. Kept here because they explain why the
configuration looks the way it does, and what to re-check if it regresses.

### 13.1 Celery worker boot on Render ✅

`ALLOWED_HOSTS` now lives in `render.yaml`'s `code-analyzer-shared` env group, so
the worker inherits it. It was previously set only on the web service, so the
worker started with an empty list, failed `core/settings_validation.py`, and
crash-looped — silently, since webhooks still returned `202`.

**Validation was not weakened.** Guarded by `core.tests.RenderDeploymentConfigTests`,
which asserts every non-Redis service inherits the shared group.

*Re-check if:* PR reviews stop appearing after an env-var change.

### 13.2 AI request timeout ✅

Two changes that must stay in step:

- All three providers share `settings.AI_REQUEST_TIMEOUT_SECONDS` (default 30s),
  and the Groq client is built with `max_retries=0` so the SDK cannot multiply
  its own timeout. Worst case: **3 × 30 = 90s**.
- Gunicorn runs `--timeout 120`.

**Raising one without the other reintroduces the bug** — a higher gunicorn
timeout with unbounded providers just lets one request hold 1 of 3 workers for
minutes. `core.tests.RenderDeploymentConfigTests` asserts the ordering.

### 13.3 Avatar persistence ✅

`render.yaml` mounts a 1GB disk at `/app/media` (web service only — Render disks
cannot be shared), and `config/urls.py` registers `/media/` unconditionally.

**Worth knowing:** `django.conf.urls.static.static()` returns `[]` when
`DEBUG=False` — that helper *is* the DEBUG gate — so removing the surrounding
`if` would have changed nothing. The route is registered directly with
`re_path(..., django.views.static.serve)`.

*Limitation:* `serve` has no caching layer, and replaced avatars are never
deleted, so orphans now accumulate on the disk. Watch disk usage.

### 13.4 Shared throttle cache ✅

`CACHES` points at the Celery Redis (`CACHE_REDIS_URL`, defaulting to
`CELERY_BROKER_URL`). Throttle counters are shared across workers instead of
being per-process.

**Deliberate trade-off:** the backend is `core.cache.ResilientRedisCache`, which
treats Redis errors as a cache miss — so while Redis is down, **throttles fail
open** rather than 500ing every endpoint. Safe because the real brute-force
protections are database-backed: the OTP attempt lockout (`select_for_update`)
and the daily quotas. Degradation is logged as `cache.redis_unavailable` and
surfaces in `/api/health/ready/`.

*Falls back to `LocMemCache`* when no Redis URL is set, and always under
`manage.py test`.

### 13.5 Database outage response ✅

`core/exceptions.py` maps `OperationalError`/`InterfaceError` to **`503`** with a
retryable message, logged as `database_unavailable`. Previously a generic `500`.

**No automatic retry was added, deliberately** — retrying into an exhausted
pooler turns a blip into an outage.

### 13.6 Security scan completeness ✅

Two parts:

1. Reports now carry `scan_complete` and `scanners_unavailable[]`. A scanner
   that cannot run no longer renders as a clean result. The score is unchanged
   by unavailability — a scanner failing is not evidence of vulnerabilities.
2. **Root cause:** `bandit_service` invoked a bare `'bandit'`, which is not on
   `PATH` when Python is run by absolute path. It now runs
   `sys.executable -m bandit`. This was live in the local test environment and
   was the cause of 9 long-standing test failures.
3. Those two fields reach the **Security Analysis Mode** endpoint, which
   serializes the whole report. The other three consumers — PR review, the
   single-file check and the context check — go through
   `pr_analysis_service._analyze_file_content`, whose contract is a flat issue
   list; it read only `vulnerabilities` off the report and dropped the rest, so
   a Bandit that was missing, hung, produced unparsable output or was skipped
   for budget still rendered as "no security issues found" on all three. Each
   entry in `scanners_unavailable[]` is now folded into that list as a
   `scanner_unavailable` issue at severity `info` — outside the `Severity` enum,
   so `SEVERITY_PENALTIES.get(..., 0)` keeps point 1's "does not move the score"
   rule intact. It carries `line: None`, so `comment_service` routes it to the
   PR comment's "Additional findings" section, and `_build_summary` names the
   affected file count in the headline rather than leaving it to be inferred.

*Re-check if:* `scan_complete` starts coming back `false` in production — grep
for `Bandit is not installed`. On the PR/file paths the equivalent signal is a
`scanner_unavailable` issue in `FileAnalysis.issues` /
`RepositoryFileCheck.issues` / `RepositoryContextCheck.issues`.

### 13.7 Repo-context fetch budget ✅

`RepositoryFileContextAnalyzeView` makes up to 11 GitHub calls in one request —
the target file, the repo tree and a `settings.py` lookup, up to 6 dependency-
graph neighbors, and a second lazy `settings.py` lookup. Each had a 15s
per-request timeout and nothing bounded the sum: **165s** against gunicorn's
120s, so a degraded GitHub turned the request into a `502`.

`github_integration/services/fetch_budget.py` adds the missing total. One
`FetchBudget` is created per context analysis and passed to every `GitHubClient`
in the phase, holding a `time.monotonic()` deadline of
`GITHUB_CONTEXT_FETCH_BUDGET_SECONDS` (**default 45s**). Two enforcement points:

- `PRAnalysisService.analyze_file_with_context` checks the budget **before**
  each neighbor fetch, so no further GitHub call is attempted once it is spent.
- `GitHubClient._request` clamps each call's `timeout` to `min(15s, remaining)`.
  That clamp is what makes the total an actual bound rather than a check that a
  single slow call can overshoot by 15s.

**45s was chosen against the 120s timeout**: it leaves 75s for the analysis that
follows, covering Bandit's hard 20s cap plus one `AI_REQUEST_TIMEOUT_SECONDS`
leg (30s) with ~25s of margin. A healthy GitHub answers all 11 calls in a few
seconds, so the budget only bites when GitHub is degraded.

**Exhaustion is not a failure.** The already-analyzed primary file and whatever
neighbors were collected are returned and persisted, with
`context_truncated: true` and `context_truncated_reason: "fetch_budget_exhausted"`
on the response *and* on the `RepositoryContextCheck` row (so the free
same-path-again-today cached response reports it too). A
`github_context_check.truncated` warning is logged with how many neighbors made
it. `FetchBudgetExceeded` is deliberately **not** a `GitHubAPIError` subclass, so
it cannot be swallowed by the existing per-file `except GitHubAPIError` handlers
— auth errors (401 → reconnect), rate limits (429 → `reset_at`) and genuine
fetch failures keep their existing, distinct handling.

Only the context path passes a budget. PR review, OAuth, repo listing and index
building construct `GitHubClient` without one and behave exactly as before.

Guarded by `github_integration.tests.test_fetch_budget`, including an arithmetic
check that the bounded worst case stays ≥20s under 120s.

*Re-check if:* `github_fetch_budget.exhausted` starts appearing frequently in
logs — that means GitHub latency, not a misconfigured budget, and the answer is
to look at GitHub's status rather than to raise the number.

### 13.8 Repo-context request budget ✅

§13.7 bounded the GitHub fetching. It did not bound what runs *after* each
fetch, and that is the larger cost: `analyze_file_with_context` runs the full
per-file pipeline on the primary file **and each of up to 6 neighbors**, and
each run has three stages that can block on wall clock:

| Stage | Ceiling | Applies to |
|---|---|---|
| `sandbox.run_python` (runtime error detection) | `TIMEOUT_SECONDS` = 5s | Python, macOS hosts only |
| `BanditScanner.scan` | `BANDIT_TIMEOUT_SECONDS` = 20s | Python only |
| AI fallback chain (`_call_with_fallback`) | 3 × `AI_REQUEST_TIMEOUT_SECONDS` = 90s | any file with findings |

115s per file × 7 files = **805s** with nothing bounding the sum. The sandbox
stage is easy to miss — it is three call frames below `analyze_code` and only
costs anything on a macOS host, where `sandbox.is_available()` is true.

`core/execution_budget.py` adds the request-wide bound. One `RequestBudget` is
created per context analysis, holding a `time.monotonic()` deadline of
`GITHUB_CONTEXT_REQUEST_BUDGET_SECONDS` (**default 90s**), and is threaded to
every stage above. `FetchBudget` (§13.7) is now a subclass of the same
`ExecutionBudget` and is constructed as
`min(GITHUB_CONTEXT_FETCH_BUDGET_SECONDS, request_budget.remaining())` — a
*share* of the 90s, never an addition to it.

**Stages are skipped, not squeezed.** Each has a minimum viable slice
(`sandbox.TIMEOUT_SECONDS`, `MIN_BANDIT_SLICE_SECONDS` = 5s,
`MIN_AI_SLICE_SECONDS` = 8s, `MIN_RELATED_FILE_BUDGET_SECONDS` = 12s for
starting another neighbor). Below it the stage does not start. Squeezing Bandit
into 0.5s would get it killed and reported as `reason='timeout'` — a lie about
the scanner; refusing to start it and saying `reason='budget_exhausted'` is the
truth. Above the minimum the stage runs with its timeout clamped to whatever
remains, which is what makes the total an actual bound.

**90s was chosen** to leave a 30s margin (25%) under the 120s timeout for the DB
writes, serialization and CPU-bound static analysis that follow and are too
short to budget individually. Each individual stage ceiling (5/20/30s) fits
inside 90s, so a healthy request never degrades from the first file.

**Nothing is thrown away when it runs out.** The primary file keeps its free
static analysis and every scanner finding; already-analyzed neighbors are kept;
findings whose AI prose was skipped fall back to scanner-written text. The
response and the `RepositoryContextCheck` row carry
`context_truncated_reason: "request_budget_exhausted"` and `degraded_stages`
(`runtime_check` / `bandit` / `ai_enrichment` / `related_files`) — stored, not
derived, so the free same-path-again-today cached response reports them too.

**Four failure modes stay distinct.** `BudgetExceeded` is a standalone
exception type, unrelated to `GitHubAPIError` or any provider/scanner error, and
is re-raised past every broad `except Exception` handler on the way out:

| What happened | How it reports |
|---|---|
| Request budget exhausted | `request_budget.exhausted` log; `context_truncated_reason=request_budget_exhausted`; `degraded_stages` |
| GitHub fetch budget exhausted | `github_fetch_budget.exhausted`; `context_truncated_reason=fetch_budget_exhausted` (§13.7) |
| An AI provider is down | `AI provider X failed, falling back…`; chain continues in the same order; scanner text on total failure |
| Bandit genuinely hung | `ScannerUnavailable(reason='timeout')` — *not* `'budget_exhausted'` |

`degraded_stages` is deliberately separate from `context_truncated`: a check can
cover every neighbor (not truncated) and still have skipped the AI prose.

Only this path passes a budget. PR review, the plain single-file check, the
paste/upload analysis, the security-scan endpoint and both chat surfaces call
the same functions with `budget=None` and behave exactly as before — including
the AI fallback order, which is never reordered, only stopped early.

Guarded by `core.tests_execution_budget` (24 tests) and
`github_integration.tests.test_request_budget` (15, including the arithmetic
guards against 120s).

*Re-check if:* `request_budget.exhausted` appears in logs regularly. That means
real analysis latency — a slow AI provider or a large file — not a
misconfigured budget. Raising the number without also raising gunicorn's
`--timeout` reintroduces the `502`.

## 14. Incident Runbooks

### Triage

```
Frontend loads?            no -> Vercel: deployment/build logs
  | yes
/api/health/ 200?          no -> Render web: logs, crash loop, env vars  -> RB-1
  | yes                          (green proves only that Python is alive)
Login works?               no -> RB-3
  | yes
Feature-specific?             -> RB-4 .. RB-8
```

### RB-1 — Total outage / backend won't start

1. Render → `code-analyzer-backend` → Logs. A crash loop is almost always a boot-time failure; the exception names the cause.
2. Check in order: `SECRET_KEY` missing (`KeyError`) → `ENVIRONMENT` typo (`ImproperlyConfigured`) → `ALLOWED_HOSTS` empty or `'*'` (`ImproperlyConfigured`) → failed migration (`entrypoint.sh` aborts under `set -e` before `exec`).
3. A failed migration means the schema is partially applied. **There is no automated rollback (§5).** Assess with `python manage.py showmigrations`.

### RB-2 — Every request returns 400

`ALLOWED_HOSTS` does not include the hostname actually being requested. Check the Render env var against the real host (including any custom domain). Fails closed by design.

### RB-3 — Nobody can log in / everyone logged out

1. Confirm `ENVIRONMENT=production` is really set — it drives cookie `Secure`/`SameSite`. Wrong value → cookies rejected by the browser.
2. Confirm the frontend origin is in `CORS_ALLOWED_ORIGINS` (`CSRF_TRUSTED_ORIGINS` is derived from it).
3. If frontend and backend are on different subdomains, confirm `COOKIE_DOMAIN` (leading dot).
4. Browser devtools → Application → Cookies: is `csrftoken` readable? Are `access_token`/`refresh_token` being sent?
5. "Logged out after ~15 min" = refresh failing. Check `/api/auth/refresh/` in the Network tab.

### RB-4 — Signups failing

1. `grep brevo_api <logs>` → `network_error` (Brevo unreachable) or `error_response` (has `status_code` + body).
2. Brevo dashboard: quota exhausted? Is `BREVO_SENDER_EMAIL` still a verified sender?
3. A `503` on register means the user row was rolled back (§8) — nothing to clean up.
4. If the send succeeded but no email arrived: spam, then Brevo's delivery log. Not a backend fault.

### RB-5 — AI features failing

1. `grep 'AI provider' <logs>` — shows which providers fell through.
2. **No such lines but users report failures** → `grep 'WORKER TIMEOUT'`. The AI-only path budgets 90s against gunicorn's 120s and the repo-context path is now bounded at 90s too (§13.8), so a timeout points elsewhere — a security scan (110s budget, still unbounded in total), a slow query, or a provider timeout raised without a matching gunicorn change. `grep 'request_budget.exhausted'` / `'github_fetch_budget.exhausted'` shows the budgets *working*, not causing a `502`; those requests return `201` with a degraded body. See the table in §12.
3. All three failing at once usually means missing/expired keys, not simultaneous outages — verify `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`.

### RB-6 — GitHub PR reviews not appearing

Work through in order:

1. **Is the worker running?** Check worker logs. (The `ALLOWED_HOSTS` boot failure that used to make this the default answer is fixed — §13.1 — but confirm it started.)
2. **Redis reachable?** Worker logs will show connection errors.
3. **Did GitHub deliver?** Repo → Settings → Webhooks → Recent Deliveries shows our response code. `401` = signature mismatch (`GITHUB_WEBHOOK_SECRET`).
4. **Was it queued?** `WebhookEvent.objects.filter(processed=False)` — rows here mean received but not processed → worker problem.
5. **Was it deliberately skipped?** Only PR actions in `{opened, reopened, synchronize, edited}` are processed, and only pushes to the default branch. `github_webhook.received` logs `should_process`. A skipped event is marked processed and is *not* a fault.
6. **Did it run and fail?** `PullRequestAnalysis.objects.filter(status='failed')` — read `.error`.
7. **Token revoked?** `github_task.auth_failed` and `integration.token_invalid = True` mean the user must reconnect. Not retried by design.

### RB-7 — Repository indexing stuck or incomplete

1. `RepositoryIndex.objects.filter(status='failed').values('repository__full_name', 'error')`.
2. `truncated=True` is **not** a failure — the repo exceeded `GITHUB_MAX_INDEXED_FILES` (300) or GitHub truncated the tree (`github_client.tree_truncated`). Partial graph by design.
3. `repository_index.file_fetch_failed` indicates per-file fetch problems (usually rate limiting) — one API call per file makes indexing the heaviest GitHub consumer.
4. Manual rebuild: the reindex endpoint, which re-queues `build_repository_index`.

### RB-8 — Database problems

1. No handling exists for `OperationalError` — a dropped connection or exhausted pooler surfaces as a generic `500` with a traceback. `grep unhandled_exception`.
2. Supabase dashboard: connection count, pooler saturation. `conn_max_age=600` holds connections open for 10 minutes, so workers accumulate them.
3. If `DATABASE_URL_PROD` uses the transaction pooler (port 6543), confirm `?pgbouncer=true` is present — `backend/config/settings.py` translates it to `DISABLE_SERVER_SIDE_CURSORS = True`.
4. **`/api/health/` stays green throughout** — it is liveness only. Use `/api/health/ready/`, which returns `503` with `checks.database.ok = false` (§10). Application requests now return `503`, not `500` (§13.5).

### RB-9 — Slow or timing-out requests

1. `grep 'WORKER TIMEOUT'` → a request exceeded 120s (§12).
2. Remember concurrency is **3** (§12). Three slow AI requests block everything, including registration — a 90s budget is still 90s of a worker.
3. There are no request-duration metrics (§11) — log timestamps are the only timing signal.

### After any incident

**Capture logs before they age out** (§10). There is no correlation ID, so match on timestamp and path.

## 15. Operational Security Rules

Full model in [SECURITY.md](SECURITY.md); pre-deploy verification in [SECURITY_CHECKLIST.md](SECURITY_CHECKLIST.md).

- **Never put real secrets in `render.yaml`** — every entry is `sync: false` and set in the dashboard. The file is committed.
- **Never commit `.env`.** Gitignored, and excluded from build contexts by `backend/.dockerignore` / `frontend/.dockerignore`, so secrets are not baked into images.
- **Never set `ALLOWED_HOSTS=*` in production** — startup rejects it (`core/settings_validation.py`). It disables Host-header validation, which protects the reset links built in `accounts/emails.py`.
- **Never publish the backend port in Compose** — nginx is the only ingress; a published `8000` bypasses it.
- **Rotating `GITHUB_TOKEN_ENCRYPTION_KEY` forces every connected user to reconnect** (§6). It must stay distinct from `SECRET_KEY`.
- **Containers run as non-root** — keep it that way.
- **The sandbox is macOS-only.** On Linux (every deployed environment) `sandbox-exec` is unavailable, runtime checks are skipped, and this is disclosed as a `runtime_check_unavailable` issue. **Production never executes submitted code.**
- **Webhook signatures are HMAC-verified**; failures return `401` and log `github_webhook.invalid_signature`. Repeated hits mean a secret mismatch or a spoofing attempt.

> **Gap — DRF browsable API enabled in production.** `DEFAULT_RENDERER_CLASSES` is unset, so `BrowsableAPIRenderer` is active everywhere. It does not bypass authentication or permissions, but any API URL opened in a browser renders an interactive HTML page.

## 16. Current Limitations

Real constraints of the implementation, distinct from the gaps above.

- **Concurrency is 3 sync workers.** No async workers; AI calls block a worker for up to the bounded 90s.
- **Migrations are unattended and forward-only** (§5).
- **Email is a hard signup dependency** — no queue, no retry (§8).
- **AI latency budgets 90s** and is still synchronous — a slow chain occupies 1 of 3 workers for that long (§9). The budget is per-I/O-phase, not a hard total; gunicorn's `--timeout` is the hard bound (§12).
- **Repo-context analyze is fully bounded, but degrades under load.** The whole request is capped at `GITHUB_CONTEXT_REQUEST_BUDGET_SECONDS` (§13.8), covering GitHub fetching, the sandbox, Bandit and the AI chain. The cost is that a slow run silently returns *less*: fewer neighbors, or findings with scanner-written rather than AI-written text. That is reported (`context_truncated`, `degraded_stages`) but the frontend does not yet render it — a user sees a thinner result with no on-screen explanation. Grep `request_budget.exhausted` to see how often it is happening.
- **Repository indexing costs one GitHub API call per file**, capped at 300; large repos get a `truncated` partial graph.
- **No data retention or cleanup.** `WebhookEvent` (full webhook payloads), `ChatMessage`, and `Analysis` (complete submitted source in `source_code`) grow without bound. Nothing prunes them.
- **No reaper for stuck rows.** An `Analysis` left `PENDING` by a process death stays `PENDING` forever.
- **Replaced avatars are never deleted**, and now accumulate on the persistent disk (§13.3). Monitor disk usage.
- **No automated dependency updates** — no Dependabot, no Renovate. Manual only.

### Manual maintenance commands

The project has exactly one management command:

```bash
python manage.py backfill_webhook_push_events
```

Updates every monitored repository's **existing** GitHub webhook to also send `push` events. Needed because GitHub does not retroactively change an existing webhook's event list, so repos selected before `push` was added to `WEBHOOK_EVENTS` (`github_integration/services/github_client.py`) never trigger re-indexing on push. Safe to re-run — PATCHing to the event list a webhook already has is a no-op on GitHub's side.

**Relevant to RB-7:** if one repository never re-indexes after a push while others do, its webhook predates push support. Run this command.

No other scheduled or recurring maintenance exists — there is no cron and no Celery Beat (§7).

## 17. Recommended Improvements

Not implemented. Ordered by value relative to effort.

1. **Add error tracking** (backend + frontend) — the single biggest visibility gain given §11. The frontend side now has one reporting hook (`reportClientError` in `lib/api.js`) to wire up.
2. **External uptime monitoring** against `/api/health/ready/`, which is now meaningful (§10).
3. **Alert on Celery failures** — queue depth and `PullRequestAnalysis.status='failed'` rate (§7).
4. **Document and test a database restore** from Supabase backups — untested backups are not backups (§6).
5. **Move avatars to object storage** — removes both the uncached `serve` route and the orphan-file growth (§13.3).
6. **Add a correlation ID** to 500/503 responses and log records (§10).
7. **Retention/cleanup for `WebhookEvent` and old analyses** (§16).
8. **A reaper for `PENDING` analyses** (§16).
9. **Dependabot or Renovate**, and make the audit steps gating once the SECURITY.md §5.1 backlog is clear.

## 18. Open Gaps (Ranked by Practical Impact)

Six of the twenty previously listed here are fixed (§13). What remains is
almost entirely operational safety-net work, not application defects.

| # | Gap | Impact | Reference |
|---|---|---|---|
| 1 | No monitoring, alerting, or error tracking | Incidents found only via user reports | §11 |
| 2 | No backup/recovery in repo; Supabase backups unverified | Potential unrecoverable data loss | §6 |
| 3 | Migrations unattended, no pre-deploy backup, no rollback | Failed migration = manual recovery | `entrypoint.sh`, §5 |
| 4 | No automated rollback; migrations forward-only | Slow, risky incident recovery | §5 |
| 5 | No Celery queue visibility, DLQ, or failure alert | Failures invisible without DB queries | §7 |
| 6 | No worker liveness signal | Dead worker undetected | §7 |
| 7 | No log aggregation or retention beyond platform dashboards | Post-incident forensics limited | §10 |
| 8 | Unbounded growth of `WebhookEvent` / `ChatMessage` / `Analysis` | Storage growth, no pruning | §16 |
| 9 | No email retry/queue — Brevo outage blocks all signups | Onboarding impact | §8 |
| 10 | Replaced avatars never deleted; accumulate on the persistent disk | Disk growth over time | §13.3 |
| 11 | `render.yaml` never validated against Render's live API | Blueprint may not apply cleanly | `render.yaml`, §2 |
| 12 | No web/worker startup ordering on Render | Brief schema mismatch after migrating deploys | §3 |
| 13 | Frontend and backend deploy independently; partial failure undetected | Version mismatch | §5 |
| 14 | No automated dependency updates | Security drift | §16 |
| 15 | DRF browsable API enabled in production | Information disclosure | §15 |
| 16 | No correlation ID between a 500/503 and its traceback | Slower diagnosis | §10 |
| 17 | No reaper for analyses stuck in `PENDING` | Rows stuck indefinitely | §16 |
