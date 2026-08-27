# Load testing

Baseline capacity measurement for this project. Scenario A (normal user) only —
the PDF, AI-concurrency and admin scenarios are planned but **not implemented yet**.

Context and predictions live in [../docs/SCALABILITY.md](../docs/SCALABILITY.md).
This directory is what turns those predictions into measurements.

---

## Why it is built this way

Four constraints in the application decide almost every design choice here.

**1. Registration is unusable for fixtures.** `POST /api/auth/register/` sends a
real Brevo email per call, is throttled at 5/hour per IP, and runs
`PwnedPasswordsValidator`, which makes a live HTTPS call to Have I Been Pwned on
every password set. So users are created straight through the ORM by a
management command. `set_password()` does not run validators, so nothing leaves
the process.

**2. Login is throttled per IP, not per user.** `LoginRateThrottle` extends
`ScopedIdentityRateThrottle`, which falls back to the client IP when the request
is unauthenticated — and login requests always are. At `login: 10/min`, 100 VUs
logging in from one load generator would get 10 successful logins a minute. So
the test never logs in: tokens are minted offline.

**3. Access tokens expire in 15 minutes.** `SIMPLE_JWT.ACCESS_TOKEN_LIFETIME` is
15 minutes and the full profile runs ~19 minutes. Tokens minted at setup would
expire mid-run, every request would 401, and the test would report excellent
latency for an application doing nothing. `mint_loadtest_tokens` overrides the
expiry **per token**, so no settings change is needed and nothing leaks into how
the real app issues tokens.

**4. One shared account would cap the whole test at 5 rps.** `UserRateThrottle`
is `300/min` keyed by user id. Every VU gets its own fixture user, which puts
each at roughly 50 req/min against its own bucket. **Any 429 in the results means
the test is misconfigured, not that the app hit a limit.**

Bearer auth also skips CSRF entirely — `CookieJWTAuthentication` checks the
`Authorization` header first and only enforces the double-submit check for
cookie-sourced auth. That is a deliberate, stated gap: this scenario does not
exercise the CSRF path.

---

## Requirements

```bash
brew install k6          # k6 v1.0+ (tested on v2.2.0)
```

Docker Desktop running, and a `.env` at the repo root (`cp .env.example .env`,
fill in `SECRET_KEY` and `POSTGRES_PASSWORD`).

---

## Running it

Bring the stack up **with the load-test overlay** — `run.sh` refuses to run
without it, because the gunicorn access log and `pg_stat_statements` would
silently be missing:

```bash
docker compose -f docker-compose.yml -f docker-compose.loadtest.yml up -d --build
```

Then:

```bash
./loadtest/run.sh smoke                    # ~50s, 3 VUs - checks the harness
./loadtest/run.sh full                     # idle -> 10 -> 50 -> 100, ~19 min
USERS=100 ROWS=1000 ./loadtest/run.sh full # the high-data-volume comparison run

USERS=500 ./loadtest/run.sh capacity_smoke # ~2.5 min - validates a 500-VU config
USERS=500 ./loadtest/run.sh capacity       # 1 -> 100 -> 250 -> 500 -> recovery, ~25 min
```

Environment knobs: `USERS` (default 100), `ROWS` (analyses per user, 50),
`BASE_URL` (`http://localhost`), `TOKEN_TTL_HOURS` (6), `RESEED=0` to keep the
existing corpus, `THINK_MIN`/`THINK_MAX` (1/3 seconds between journey steps).

**`USERS` must be at least the profile's peak VU count.** `run.sh` refuses to
start otherwise, because sharing fixture accounts means sharing
`UserRateThrottle` buckets, and the run would report 429s that measure the
fixture set rather than the application (constraint 4 above).

Teardown:

```bash
docker compose -f docker-compose.yml -f docker-compose.loadtest.yml \
  exec -T backend python manage.py delete_loadtest_users --yes
```

---

## The stage profiles

`full` - the original baseline profile:

```
idle baseline   1 VU,  2 min     <- the unloaded latency floor
ramp 0 -> 10   30 s
hold at 10      5 min            <- ~3x the 3-worker ceiling
ramp 10 -> 50  30 s
hold at 50      5 min            <- ~17x
ramp 50 -> 100 30 s
hold at 100     5 min            <- ~33x
ramp -> 0      30 s              <- recovery: does latency return to the floor?
```

`capacity` - runs past the point `full` establishes is not a limit:

```
reference       1 VU,  2 min     <- same warm-up as `full`
ramp -> 100     1 min
hold at 100     5 min            <- repeated verbatim, so the two runs compare
ramp -> 250     1 min
hold at 250     5 min
ramp -> 500     1 min
hold at 500     6 min            <- the measurement
ramp -> 1       1 min
hold at 1       3 min            <- recovery, under load rather than at zero
ramp -> 0      10 s
```

Recovery is held at 1 VU rather than dropped straight to 0 so there is
something still measuring while the system drains. A run that ramps to zero
produces no requests during recovery and therefore no evidence of it.

The 1-VU stage comes first and is not optional. Note that it includes cold
start - the first requests pay Python import, Django connection setup and an
empty Postgres buffer cache, so the 1-VU numbers come out consistently *worse*
than the loaded ones. The warm floor is the 100-VU plateau, not the 1-VU one.

---

## The journey

Per iteration, with 1–3 s think time between steps:

