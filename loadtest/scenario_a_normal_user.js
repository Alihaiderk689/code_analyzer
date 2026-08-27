// Scenario A - normal user journey.
//
// Models what a signed-in user actually does: open the app, look at the
// dashboard, browse history, search, open a report, occasionally submit a new
// analysis. Deliberately contains NO AI endpoint, no security scan, no PDF
// render and no GitHub call - those are separate scenarios (see
// loadtest/README.md) because they cost real money, hit shared third-party
// rate limits, or are capped by 1/day quotas.
//
// Auth is a pre-minted bearer token per VU, one dedicated fixture user each.
// Why, in short: LoginRateThrottle is 10/min PER IP for unauthenticated
// requests, and UserRateThrottle is 300/min per user id - so neither a
// per-iteration login nor a shared account is workable. See
// backend/core/management/commands/mint_loadtest_tokens.py.
//
// Run via loadtest/run.sh, which handles fixtures and observability capture.

import http from 'k6/http';
import exec from 'k6/execution';
import { check, group, sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import { SharedArray } from 'k6/data';

// ---------------------------------------------------------------- config ---

const BASE_URL = (__ENV.BASE_URL || 'http://localhost').replace(/\/$/, '');
const API = `${BASE_URL}/api`;
const PROFILE = __ENV.PROFILE || 'smoke';
const THINK_MIN = Number(__ENV.THINK_MIN || 1);
const THINK_MAX = Number(__ENV.THINK_MAX || 3);

// Kept in sync with SEARCH_VOCABULARY in
// backend/core/management/commands/seed_loadtest_data.py, so search terms
// actually match seeded rows instead of scanning everything to find nothing.
const SEARCH_TERMS = ['payment', 'invoice', 'session', 'cache', 'retry', 'parser', 'upload', 'webhook'];

// SharedArray parses the file once and shares one copy across all VUs, rather
// than every VU holding its own deserialized copy (which at 100 VUs is what
// turns the load generator into the bottleneck).
const users = new SharedArray('loadtest users', () => JSON.parse(open('./users.json')));

const STAGE_PROFILES = {
  // Quick correctness check of the harness itself - not a measurement.
  smoke: [
    { duration: '20s', target: 1 },
    { duration: '20s', target: 3 },
    { duration: '10s', target: 0 },
  ],
  // The real profile. The 2m at 1 VU is the unloaded latency floor: every
  // later number is only interpretable as a multiple of it.
  full: [
    { duration: '2m', target: 1 },    // idle baseline
    { duration: '30s', target: 10 },
    { duration: '5m', target: 10 },   // ~3x the 3-worker ceiling
    { duration: '30s', target: 50 },
    { duration: '5m', target: 50 },   // ~17x
    { duration: '30s', target: 100 },
    { duration: '5m', target: 100 },  // ~33x
    { duration: '30s', target: 0 },   // recovery - does latency return to floor?
  ],
  // Capacity profile. `full` establishes that 100 VUs is not a limit; this
  // asks where the limit actually is. The 100-VU plateau is repeated verbatim
  // (same duration) so a capacity run is directly comparable to a `full` run
  // rather than only internally consistent.
  //
  // Requires 500 fixture users (USERS=500). At 500 VUs against 100 users each
  // account carries 5 VUs, which is ~285 req/min against UserRateThrottle's
  // 300/min bucket - close enough that a slow iteration would tip it into
  // 429s that measure the fixture set, not the app. See loadtest/README.md.
  capacity: [
    { duration: '2m', target: 1 },    // reference floor - same as `full`
    { duration: '1m', target: 100 },
    { duration: '5m', target: 100 },  // reference plateau, comparable to `full`
    { duration: '1m', target: 250 },
    { duration: '5m', target: 250 },
    { duration: '1m', target: 500 },
    { duration: '6m', target: 500 },  // the measurement
    { duration: '1m', target: 1 },    // load drops
    { duration: '3m', target: 1 },    // recovery - does latency return to floor?
    { duration: '10s', target: 0 },
  ],
  // Validates the 500-VU CONFIGURATION (fixture count, file descriptors,
  // ephemeral ports, users.json parsing) in ~2.5 min instead of ~25. Not a
  // measurement: 60s at 500 VUs is barely three iterations per VU.
  capacity_smoke: [
    { duration: '30s', target: 1 },
    { duration: '30s', target: 500 },
    { duration: '1m', target: 500 },
    { duration: '20s', target: 0 },
  ],
};

const ENDPOINTS = [
  'profile', 'admin_stats_probe',
  'dashboard_summary',
  'history_list', 'search', 'analysis_detail', 'analyze_create',
];

// A permissive threshold per endpoint is what makes k6 render a per-endpoint
// sub-metric block in the summary (with the percentiles configured below).
// Set high on purpose: this run must COMPLETE and produce a baseline, not
// abort partway through on a pass/fail judgement we have not earned yet.
const thresholds = {
  http_req_failed: ['rate<1.00'],
  checks: ['rate>0.00'],
};
for (const ep of ENDPOINTS) {
  thresholds[`http_req_duration{endpoint:${ep}}`] = ['p(99)<120000'];
}

export const options = {
  scenarios: {
    normal_user: {
      executor: 'ramping-vus',
      startVUs: 1,
      stages: STAGE_PROFILES[PROFILE] || STAGE_PROFILES.smoke,
      gracefulRampDown: '30s',
      tags: { scenario: 'normal_user' },
    },
  },
  thresholds,
  summaryTrendStats: ['avg', 'min', 'med', 'p(50)', 'p(90)', 'p(95)', 'p(99)', 'max'],
  noConnectionReuse: false,
  discardResponseBodies: false,
};

// --------------------------------------------------------------- metrics ---

const errorRate = new Rate('journey_errors');
const throttled429 = new Counter('http_429_throttled');
const server5xx = new Counter('http_5xx');
const badGateway502 = new Counter('http_502_worker_killed');
const timeouts = new Counter('net_timeouts');
const connFailures = new Counter('net_connection_failures');
const respBytes = new Trend('response_bytes', false);

// ------------------------------------------------------------- utilities ---

function think() {
  sleep(THINK_MIN + Math.random() * (THINK_MAX - THINK_MIN));
}

function pick(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

function authFor(vuId) {
  // Stable VU -> user mapping. Modulo so a smoke run with 3 VUs against 100
  // minted users still works, and so does the reverse.
  const user = users[(vuId - 1) % users.length];
  return {
    user,
    headers: {
      Authorization: `Bearer ${user.access}`,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
  };
}

function params(endpoint, headers, extra) {
  return Object.assign({ headers, tags: { endpoint }, timeout: '130s' }, extra || {});
}

// Classifies one response and feeds the counters the report is built from.
// `expected` is the set of statuses that are a CORRECT outcome for this
// endpoint - notably 403 for the admin probe, which every real session makes
// and which must not be scored as an error.
function record(res, endpoint, expected) {
  const ok = expected.includes(res.status);
  errorRate.add(!ok, { endpoint });

  if (res.status === 0) {
    // No HTTP response at all. k6 reports request timeouts with error_code
    // 1050; anything else at status 0 is a connection-level failure, which at
    // high VU counts is what gunicorn's listen backlog overflowing looks like.
    if (res.error_code === 1050 || /timeout/i.test(res.error || '')) {
      timeouts.add(1, { endpoint });
    } else {
      connFailures.add(1, { endpoint });
    }
  } else {
    if (res.status === 429) throttled429.add(1, { endpoint });
    if (res.status === 502) badGateway502.add(1, { endpoint });
    if (res.status >= 500) server5xx.add(1, { endpoint });
    if (res.body) respBytes.add(res.body.length, { endpoint });
  }

  check(res, { [`${endpoint} ok`]: () => ok }, { endpoint });
  return ok;
}

function snippet(term) {
  return `"""Fixture module for ${term}."""
import json
import logging

logger = logging.getLogger(__name__)


def build_${term}_payload(records, *, strict=True):
    out = []
    for record in records:
        if strict and '${term}' not in record:
            raise ValueError('missing ${term}')
        out.append({'${term}': record.get('${term}'), 'ok': True})
    return out


def summarise(rows):
    total = 0
    for row in rows:
        total += len(json.dumps(row))
    # TODO: cache this instead of recomputing per call
    return {'count': len(rows), 'bytes': total}
`;
}

// ---------------------------------------------------------------- journey ---

export function setup() {
  // One anon request. Fails the run immediately if the stack is not up, which
  // is much clearer than 100 VUs all reporting connection refused.
  const res = http.get(`${API}/health/`, { tags: { endpoint: 'setup_health' } });
  if (res.status !== 200) {
    throw new Error(`Backend not healthy at ${API}/health/ (status ${res.status}). Is the compose stack up?`);
  }
  return { users: users.length };
}

export default function () {
  // k6 does not emit a metric that was never touched, which would make a
  // clean run's summary omit these counters entirely - indistinguishable from
  // a broken counter. Adding 0 forces them to render as an explicit 0 without
  // changing any sum.
  throttled429.add(0);
  server5xx.add(0);
  badGateway502.add(0);
  timeouts.add(0);
  connFailures.add(0);

  const { headers } = authFor(exec.vu.idInTest);
  let recentIds = [];

  // 1. App boot. AuthContext.jsx primes CSRF, fetches the profile, then
  //    probes /api/admin/stats/ to discover whether the user is an admin -
  //    which 403s for a normal user. Both happen on every single page load,
  //    so both belong in the journey.
  group('boot', () => {
    const responses = http.batch([
      { method: 'GET', url: `${API}/users/profile/`, params: params('profile', headers) },
      {
        method: 'GET',
        url: `${API}/admin/stats/`,
        params: params('admin_stats_probe', headers, {
          responseCallback: http.expectedStatuses(200, 403),
        }),
      },
    ]);
    record(responses[0], 'profile', [200]);
    record(responses[1], 'admin_stats_probe', [200, 403]);
  });
  think();

  // 2. Dashboard. ONE request - Dashboard.jsx calls getDashboard() against the
  //    /api/dashboard/ summary endpoint. It previously fired four parallel
  //    calls (/stats/, /recent/, /languages/, /scores/) via Promise.all, which
  //    made every dashboard load demand four of the three available gunicorn
  //    workers at once; load testing measured those four at 47.8% of all
  //    worker time. This step tracks the page, so it changed when the page did.
  //
  //    The four endpoints still exist and still work - they are simply no
  //    longer what the dashboard uses, so they are no longer in this journey.
  group('dashboard', () => {
    const res = http.get(`${API}/dashboard/`, params('dashboard_summary', headers));
    const ok = record(res, 'dashboard_summary', [200]);

    if (ok) {
      try {
        recentIds = (res.json('recent_analyses') || []).map((r) => r.id);
      } catch (e) {
        recentIds = [];
      }
    }
  });
  think();

  // 3. History list - unpaginated, serializes the user's entire corpus.
  group('history', () => {
    const res = http.get(`${API}/history/`, params('history_list', headers));
    record(res, 'history_list', [200]);
  });
  think();

  // 4. Search - ~30% of iterations. ILIKE '%term%' over source_code plus a
  //    separate COUNT: two sequential scans, no index can serve either.
  if (Math.random() < 0.3) {
    group('search', () => {
      const term = pick(SEARCH_TERMS);
      const res = http.get(`${API}/search/?q=${encodeURIComponent(term)}`, params('search', headers));
      record(res, 'search', [200]);
    });
    think();
  }

  // 5. Open one of the analyses the dashboard just showed - the realistic
  //    "click a row you can see" path, rather than a pre-baked id list.
  if (recentIds.length) {
    group('open_report', () => {
      const id = pick(recentIds);
      const res = http.get(`${API}/analysis/${id}/`, params('analysis_detail', headers));
      record(res, 'analysis_detail', [200]);
    });
    think();
  }

  // 6. Submit a new analysis - ~20% of iterations, because real users read far
  //    more than they submit. NOT a cheap CRUD write: analysis_views.py's
  //    _run_analysis() runs ast + parso + pyflakes synchronously, in-request.
  //    (The macOS-only sandbox stage is inert on these Linux containers, which
  //    is exactly why the test runs under Compose and not on the host.)
  if (Math.random() < 0.2) {
    group('analyze', () => {
      const term = pick(SEARCH_TERMS);
      const payload = JSON.stringify({ name: `k6-${term}-${exec.vu.idInTest}-${exec.scenario.iterationInTest}.py`, code: snippet(term) });
      const res = http.post(`${API}/analysis/analyze/`, payload, params('analyze_create', headers));
      record(res, 'analyze_create', [201]);
    });
    think();
  }
}

export function teardown() {
  // Nothing to clean up here - the rows this run created belong to fixture
  // users and are removed by `manage.py delete_loadtest_users`, or replaced
  // wholesale by the next `seed_loadtest_data --clear`.
}

// No handleSummary() on purpose: run.sh passes --summary-export, which still
// works in this k6 build and keeps k6's own (much more readable) stdout
// summary intact. Overriding it would mean pulling textSummary from the
// remote jslib CDN, i.e. a network dependency for every run.
