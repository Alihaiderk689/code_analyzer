#!/usr/bin/env bash
# Samples per-container CPU/memory while a load test runs.
#
# This is the "minimum server-side observability" half of the setup: k6 sees
# latency from outside, this sees which container was actually busy. Without
# it a result says "p95 was 4s" without saying whether the backend, Postgres
# or Redis was the thing that was saturated.
#
# Usage: observe.sh <output-csv> [interval-seconds]
# Runs until killed (run.sh stops it when k6 exits).
set -euo pipefail

OUT="${1:?usage: observe.sh <output-csv> [interval]}"
INTERVAL="${2:-5}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "$REPO_ROOT/docker-compose.yml" -f "$REPO_ROOT/docker-compose.loadtest.yml")

echo "epoch,timestamp,container,cpu_percent,mem_usage,mem_percent,net_io,block_io,pids" > "$OUT"

# --no-stream so each call is one snapshot; the loop controls the cadence.
# Container IDs are resolved once - `docker stats` on names would re-resolve
# every tick and cost more than the sample is worth.
IDS="$("${COMPOSE[@]}" ps -q | tr '\n' ' ')"
if [ -z "${IDS// /}" ]; then
  echo "observe.sh: no running compose containers found" >&2
  exit 1
fi

while true; do
  TS="$(date +%s),$(date +%Y-%m-%dT%H:%M:%S)"
  # shellcheck disable=SC2086
  docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}},{{.MemPerc}},{{.NetIO}},{{.BlockIO}},{{.PIDs}}' $IDS \
    | sed "s|^|${TS},|" \
    | tr -d '%' >> "$OUT" || true
  sleep "$INTERVAL"
done
