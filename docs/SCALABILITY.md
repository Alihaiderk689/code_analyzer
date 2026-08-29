# Scalability

What this system can and cannot grow through, measured against how it is actually deployed today. This document is about **capacity and what breaks first**; the architecture itself is in [ARCHITECTURE.md](ARCHITECTURE.md), the runtime/incident view in [OPERATIONS.md](OPERATIONS.md), and the failure-mode catalogue in [EDGE_CASES.md](EDGE_CASES.md).

Every figure below is read from committed configuration or code, with a file reference. Where a number is an estimate rather than a measured value, it says so — **there is no load testing and no APM in this project**, so nothing here is a benchmark.

---

## 1. Current Capacity

| Tier | Deployed as | Concurrency | Source |
|---|---|---|---|
| Backend web | gunicorn, Render `plan: starter` | **3 sync workers** | `backend/Dockerfile` CMD, `render.yaml` |
| Celery worker | same image, `celery -A config worker` | default prefork (= CPU count) | `docker-compose.yml`, `render.yaml` |
| PostgreSQL | Supabase, pgbouncer transaction pooling (`:6543`) | `CONN_MAX_AGE=600` per process | `config/settings.py:432` |
| Redis | Render key-value | broker + result backend + Django cache | `config/settings.py:301,323` |
| Frontend | static build (Vercel / nginx) | effectively unbounded | `render.yaml` |
| Media | Render persistent disk, **1 GB** | single mount | `render.yaml` |

**Practical ceiling: 3 concurrent backend requests.** Sync workers serve one request each for its full duration. Because AI calls are synchronous and in-request, three concurrent AI requests saturate the entire API — including login and registration, which have nothing to do with AI. That is the defining scalability property of this deployment and everything below is downstream of it.

---

## 2. The Hard Blocker: Horizontal Scaling

```yaml
# render.yaml — backend web service
disk:
  name: code-analyzer-media
  mountPath: /app/media
  sizeGB: 1
```

**A Render service with a persistent disk cannot run more than one instance.** Render disks are single-attach, so `numInstances > 1` is rejected and zero-downtime deploys are unavailable. Adding a second backend instance is not a configuration change today — it requires removing the disk first.

The disk exists for exactly one thing: user avatars (`Profile.avatar`, `upload_to='avatars/%Y/%m/'`, `accounts/models.py:8`). It is the *only* local state the backend holds. Everything else — sessions, throttle counters, quotas, analysis results — is already in Postgres or Redis, so the application code is otherwise stateless and would scale out unmodified.

**To unblock horizontal scaling**, avatars must move to object storage (S3, Cloudflare R2, Supabase Storage). That is one storage backend swap plus deleting the disk and the `/media/` URL pattern in `config/urls.py`. It also removes the "media served by `django.views.static.serve` in production" compromise documented in that file, and the 1 GB cap — worth noting that 1 GB of 5 MB-max avatars is roughly 200 users at the limit, and **replaced avatars are never deleted** ([OPERATIONS.md §16](OPERATIONS.md)), so the disk fills monotonically.

Until then: **vertical scaling only** (a larger Render plan, more gunicorn workers).

---

## 3. Where Time Goes

Scaling a request-per-second number is meaningless here without the latency profile, because worker-seconds are the scarce resource. Worst-case bounded budgets on the synchronous path:

| Path | Bounded at | Bound enforced by |
|---|---|---|
| AI only (suggestions / explanation / refactor / chat) | 60s | 3 × `AI_REQUEST_TIMEOUT_SECONDS` (default 20s each) |
| Security scan | 80s | Bandit 20s **then** the AI chain |
| Repo-context analyze | 90s | `GITHUB_CONTEXT_REQUEST_BUDGET_SECONDS`, a true monotonic total |
| Everything else (CRUD, auth, dashboard) | milliseconds | — |

At 3 workers, **one slow AI request removes 33% of capacity for up to 60 seconds.** Three concurrent ones queue every other request behind gunicorn's backlog until one finishes or the 120s timeout fires.

The daily quotas are what actually keep this survivable: 3 chat messages/user/day (`chat/rate_limit.py`), 1 file check/user/day, 1 context check/user/day. They are product limits, but their operational function is capping how much AI latency the system can be made to absorb.

**The scaling move here is not more workers — it is moving AI work off the request path** (Celery + polling, or SSE), which the PR-review pipeline already does correctly. Raising worker count without that just multiplies memory for processes that are blocked on network I/O.

---

## 4. Database

Connection handling is already right for the deployment: `DISABLE_SERVER_SIDE_CURSORS = True` is set when `pgbouncer=true` is present (`config/settings.py:438`), which is required under transaction pooling. `CONN_MAX_AGE=600` means each of the 3 web workers plus each Celery worker holds a pooler client connection — fine at this size, worth recounting against the pooler limit before raising worker count.