| Step | Requests | Frequency | Why it is in |
|---|---|---|---|
| boot | `users/profile/` + `admin/stats/` (403) | every | `AuthContext.jsx` does both on every page load |
| dashboard | 4 parallel `dashboard/*` calls | every | `Dashboard.jsx` fires them in one `Promise.all` |
| history | `history/` | every | unpaginated — serializes the whole corpus |
| search | `search/?q=` | 30% | `ILIKE '%term%'` over `source_code` + separate `COUNT` |
| open report | `analysis/<id>/` | when ids available | id comes from the dashboard response, as a real click would |
| analyze | `POST analysis/analyze/` | 20% | synchronous `ast`+`parso`+`pyflakes` in-request |

Search terms are shared with `seed_loadtest_data.py`'s `SEARCH_VOCABULARY` so
queries match real rows instead of scanning everything to find nothing.

**Not included, on purpose:** all AI endpoints and the security scan (real
provider calls on one shared API key, and they cache — a second call returns
instantly and would silently make the numbers meaningless), the PDF report, all
`/api/github/*` (real GitHub API, 1/day quotas), `history/clear/` (deletes the
corpus), avatar upload (fills the 1 GB disk), and registration.

---

## Output

`loadtest/results/<timestamp>-<profile>/`:

| File | What it is |
|---|---|
| `k6-console.txt` | k6's summary — p50/p90/p95/p99 per endpoint tag, plus the counters |
| `summary.json` | the same, machine-readable, for diffing runs |
| `metrics.json` | every raw datapoint (large) — for per-stage breakdowns |
| `docker-stats.csv` | per-container CPU/memory, sampled every 5 s |
| `gunicorn-access.log` | per-request timing measured **inside** gunicorn |
| `gunicorn-by-endpoint.txt` | mean/max service time per path |
| `pg-top-queries.txt` | top 25 queries by total time, from `pg_stat_statements` |
| `backend-accept-queue.csv` | gunicorn's listen-socket accept queue, sampled every 2 s |
| `nginx-sockets.csv` | nginx's ESTABLISHED/TIME_WAIT population - ephemeral-port headroom |
| `host-loadgen.csv` | k6's own CPU/RSS and host load - proof the generator was not the limit |
| `nginx-errors.log` | anything nginx logged: upstream refused, worker_connections, timeouts |
| `pg-summary.txt` | backends in use vs `max_connections`, cache hit ratio, temp files, table scan counts |
| `run-metadata.txt` | profile, fixture sizes, host CPU/RAM, gunicorn argv, nginx worker config |

Run `python3 loadtest/analyze_run.py loadtest/results/<dir>` for the per-stage
breakdown. k6's own summary aggregates the whole ramp into one set of
percentiles, which for a multi-stage profile is not a usable number; the script
discovers each plateau from the `vus` gauge and reports latency, throughput,
errors, container CPU/memory, accept-queue depth and load-generator cost for
each one separately.

**The comparison that matters:** k6's `http_req_duration` for an endpoint minus
gunicorn's `micros` for the same path. k6 measures queueing *plus* service time;
gunicorn measures service time alone. The gap is the queue — which is how you
tell "this endpoint is slow" from "this endpoint waited behind three busy
workers".

Custom counters in the k6 summary: `http_429_throttled` (should be 0 — see
constraint 4), `http_502_worker_killed` (gunicorn's 120 s timeout fired),
`http_5xx`, `net_timeouts`, `net_connection_failures` (listen-backlog overflow).

`backend-accept-queue.csv` measures that same queue *directly* rather than
inferring it: for a listening socket, `/proc/net/tcp`'s `rx_queue` field is
`sk_ack_backlog` — connections that finished the TCP handshake and are waiting
for a worker to `accept()` them. Zero there while k6 latency climbs means the
application got slower; non-zero means requests waited for a worker.

---

## Known confounds

State these alongside any result.

- **Compose on macOS is not Render.** Production is a Render `starter` instance
  against Supabase over the network with pgbouncer transaction pooling; this is
  a local Linux VM against a container-local Postgres with direct connections.
  Worker count (3) and gunicorn's timeout (120 s) match. **Absolute numbers do
  not transfer; the shape of the curve and the ranking of endpoints do.**
- **The load generator shares the machine.** At 100 VUs k6 is nearly idle
  compared to the stack, but it is not zero. At 500 VUs it is not negligible,
  which is why `host-loadgen.csv` exists - a capacity result is only valid if
  k6's own CPU was not the thing that ran out.
- **nginx opens a fresh upstream connection per request** (no keepalive,
  matching production's config), so every proxied request holds an ephemeral
  port for 60 s of TIME_WAIT. The container's range is ~28k ports, which caps
  the environment at roughly 470 sustained rps regardless of the application.
  Above that, failures are the harness's, not the app's - `nginx-sockets.csv`
  is what tells the two apart.
- **Docker Desktop's macOS port forwarding** sits between k6 and nginx. It is
  a userspace proxy and is not part of production; at several hundred
  concurrent connections it is a plausible limiter of its own.
- **The sandbox is inert here, correctly.** `analyses/sandbox.py` is macOS-only
  and reports itself unavailable on Linux — matching production. This is the
  main reason not to run this against `runserver` on the host, where the
  sandbox *would* run and inflate every `analyze` measurement.
- **No nginx upstream keepalive.** Each proxied request opens a fresh
  connection to gunicorn, same as production's nginx config.
- **Throttle counters live in Redis** and persist between runs; the buckets are
  1-minute windows, so they self-expire, but back-to-back runs can overlap.
