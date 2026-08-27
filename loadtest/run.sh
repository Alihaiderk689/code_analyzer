#!/usr/bin/env bash
# Scenario A orchestrator: fixtures -> observability -> k6 -> artifacts.
#
#   ./loadtest/run.sh smoke              # harness check, ~50s, 3 VUs
#   ./loadtest/run.sh full               # idle -> 10 -> 50 -> 100, ~26min
#   USERS=100 ROWS=1000 ./loadtest/run.sh full
#   USERS=500 ./loadtest/run.sh capacity_smoke   # validates a 500-VU config, ~2.5min
#   USERS=500 ./loadtest/run.sh capacity         # 1 -> 100 -> 250 -> 500 -> recovery, ~25min
#
# USERS must be >= the profile's peak VU count, or VUs share fixture accounts
# and therefore share UserRateThrottle buckets - the preflight below refuses
# to run in that case rather than producing 429s that look like an app limit.
#
# Everything lands in loadtest/results/<timestamp>-<profile>/.
set -euo pipefail
# Job control off: without it, killing the background sampler prints a
# "Terminated: 15" line into the middle of the results output.
set +m

PROFILE="${1:-smoke}"
USERS="${USERS:-100}"
ROWS="${ROWS:-50}"
BASE_URL="${BASE_URL:-http://localhost}"
TOKEN_TTL_HOURS="${TOKEN_TTL_HOURS:-6}"
RESEED="${RESEED:-1}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOADTEST_DIR="$REPO_ROOT/loadtest"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="$LOADTEST_DIR/results/${STAMP}-${PROFILE}"
mkdir -p "$OUT_DIR"

COMPOSE=(docker compose -f "$REPO_ROOT/docker-compose.yml" -f "$REPO_ROOT/docker-compose.loadtest.yml")

# Postgres credentials come from the same .env the compose stack uses.
PG_USER="$(grep -E '^POSTGRES_USER=' "$REPO_ROOT/.env" 2>/dev/null | cut -d= -f2- || true)"
PG_DB="$(grep -E '^POSTGRES_DB=' "$REPO_ROOT/.env" 2>/dev/null | cut -d= -f2- || true)"
PG_USER="${PG_USER:-code_analyzer}"
PG_DB="${PG_DB:-code_analyzer}"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- preflight --

say "Preflight"
command -v k6 >/dev/null || { echo "k6 not installed (brew install k6)"; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker daemon is not running"; exit 1; }

if ! "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -q '^backend$'; then
  echo "The compose stack is not running. Start it with:"
  echo "  docker compose -f docker-compose.yml -f docker-compose.loadtest.yml up -d --build"
  exit 1
fi

# Guard against benchmarking a stack that is running WITHOUT the overlay - the
# access log and pg_stat_statements would both be silently missing.
if ! "${COMPOSE[@]}" exec -T backend sh -c 'cat /proc/1/cmdline | tr "\0" " "' 2>/dev/null | grep -q 'access-logfile'; then
  echo "WARNING: backend is running without docker-compose.loadtest.yml (no gunicorn access log)."
  echo "         Recreate with: docker compose -f docker-compose.yml -f docker-compose.loadtest.yml up -d"
  exit 1
fi

curl -fsS "${BASE_URL}/api/health/" >/dev/null || { echo "Backend not reachable at ${BASE_URL}/api/health/"; exit 1; }

# Peak VU count per profile, kept in sync with STAGE_PROFILES in
# scenario_a_normal_user.js. One fixture user per VU is the whole reason the
# test does not measure UserRateThrottle (300/min, keyed by user id) instead
# of the application - see loadtest/README.md constraint 4.
case "$PROFILE" in
  smoke)          PEAK_VUS=3 ;;
  full)           PEAK_VUS=100 ;;
  capacity)       PEAK_VUS=500 ;;
  capacity_smoke) PEAK_VUS=500 ;;
  *)              PEAK_VUS=0 ;;
esac
if [ "$PEAK_VUS" -gt "$USERS" ]; then
  echo "Profile '$PROFILE' peaks at ${PEAK_VUS} VUs but USERS=${USERS}."
  echo "VUs would share fixture accounts, and therefore share per-user throttle buckets."
  echo "Re-run with: USERS=${PEAK_VUS} ./loadtest/run.sh ${PROFILE}"
  exit 1
fi

# k6 needs a file descriptor per in-flight connection. macOS shells often
# default to 256, which at 500 VUs (up to 4 parallel requests each) shows up
# as connection failures attributable to the load generator, not the app.
FD_LIMIT="$(ulimit -n)"
if [ "$FD_LIMIT" != "unlimited" ] && [ "$FD_LIMIT" -lt $((PEAK_VUS * 8)) ]; then
  echo "Raising file-descriptor limit for this run: ${FD_LIMIT} -> $((PEAK_VUS * 8))"
  ulimit -n $((PEAK_VUS * 8)) || { echo "Could not raise ulimit -n; run 'ulimit -n 8192' first"; exit 1; }
