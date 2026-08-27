#!/usr/bin/env bash
# Samples the things docker stats cannot see: gunicorn's accept queue, the
# nginx<->gunicorn socket population, and the load generator's own cost.
#
# Why each one exists:
#
#   backend accept queue - for a LISTEN socket, /proc/net/tcp's rx_queue field
#     is sk_ack_backlog: connections that have completed the handshake and are
#     waiting for a worker to call accept(). A non-zero value IS gunicorn
#     queueing, observed directly, rather than inferred from the gap between
#     k6's latency and gunicorn's %(D)s. Zero here while k6 latency climbs
#     means the app got slower; non-zero means requests waited for a worker.
#
#   nginx sockets - nginx opens a fresh upstream connection per proxied
#     request (no keepalive, matching production's config), so every request
#     costs an ephemeral port for 60s of TIME_WAIT. The container's range is
#     32768-60999 (~28k ports). At a few hundred rps that is a ceiling the
#     TEST ENVIRONMENT hits, not the application - it has to be measured so it
#     can be told apart from a real limit.
#
#   k6 cpu/rss + host load - the load generator shares this 8-core machine
#     with the Docker VM. A result is only valid if k6 was not the bottleneck.
#
# Usage: observe_queue.sh <out-dir> [interval-seconds]
# Runs until killed. Each sampler is a single long-lived exec, not one exec
# per tick, because ~1500 `docker exec` invocations would themselves be load.
set -euo pipefail

OUT_DIR="${1:?usage: observe_queue.sh <out-dir> [interval]}"
INTERVAL="${2:-2}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "$REPO_ROOT/docker-compose.yml" -f "$REPO_ROOT/docker-compose.loadtest.yml")

# ---- gunicorn accept queue + established connections (inside backend) ------
# python3 rather than awk: the backend image is python:3.11-slim, which has no
# awk at all.
"${COMPOSE[@]}" exec -T backend python3 -u -c '
import time, sys
PORT = ":1F40"   # 8000
print("epoch,timestamp,accept_queue,accept_queue_max,established", flush=True)
while True:
    q = qmax = est = 0
    try:
        with open("/proc/net/tcp") as fh:
            next(fh)
            for line in fh:
                p = line.split()
                if not p[1].endswith(PORT):
                    continue
                tx, rx = p[4].split(":")
                if p[3] == "0A":          # LISTEN: rx_queue is sk_ack_backlog
                    q = int(rx, 16)
                    qmax = int(tx, 16)
                elif p[3] == "01":        # ESTABLISHED
                    est += 1
    except Exception as exc:              # never let the sampler kill the run
        print("#error %s" % exc, file=sys.stderr, flush=True)
    print("%d,%s,%d,%d,%d" % (time.time(), time.strftime("%Y-%m-%dT%H:%M:%S"), q, qmax, est), flush=True)
    time.sleep('"$INTERVAL"')
' > "$OUT_DIR/backend-accept-queue.csv" 2>"$OUT_DIR/backend-accept-queue.err" &

# ---- nginx socket population (inside the frontend container) ---------------
# nginx:alpine has busybox awk. States: 01 ESTABLISHED, 06 TIME_WAIT.
"${COMPOSE[@]}" exec -T frontend sh -c '
echo "epoch,timestamp,established,time_wait,syn_recv,total"
while true; do
  awk -v ts="$(date +%s),$(date +%Y-%m-%dT%H:%M:%S)" "
    NR>1 { n++; if (\$4==\"01\") e++; else if (\$4==\"06\") t++; else if (\$4==\"03\") s++ }
    END { printf \"%s,%d,%d,%d,%d\n\", ts, e, t, s, n }
  " /proc/net/tcp
  sleep '"$INTERVAL"'
done' > "$OUT_DIR/nginx-sockets.csv" 2>"$OUT_DIR/nginx-sockets.err" &

# ---- load generator + host (on the host, not in a container) --------------
{
  echo "epoch,timestamp,k6_cpu_pct,k6_rss_kb,host_load1,host_load5"
  while true; do
    TS="$(date +%s),$(date +%Y-%m-%dT%H:%M:%S)"
    # -o with no header; sum across k6 processes (there is normally one).
    # `|| true` on both: grep exits 1 when k6 is not running yet (the sampler
    # starts before k6 does), and `set -e` would kill this loop on tick one.
    K6="$(ps -Ao pcpu=,rss=,comm= | grep -w '[k]6' | awk '{c+=$1; r+=$2} END {printf "%.1f,%d", c, r}' || true)"
    [ -z "$K6" ] && K6="0.0,0"
    LOAD="$(sysctl -n vm.loadavg | tr -d '{}' | awk '{printf "%s,%s", $1, $2}' || true)"
    [ -z "$LOAD" ] && LOAD="0,0"
    echo "${TS},${K6},${LOAD}"
    sleep "$INTERVAL"
  done
} > "$OUT_DIR/host-loadgen.csv" 2>"$OUT_DIR/host-loadgen.err" &

# One kill of this script's process group takes all three samplers with it.
wait
