"""Seeds each load-test user with a realistic corpus of Analysis rows.

Why this is not optional: almost every read endpoint in the normal-user
journey costs whatever the caller's row count costs.

  - analyses/views.py:49  _score_summary_for pulls EVERY completed analysis's
    quality_score into Python and buckets it in a loop, on every dashboard load.
  - analyses/analysis_views.py:107  the history list serializes an unbounded
    queryset - there is no DEFAULT_PAGINATION_CLASS.
  - analyses/search_views.py:16  ILIKE '%term%' over source_code, an unindexed
    TextField, then a separate .count() - two sequential scans per search.

Against a user with zero analyses all three are instant and the whole test
reports a flat, fast, meaningless baseline. Seeding at two volumes (e.g. 50
and 1000 rows/user) and diffing the curves is what turns docs/SCALABILITY.md's
predictions into measurements.

The analysis engine is deliberately NOT run here - rows are synthesized to
match its output shape ({'line', 'type', 'message'} issues, see
analyses/engine.py). Running the real engine for 100 users x 1000 rows would
take hours and would measure the seeder, not the app.
"""
import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from analyses.models import Analysis

from ._loadtest import guard_environment, loadtest_users_queryset

# Terms baked into the generated source so the k6 search step matches a
# realistic fraction of rows rather than scanning everything to find nothing.
# loadtest/scenario_a_normal_user.js searches these same terms - keep in sync.
SEARCH_VOCABULARY = ['payment', 'invoice', 'session', 'cache', 'retry', 'parser', 'upload', 'webhook']

# Weighted to look like real usage rather than a uniform spread - the language
# breakdown endpoint groups on this column.
_LANGUAGES = (
    ['Python'] * 12 + ['JavaScript'] * 4 + ['TypeScript'] * 2 + ['Java'] + ['Go']
)

# Mostly completed, with a realistic tail. Only COMPLETED rows carry a
# quality_score, which is what the dashboard's bucketing walks.
_STATUSES = (
    [Analysis.Status.COMPLETED] * 18
    + [Analysis.Status.FAILED]
    + [Analysis.Status.CANCELLED]
)

_ISSUE_TYPES = [
    'todo', 'long_line', 'no_comments', 'unused_import', 'unused_variable',
    'undefined_name', 'redefined_while_unused', 'import_star_used',
]

_HELPERS = """

def _{verb}_{noun}(payload, *, retries={retries}):
    \"\"\"{Verb} the {noun} record and return the normalised result.\"\"\"
    attempt = 0
    result = None
    while attempt < retries:
        try:
            result = {{'{noun}': payload.get('{noun}'), 'attempt': attempt}}
            break
        except (KeyError, ValueError) as exc:  # noqa: PERF203
            attempt += 1
            if attempt >= retries:
                raise RuntimeError('could not {verb} {noun}') from exc
    return result
"""


def _generate_source(rng, language):
    """Builds a plausible module of a few KB. Content matters only in that it
    is long, varied, and contains the search vocabulary."""
    noun = rng.choice(SEARCH_VOCABULARY)
    header = (
        f'"""Module handling {noun} operations."""\n'
        'import json\n'
        'import logging\n'
        'import os\n\n'
        'logger = logging.getLogger(__name__)\n\n'
        f'DEFAULT_{noun.upper()}_TIMEOUT = {rng.randint(5, 60)}\n'
    )
    body = ''.join(
        _HELPERS.format(
            verb=verb, Verb=verb.capitalize(), noun=rng.choice(SEARCH_VOCABULARY),
            retries=rng.randint(2, 5),
        )
        for verb in rng.sample(['process', 'validate', 'fetch', 'persist', 'render', 'audit'], k=rng.randint(3, 6))
    )
    # A handful of long lines and TODOs, so the row looks like something the
    # real engine would have flagged.
    tail = ''.join(
        f'\n# TODO: revisit the {rng.choice(SEARCH_VOCABULARY)} path - '
        + 'x' * rng.randint(40, 140)
        + '\n'
        for _ in range(rng.randint(1, 5))
    )
    if language != 'Python':
        # Close enough for a size/latency fixture; the engine only runs real
        # parsing for Python anyway.
        header = f'// Module handling {noun} operations.\n' + header
    return header + body + tail


def _generate_issues(rng, count):
    return [
        {
            'line': rng.randint(1, 400),
            'type': rng.choice(_ISSUE_TYPES),
            'message': f"'{rng.choice(SEARCH_VOCABULARY)}' flagged by the static analyser.",
        }
        for _ in range(count)
    ]


def _quality_score(rng):
    """Spread across all four dashboard buckets (>=90 / >=70 / >=50 / <50) so
    _score_summary_for's loop does representative work."""
    bucket = rng.random()
    if bucket < 0.25:
        return round(rng.uniform(90, 100), 1)
    if bucket < 0.60:
        return round(rng.uniform(70, 89.9), 1)
    if bucket < 0.85:
        return round(rng.uniform(50, 69.9), 1)
    return round(rng.uniform(0, 49.9), 1)


class Command(BaseCommand):
    help = 'Seed each load-test user with synthetic Analysis rows sized for meaningful read benchmarks.'

    def add_arguments(self, parser):
        parser.add_argument('--analyses-per-user', type=int, default=50, help='Rows per fixture user (default 50).')
        parser.add_argument('--clear', action='store_true', help='Delete existing fixture analyses first.')
        parser.add_argument('--seed', type=int, default=1337, help='RNG seed, for reproducible corpora.')
        parser.add_argument('--days', type=int, default=90, help='Spread created_at over this many past days.')
        parser.add_argument('--force', action='store_true', help='Allow running with ENVIRONMENT=production.')

    def handle(self, *args, **options):
        guard_environment(options['force'])
        per_user = options['analyses_per_user']
        rng = random.Random(options['seed'])
        users = list(loadtest_users_queryset().order_by('id'))

        if not users:
            self.stderr.write('No load-test users found. Run create_loadtest_users first.')
            return

        if options['clear']:
            deleted, _ = Analysis.objects.filter(owner__in=users).delete()
            self.stderr.write(f'Cleared {deleted} existing fixture row(s).')

        now = timezone.now()
        window_seconds = options['days'] * 24 * 3600
        total = 0

        for user in users:
            rows = []
            for n in range(per_user):
                language = rng.choice(_LANGUAGES)
                status = rng.choice(_STATUSES)
                source = _generate_source(rng, language)
                issue_count = rng.randint(0, 15)
                rows.append(Analysis(
                    owner=user,
                    name=f'{rng.choice(SEARCH_VOCABULARY)}_module_{n:04d}.py',
                    language=language,
                    status=status,
                    quality_score=_quality_score(rng) if status == Analysis.Status.COMPLETED else None,
                    issues_count=issue_count,
                    lines_of_code=source.count('\n') + 1,
                    issues=_generate_issues(rng, issue_count),
                    source_code=source,
                ))

            created = Analysis.objects.bulk_create(rows, batch_size=200)

            # created_at is auto_now_add, so the value passed to the
            # constructor is ignored on insert. bulk_update writes it
            # afterwards (it issues plain UPDATEs and does not re-apply
            # auto_now_add/auto_now), which is what spreads the corpus over
            # time instead of stacking every row on one timestamp.
            for row in created:
                row.created_at = now - timedelta(seconds=rng.randint(0, window_seconds))
            Analysis.objects.bulk_update(created, ['created_at'], batch_size=200)
            total += len(created)

        self.stdout.write(f'Seeded {total} analysis row(s) across {len(users)} user(s) ({per_user} each).')