fi

# ----------------------------------------------------------------- fixtures --

say "Fixtures: ${USERS} users x ${ROWS} analyses"
"${COMPOSE[@]}" exec -T backend python manage.py create_loadtest_users --count "$USERS"

if [ "$RESEED" = "1" ]; then
  "${COMPOSE[@]}" exec -T backend python manage.py seed_loadtest_data --analyses-per-user "$ROWS" --clear
else
  echo "RESEED=0 - keeping the existing corpus."
fi

say "Minting bearer tokens (ttl ${TOKEN_TTL_HOURS}h)"
"${COMPOSE[@]}" exec -T backend python manage.py mint_loadtest_tokens --ttl-hours "$TOKEN_TTL_HOURS" \
  > "$LOADTEST_DIR/users.json"
# The Django command writes progress to stderr and only JSON to stdout, so a
# non-array here means something went wrong and k6 would fail confusingly.
head -c1 "$LOADTEST_DIR/users.json" | grep -q '\[' || { echo "Token minting produced no JSON array"; exit 1; }

# ----------------------------------------------------------- observability --

say "Resetting server-side counters"
"${COMPOSE[@]}" exec -T db psql -U "$PG_USER" -d "$PG_DB" -q \
  -c 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements;' \
  -c 'SELECT pg_stat_statements_reset();' >/dev/null

# Marker line so the access log can be sliced to exactly this run's window.
RUN_MARK="LOADTEST-RUN-${STAMP}"
# The trailing Z is load-bearing. `docker compose logs --since` parses a
# timestamp with no zone designator as LOCAL time, so a UTC-formatted value
# without it was read as local - and in any zone ahead of UTC that resolves to
# a moment hours in the past, silently pulling every earlier run's requests
# into this run's access log. RFC3339 with Z is unambiguous.
LOG_SINCE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

"$LOADTEST_DIR/observe.sh" "$OUT_DIR/docker-stats.csv" 5 &
OBSERVER_PID=$!

# Second sampler: gunicorn's accept queue (queueing, observed directly rather
# than inferred), the nginx socket population (ephemeral-port headroom), and
# the load generator's own CPU/RSS (proof k6 was not the bottleneck).
"$LOADTEST_DIR/observe_queue.sh" "$OUT_DIR" 2 &
QUEUE_PID=$!

# Both samplers spawn children (`docker stats`, long-lived `docker compose
# exec`), so killing the script's own pid is not enough - walk the tree.
kill_tree() {
  local pid="$1" child
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do kill_tree "$child"; done
  kill "$pid" 2>/dev/null || true
}
stop_samplers() { kill_tree "$OBSERVER_PID"; kill_tree "$QUEUE_PID"; }
# Stop the samplers however this script exits, including Ctrl-C.
trap stop_samplers EXIT

# ----------------------------------------------------------------------- k6 --

say "k6: profile=${PROFILE} base=${BASE_URL}"
set +e
BASE_URL="$BASE_URL" PROFILE="$PROFILE" \
  k6 run \
    --summary-export "$OUT_DIR/summary.json" \
    --out "json=$OUT_DIR/metrics.json" \
    "$LOADTEST_DIR/scenario_a_normal_user.js" 2>&1 | tee "$OUT_DIR/k6-console.txt"
K6_STATUS=${PIPESTATUS[0]}
set -e

stop_samplers

# ----------------------------------------------------------------- capture --

say "Capturing server-side detail"

# Top queries by total time. This is what turns "the dashboard is slow" into
# "the dashboard issues 13 queries and this one is 60% of the time".
"${COMPOSE[@]}" exec -T db psql -U "$PG_USER" -d "$PG_DB" -P pager=off -c "
  SELECT calls,
         round(total_exec_time::numeric, 1)  AS total_ms,
         round(mean_exec_time::numeric, 2)   AS mean_ms,
         rows,
         left(regexp_replace(query, '\s+', ' ', 'g'), 140) AS query
  FROM pg_stat_statements
  WHERE query NOT LIKE '%pg_stat_statements%'
  ORDER BY total_exec_time DESC
  LIMIT 25;" > "$OUT_DIR/pg-top-queries.txt" 2>>"$OUT_DIR/capture-errors.log" || true

