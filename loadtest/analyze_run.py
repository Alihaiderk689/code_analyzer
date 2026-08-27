#!/usr/bin/env python3
"""Per-stage breakdown of a run in loadtest/results/<stamp>-<profile>/.

k6's own summary aggregates the WHOLE run into one set of percentiles. For a
ramping profile that is close to meaningless: a p99 computed over 2 minutes at
1 VU plus 6 minutes at 500 tells you neither number. This reads the raw
datapoints and reports each plateau separately, which is the only form in
which "latency starts degrading at N users" is a statement you can make.

Stages are discovered from the `vus` gauge rather than hard-coded, so this
cannot drift out of sync with STAGE_PROFILES in scenario_a_normal_user.js.

  python3 loadtest/analyze_run.py loadtest/results/<dir> [--settle 30]

Everything it reads is produced by run.sh: metrics.json (k6), docker-stats.csv,
backend-accept-queue.csv, nginx-sockets.csv, host-loadgen.csv and the
timestamped gunicorn access log.
"""
import argparse
import csv
import json
import os
import sys
from array import array
from datetime import datetime, timezone

COUNTERS = [
    'http_429_throttled', 'http_5xx', 'http_502_worker_killed',
    'net_timeouts', 'net_connection_failures',
]


def parse_rfc3339(text):
    """k6 emits RFC3339 with a numeric offset; docker emits nanoseconds + Z.
    datetime.fromisoformat rejects >6 fractional digits before 3.11 and 'Z'
    before 3.11, so normalise both rather than depending on the interpreter."""
    text = text.strip()
    if text.endswith('Z'):
        text, tz = text[:-1], '+00:00'
    elif len(text) > 6 and text[-6] in '+-' and text[-3] == ':':
        text, tz = text[:-6], text[-6:]
    else:
        tz = '+00:00'
    if '.' in text:
        head, frac = text.split('.', 1)
        text = head + '.' + frac[:6]
    return datetime.fromisoformat(text + tz).timestamp()


def percentile(sorted_values, q):
    if not sorted_values:
        return float('nan')
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    low = int(pos)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (pos - low)


# ------------------------------------------------------------- k6 metrics --

def load_metrics(path):
    """One streaming pass. Durations are kept in three parallel arrays rather
    than a list of dicts - at 500 VUs this file holds millions of points and
    the dict-per-point form is a few GB of RSS for no benefit."""
    times = array('d')
    endpoint_ids = array('i')
    values = array('d')
    endpoints = {}
    vus = []
    iterations = []
    counters = {name: [] for name in COUNTERS}
    req_times = array('d')
    failed = array('d')

    with open(path, 'r') as handle:
        for line in handle:
            if '"Point"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get('type') != 'Point':
                continue
            metric = row.get('metric')
            data = row['data']
            if metric == 'vus':
                vus.append((parse_rfc3339(data['time']), data['value']))
                continue
            if metric == 'http_reqs':
                req_times.append(parse_rfc3339(data['time']))
                continue
            if metric == 'iteration_duration':
                iterations.append((parse_rfc3339(data['time']), data['value']))
                continue
            if metric == 'http_req_failed':
                failed.append(data['value'])
                continue
            if metric in counters:
                if data['value']:
                    counters[metric].append((parse_rfc3339(data['time']), data['value']))
                continue
            if metric == 'http_req_duration':
                endpoint = (data.get('tags') or {}).get('endpoint', 'untagged')
                if endpoint not in endpoints:
                    endpoints[endpoint] = len(endpoints)
                times.append(parse_rfc3339(data['time']))
                endpoint_ids.append(endpoints[endpoint])
                values.append(data['value'])

    return {
        'times': times, 'endpoint_ids': endpoint_ids, 'values': values,
        'endpoints': endpoints, 'vus': sorted(vus), 'iterations': iterations,
        'counters': counters, 'req_times': req_times, 'failed': failed,
    }


def detect_stages(vus, min_seconds=60):
    """A plateau is a run of consecutive identical `vus` samples lasting at
    least min_seconds. Ramps vary every sample and so are excluded, which is
    what we want - a ramp is a transition, not a measurement."""
    stages = []
    if not vus:
        return stages
    start_time, level = vus[0]
    previous_time = vus[0][0]
    for moment, value in vus[1:]:
        if value != level:
            if previous_time - start_time >= min_seconds:
                stages.append({'vus': int(level), 'start': start_time, 'end': previous_time})
            start_time, level = moment, value
        previous_time = moment
    if previous_time - start_time >= min_seconds:
        stages.append({'vus': int(level), 'start': start_time, 'end': previous_time})

    # Label duplicated levels by order, so the 1-VU warm-up and the 1-VU
    # recovery window are distinguishable in the report.
    seen = {}
    for stage in stages:
        seen[stage['vus']] = seen.get(stage['vus'], 0) + 1
    counted = {}
    for stage in stages:
        level = stage['vus']
        counted[level] = counted.get(level, 0) + 1
        if seen[level] > 1:
            suffix = ' (warm-up)' if counted[level] == 1 else ' (recovery)'
        else:
            suffix = ''
        stage['label'] = f'{level} VU{"s" if level != 1 else ""}{suffix}'
    return stages