`select_related` / `prefetch_related` are used where they matter (`chat/views.py:55`, `adminapi/views.py:24,59`, `github_integration/pr_views.py:39,57`), so there is no widespread N+1 problem.

Three things degrade with data volume:

### 4.1 Full-text search is a sequential scan 🔴

```python
# analyses/search_views.py:17
queryset = Analysis.objects.filter(owner=request.user).filter(
    Q(name__icontains=query) | Q(language__icontains=query) | Q(source_code__icontains=query)
)
```

`source_code` is an unindexed `TextField` holding **complete submitted source** (`analyses/models.py:21`). `icontains` compiles to `ILIKE '%…%'`, which no btree index can serve. The endpoint then runs `.count()` separately (`search_views.py:29`), so each search is two scans over every one of that user's analyses, reading every source blob.

Fine at tens of rows per user. At thousands it is the first query to become visibly slow. Fix, in increasing order of effort: drop `source_code` from the search fields, or add a `GIN` index with `pg_trgm`, or move to `SearchVector`/`SearchQuery` with a stored `tsvector`.

### 4.2 No pagination anywhere 🔴

`REST_FRAMEWORK` has no `DEFAULT_PAGINATION_CLASS` or `PAGE_SIZE` (`config/settings.py`). Several endpoints serialize an unbounded queryset:

| Endpoint | Returns |
|---|---|
| `adminapi/views.py:37` | **every user** |
| `adminapi/views.py:73` | **every analysis, all users** |
| `analyses/search_views.py:28` | every match |
| `chat/views.py:87` | every message in a conversation |
| `github_integration/repository_views.py:118` | every repo on the GitHub account |

`github_integration/pr_views.py:47` is the exception — it paginates manually. The admin endpoints are the sharpest edge: they grow with total system size rather than per-user size, so they degrade fastest and are the ones an operator reaches for during an incident.

The payloads themselves are lean — `AnalysisSerializer` deliberately excludes `source_code` (`analyses/serializers.py:9`), so this is a row-count problem, not a blob problem.

### 4.3 No explicit indexes 🟡

No `db_index=True` and no `Meta.indexes` anywhere in the project's models. Django indexes foreign keys automatically, which covers the `owner` / `user` / `repository` filters that every ownership-scoped query starts with — so this is not urgent. What is missing is **composite indexes on `(user, created_at)`** for the daily-quota queries, which all filter an FK *and* a timestamp range:

- `chat/rate_limit.py:53` — `conversation__analysis__owner` + `created_at__gte`
- `github_integration/services/file_check_rate_limit.py:51`
- `github_integration/services/context_check_rate_limit.py:53`
- `github_integration/pr_views.py:120` — trend data, `created_at__gte` over N days

These run on quota-relevant requests, which is most of the interesting ones. Postgres will use the FK index and filter the timestamp in memory; that is acceptable until a single user accumulates a lot of rows.

### 4.4 The dashboard buckets scores in Python 🟡

`DashboardSummaryView` (`analyses/views.py:67`) issues ~13 queries per load — `_stats_for` alone is 7 (five `COUNT`s and two aggregates). All are `owner`-scoped and index-assisted, so they scale with per-user row count rather than table size, and at realistic per-user volumes that is fine.

The one that is not just a query count is `_score_summary_for` (`analyses/views.py:49`):

```python
for score in scored.values_list('quality_score', flat=True):
    if score >= 90: buckets['excellent'] += 1
    ...
```

That pulls **every** completed analysis's score into Python on every dashboard load and counts them in a loop. It is a full row fetch dressed as a summary, and unlike the surrounding aggregates it grows linearly in transferred rows, not just scanned ones. A single `COUNT(*) FILTER (WHERE …)` — or `Count(Case(When(...)))` in the ORM — collapses it to one row. Cheap fix, and the dashboard is a hot endpoint.

---

## 5. Unbounded Growth 🔴

Nothing in this project deletes anything. There is no retention policy, no pruning job, and no `celery_beat` service to run one (`CLAUDE.md` notes the absence is deliberate — there are no periodic tasks).

| Table | Grows by | Row size |
|---|---|---|
| `WebhookEvent.payload` | every GitHub webhook delivery | full JSON payload (`github_integration/models.py:266`) |
| `Analysis.source_code` | every paste/upload/file-check | complete source, plus `issues`, `ai_suggestions`, `ai_explanation`, `ai_refactored_code`, `ai_refactor_explanation`, `security_report`, `repo_context` — 8 large columns per row |
| `ChatMessage.message` | every chat turn | full text |
| `RepositoryFileNode.summary` | every indexed file, rebuilt per reindex | up to 2 KB each × up to 300 files per repo |
| Avatar files | every upload; **replacements are never deleted** | up to 5 MB, on the 1 GB disk |