# Database-side capacity picture, as distinct from "which query is slow":
# how many backends were in use against max_connections, whether the cache was
# actually serving reads, and whether any query spilled to temp files. Table
# stats separate "the query is slow" from "the query sequentially scanned N
# rows because the corpus grew".
"${COMPOSE[@]}" exec -T db psql -U "$PG_USER" -d "$PG_DB" -P pager=off \
  -c "SELECT name, setting FROM pg_settings
      WHERE name IN ('max_connections','shared_buffers','work_mem','effective_cache_size');" \
  -c "SELECT numbackends, xact_commit, xact_rollback, blks_read, blks_hit,
             round(100.0*blks_hit/NULLIF(blks_hit+blks_read,0), 2) AS cache_hit_pct,
             tup_returned, tup_fetched, tup_inserted, deadlocks, temp_files, temp_bytes
      FROM pg_stat_database WHERE datname = current_database();" \
  -c "SELECT count(*) AS connections_now,
             count(*) FILTER (WHERE state = 'active') AS active_now
      FROM pg_stat_activity WHERE datname = current_database();" \
  -c "SELECT relname, seq_scan, seq_tup_read, idx_scan, n_live_tup
      FROM pg_stat_user_tables WHERE relname IN ('analyses_analysis','auth_user','accounts_profile')
      ORDER BY seq_tup_read DESC;" \
  -c "SELECT pg_size_pretty(pg_database_size(current_database())) AS db_size;" \
  > "$OUT_DIR/pg-summary.txt" 2>>"$OUT_DIR/capture-errors.log" || true

# nginx is the only way into the backend (the backend publishes no host port),
# so any error it logged - worker_connections exhausted, upstream refused,
# ephemeral ports exhausted - is a limit the test hit BEFORE the application.
# It has to be read to tell an environment limit from an application one.
"${COMPOSE[@]}" logs --since "$LOG_SINCE" --no-log-prefix frontend 2>/dev/null \
  | grep -Ei 'error|warn|upstream|timed out|refused' > "$OUT_DIR/nginx-errors.log" || true

# gunicorn's own per-request timing (microseconds), for this run's window only.
# -t prepends docker's receive timestamp: without it the log is one flat pile
# with no way to say "service time during the 500-VU plateau" as opposed to
# "service time averaged over the whole run, most of which was not at 500".
# The awk below ignores the extra token (it has no '='), so both forms parse.
"${COMPOSE[@]}" logs -t --since "$LOG_SINCE" --no-log-prefix backend 2>/dev/null \
  | grep -E '(^| )ACCESS ' > "$OUT_DIR/gunicorn-access.log" || true

# Per-endpoint service time as measured INSIDE gunicorn. Compare against k6's
# http_req_duration for the same endpoint: k6 sees queueing + service time,
# this sees service time alone, and the gap is the queue.
# Portable key=value parsing (BSD awk on macOS has no 3-argument match()).
awk '
  {
    path = ""; micros = -1
    for (i = 1; i <= NF; i++) {
      eq = index($i, "=")
      if (eq == 0) continue
      k = substr($i, 1, eq - 1)
      v = substr($i, eq + 1)
      if (k == "path")   path = v
      if (k == "micros") micros = v + 0
    }
    if (path == "" || micros < 0) next
    gsub(/\/[0-9]+\//, "/<id>/", path)
    n[path]++; sum[path] += micros
    if (micros > max[path]) max[path] = micros
  }
  END {
    printf "%-42s %8s %12s %12s\n", "path", "count", "mean_ms", "max_ms"
    for (p in n) printf "%-42s %8d %12.1f %12.1f\n", p, n[p], sum[p]/n[p]/1000, max[p]/1000
  }
' "$OUT_DIR/gunicorn-access.log" 2>/dev/null > "$OUT_DIR/gunicorn-by-endpoint.txt" || true

{
  echo "profile=$PROFILE peak_vus=$PEAK_VUS users=$USERS rows_per_user=$ROWS base_url=$BASE_URL"
  echo "started=$LOG_SINCE finished=$(date -u +%Y-%m-%dT%H:%M:%S) k6_exit=$K6_STATUS"
  echo "docker: $(docker info --format '{{.NCPU}} CPUs, {{.MemTotal}} bytes RAM')"
  echo "host: $(sysctl -n hw.ncpu 2>/dev/null || echo '?') CPUs, $(sysctl -n hw.memsize 2>/dev/null || echo '?') bytes RAM"
  echo "loadgen: $(k6 version 2>/dev/null | head -1), ulimit -n=$(ulimit -n)"
  echo "gunicorn: $("${COMPOSE[@]}" exec -T backend sh -c 'cat /proc/1/cmdline | tr "\0" " "' 2>/dev/null)"
  echo "nginx: $("${COMPOSE[@]}" exec -T frontend sh -c 'grep -hE "worker_processes|worker_connections" /etc/nginx/nginx.conf | tr -s " " | tr "\n" " "' 2>/dev/null)"
  echo "fixture_users_in_json=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$LOADTEST_DIR/users.json" 2>/dev/null || echo '?')"
} > "$OUT_DIR/run-metadata.txt"

say "Done -> $OUT_DIR"
ls -1 "$OUT_DIR"
exit "$K6_STATUS"