# ------------------------------------------------------------- other files --

def read_csv_window(path, start, end, epoch_column='epoch'):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as handle:
        for row in csv.DictReader(handle):
            raw = row.get(epoch_column)
            if not raw:
                return []          # a pre-epoch-column CSV from an older run
            try:
                moment = float(raw)
            except ValueError:
                continue
            if start <= moment <= end:
                out.append(row)
    return out


def numeric(rows, key):
    out = []
    for row in rows:
        try:
            out.append(float(row[key]))
        except (KeyError, TypeError, ValueError):
            pass
    return out


def load_access_log(path):
    """(epoch, normalised path, service-time ms) per request, from gunicorn's
    own access log. Lines without a leading timestamp yield None and are
    skipped: without one they cannot be attributed to a stage."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, errors='replace') as handle:
        for line in handle:
            parts = line.split()
            if not parts:
                continue
            moment = None
            if 'T' in parts[0] and '=' not in parts[0]:
                try:
                    moment = parse_rfc3339(parts[0])
                except ValueError:
                    moment = None
            if moment is None:
                continue
            path_value, micros, status = None, None, None
            for token in parts:
                key, sep, value = token.partition('=')
                if not sep:
                    continue
                if key == 'path':
                    path_value = value
                elif key == 'micros':
                    try:
                        micros = float(value)
                    except ValueError:
                        micros = None
                elif key == 'status':
                    status = value
            if path_value is None or micros is None:
                continue
            cleaned = []
            for segment in path_value.split('/'):
                cleaned.append('<id>' if segment.isdigit() else segment)
            out.append((moment, '/'.join(cleaned), micros / 1000.0, status))
    return out


# ------------------------------------------------------------------ report --

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('run_dir')
    parser.add_argument('--settle', type=float, default=30.0,
                        help='Seconds to discard at the start of each plateau (default 30).')
    parser.add_argument('--min-stage', type=float, default=60.0)
    args = parser.parse_args()

    run_dir = args.run_dir.rstrip('/')
    metrics_path = os.path.join(run_dir, 'metrics.json')
    if not os.path.exists(metrics_path):
        sys.exit(f'no metrics.json in {run_dir}')

    meta = ''
    meta_path = os.path.join(run_dir, 'run-metadata.txt')
    if os.path.exists(meta_path):
        meta = open(meta_path).read().strip()

    data = load_metrics(metrics_path)
    stages = detect_stages(data['vus'], args.min_stage)
    if not stages:
        sys.exit('no plateaus found in the vus timeline')

    id_to_endpoint = {v: k for k, v in data['endpoints'].items()}
    access = load_access_log(os.path.join(run_dir, 'gunicorn-access.log'))

    print('=' * 100)
    print(f'PER-STAGE REPORT  {run_dir}')
    if meta:
        for line in meta.splitlines():
            print(f'  {line}')
    print('=' * 100)

    for stage in stages:
        start = stage['start'] + args.settle
        end = stage['end']
        if end - start < 15:
            start = stage['start']
        duration = max(end - start, 1e-9)

        print()
        print('-' * 100)
        print(f"STAGE: {stage['label']}   "
              f"window {datetime.fromtimestamp(start, timezone.utc):%H:%M:%S}-"
              f"{datetime.fromtimestamp(end, timezone.utc):%H:%M:%S}Z  "
              f"({duration:.0f}s measured, {args.settle:.0f}s settle discarded)")
        print('-' * 100)

        # --- k6 latency, per endpoint -------------------------------------
        buckets = {}
        times, ids, values = data['times'], data['endpoint_ids'], data['values']
        for index in range(len(times)):
            if start <= times[index] <= end:
                buckets.setdefault(ids[index], []).append(values[index])

        requests = sum(len(v) for v in buckets.values())
        print(f'  k6 client-side latency (ms) - queueing + service time     '
              f'requests={requests}  throughput={requests / duration:.1f} req/s')
        print(f'    {"endpoint":<24}{"n":>8}{"p50":>10}{"p95":>10}{"p99":>10}{"max":>10}')
        for endpoint_id, series in sorted(buckets.items(), key=lambda kv: -percentile(sorted(kv[1]), 0.95)):
            series.sort()
            print(f'    {id_to_endpoint[endpoint_id]:<24}{len(series):>8}'
                  f'{percentile(series, 0.50):>10.1f}{percentile(series, 0.95):>10.1f}'
                  f'{percentile(series, 0.99):>10.1f}{max(series):>10.1f}')

        # --- gunicorn service time, same window ---------------------------
        service = {}
        statuses = {}
        for moment, path_value, millis, status in access:
            if start <= moment <= end:
                service.setdefault(path_value, []).append(millis)
                statuses[status] = statuses.get(status, 0) + 1
        if service:
            print(f'\n  gunicorn service time (ms) - measured INSIDE the worker')
            print(f'    {"path":<34}{"n":>8}{"p50":>10}{"p95":>10}{"p99":>10}{"max":>10}')
            for path_value, series in sorted(service.items(), key=lambda kv: -percentile(sorted(kv[1]), 0.95)):
                series.sort()
                print(f'    {path_value:<34}{len(series):>8}'
                      f'{percentile(series, 0.50):>10.1f}{percentile(series, 0.95):>10.1f}'
                      f'{percentile(series, 0.99):>10.1f}{max(series):>10.1f}')
            served = sum(statuses.values())
            print(f'    statuses: ' + ', '.join(f'{k}={v}' for k, v in sorted(statuses.items()))
                  + f'  (total {served}, {served / duration:.1f} req/s reached a worker)')

        # --- errors --------------------------------------------------------
        error_line = []
        for name, points in data['counters'].items():
            total = sum(value for moment, value in points if start <= moment <= end)
            error_line.append(f'{name}={int(total)}')
        print('\n  errors: ' + '  '.join(error_line))

        # --- iterations ----------------------------------------------------
        iteration_values = sorted(v for t, v in data['iterations'] if start <= t <= end)
        if iteration_values:
            print(f'  iterations: n={len(iteration_values)}  '
                  f'{len(iteration_values) / duration:.2f}/s  '
                  f'duration p50={percentile(iteration_values, 0.50) / 1000:.2f}s '
                  f'p95={percentile(iteration_values, 0.95) / 1000:.2f}s')

        # --- containers ----------------------------------------------------
        stats = read_csv_window(os.path.join(run_dir, 'docker-stats.csv'), start, end)
        if stats:
            per_container = {}
            for row in stats:
                per_container.setdefault(row['container'], []).append(row)
            print('\n  containers (docker stats; 100% = one core)')
            print(f'    {"container":<32}{"cpu_p50":>10}{"cpu_p95":>10}{"cpu_max":>10}{"mem_max_%":>12}{"mem_last":>16}')
            for name, rows in sorted(per_container.items()):
                cpu = sorted(numeric(rows, 'cpu_percent'))
                mem = numeric(rows, 'mem_percent')
                print(f'    {name:<32}{percentile(cpu, 0.50):>10.1f}{percentile(cpu, 0.95):>10.1f}'
                      f'{max(cpu):>10.1f}{max(mem):>12.2f}{rows[-1]["mem_usage"].split("/")[0].strip():>16}')

        # --- queueing -------------------------------------------------------
        queue_rows = read_csv_window(os.path.join(run_dir, 'backend-accept-queue.csv'), start, end)
        if queue_rows:
            depth = sorted(numeric(queue_rows, 'accept_queue'))
            established = sorted(numeric(queue_rows, 'established'))
            nonzero = sum(1 for d in depth if d > 0)
            print(f'\n  gunicorn accept queue: p50={percentile(depth, 0.50):.0f} '
                  f'p95={percentile(depth, 0.95):.0f} max={max(depth):.0f}  '
                  f'({nonzero}/{len(depth)} samples non-zero = requests waiting for a worker)')
            print(f'  backend established conns: p50={percentile(established, 0.50):.0f} '
                  f'max={max(established):.0f}')

        nginx_rows = read_csv_window(os.path.join(run_dir, 'nginx-sockets.csv'), start, end)
        if nginx_rows:
            time_wait = sorted(numeric(nginx_rows, 'time_wait'))
            est = sorted(numeric(nginx_rows, 'established'))
            print(f'  nginx sockets: established max={max(est):.0f}  '
                  f'TIME_WAIT p95={percentile(time_wait, 0.95):.0f} max={max(time_wait):.0f} '
                  f'(ephemeral range ~28k ports)')

        host_rows = read_csv_window(os.path.join(run_dir, 'host-loadgen.csv'), start, end)
        if host_rows:
            k6_cpu = sorted(numeric(host_rows, 'k6_cpu_pct'))
            k6_rss = numeric(host_rows, 'k6_rss_kb')
            load1 = numeric(host_rows, 'host_load1')
            print(f'  load generator: k6 cpu p50={percentile(k6_cpu, 0.50):.0f}% '
                  f'max={max(k6_cpu):.0f}%  rss_max={max(k6_rss) / 1024:.0f} MiB  '
                  f'host load1 max={max(load1):.2f}')

    print()
    print('=' * 100)


if __name__ == '__main__':
    main()