`Analysis` is the one to watch: the context-check feature creates an `Analysis` row per checked file specifically so the chat machinery works unmodified (`repository_views.py`, `_create_analysis_for_file_check`), so analysis rows now accrue from GitHub browsing as well as direct submissions.

A retention policy is the cheapest scalability work available here — a single management command plus a cron trigger, no architectural change.

---

## 6. External API Limits

The ceilings that are not ours to raise:

| Service | Limit | Where it bites |
|---|---|---|
| GitHub REST | 5,000 req/hour per user token | Repo indexing is **one call per file**, capped at `GITHUB_MAX_INDEXED_FILES=300`; a context check costs up to 11 |
| Groq / Gemini / OpenRouter | per-key rate limits | Shared across all users — one key for the whole deployment |
| Brevo | plan-dependent send quota | Every registration and every email change sends an OTP |

The AI keys are the structural one: **all users share one key per provider**, so provider rate limiting is a global failure mode, not a per-user one. The three-provider fallback chain (`ai/client.py`) is what absorbs it — a rate-limited Groq falls through to Gemini rather than failing the request. There is **no circuit breaker**, so a hard-down provider costs its full bounded timeout on every single request before falling through ([EDGE_CASES.md §5.6](EDGE_CASES.md)).

Repository indexing is bounded but expensive: 300 API calls per build, triggered on repo selection and manual reindex. It runs in Celery, so it does not consume web workers.

---

## 7. What Scales Well

Worth stating, because it determines how much work growing this actually is:

- **The application is stateless apart from avatars.** Sessions are JWTs in cookies; throttles are in Redis; quotas are derived live from database rows rather than in-process counters. Remove the disk and the web tier scales out with no code changes.
- **PR review is already asynchronous.** Webhooks are acknowledged in milliseconds and the work is queued (`github_integration/tasks.py`), which is the pattern the synchronous AI paths need.
- **Celery scales independently.** It shares the web image but not the web tier's constraints, and `entrypoint.sh` deliberately does not run migrations for the worker command, so worker count can grow without migration races.
- **The frontend does not participate.** It is a static build on a CDN.
- **Every expensive path is bounded.** The AI chain, the security scan, and the repo-context request all have explicit ceilings ([OPERATIONS.md §13.2, §13.7, §13.8](OPERATIONS.md)) — so load produces degraded responses and queueing rather than 502s and killed workers.

---

## 8. Growth Path

Ordered by ratio of capacity gained to work required. Nothing here is scheduled.

| # | Change | Unlocks | Effort |
|---|---|---|---|
| 1 | Move avatars to object storage; delete the disk | **Horizontal scaling**, and removes the 1 GB cap | Small — one storage backend + one URL pattern |
| 2 | Add DRF pagination, starting with the admin endpoints | Bounded response sizes and query cost | Small |
| 3 | Retention/pruning command for `WebhookEvent`, old `Analysis`, orphaned avatars | Bounded storage growth | Small |
| 4 | Drop `source_code` from search, or add a `pg_trgm` GIN index | Search stays usable as data grows | Small–medium |
| 5 | Move AI calls off the request path (Celery + polling or SSE) | Removes the 3-concurrent-request ceiling; workers stop blocking on network I/O | **Large** — new async contract, frontend included |
| 6 | Composite `(user, created_at)` indexes on the quota tables | Quota checks stay flat as history grows | Small |
| 7 | Replace `_score_summary_for`'s Python bucketing with a filtered `COUNT` | Dashboard stops fetching every score row per load | Small |
| 8 | Circuit breaker on AI providers | Stops paying a dead provider's full timeout per request | Medium |

**Do 1 before anything else.** Every other item improves efficiency within one instance; item 1 is the only one that changes the shape of what is possible.

---

## 9. Known Non-Goals

Deliberately not designed for, so nobody plans around them:

- **Multi-region.** One Render region, one Supabase project. Cookie auth requires frontend and backend to share a registrable domain ([ARCHITECTURE.md](ARCHITECTURE.md)), which constrains how a second region would be fronted.
- **Tenant isolation.** Ownership scoping is per-user (`owner=request.user`), enforced consistently, but there is no organisation/tenant model and no per-tenant resource accounting.
- **Runtime sandboxing at scale.** `analyses/sandbox.py` is macOS-only (`sandbox-exec`) and reports itself unavailable everywhere else — so it is inert on the Linux production host by design, and scaling it would mean adopting a different isolation primitive entirely.
