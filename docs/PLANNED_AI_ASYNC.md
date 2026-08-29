# Planned: Move AI Calls Off the Synchronous Request Path

**Status: not implemented — design/plan only.** Unlike the other docs in this
folder (`OPERATIONS.md`, `EDGE_CASES.md`, `ERROR_HANDLING.md`, `SECURITY.md`),
which document verified current behavior, this file describes work that has
not been built yet. Nothing here should be treated as "what the code does
today."

This is `SCALABILITY.md`'s backlog item 5 ("Move AI calls off the request
path (Celery + polling or SSE)"), written up as an actual implementation
plan. Trigger: a production user hit a `148`-line "Refactored Code" request
that failed with a raw network error ("Unable to reach the server") instead
of a clean response — see the stopgap already shipped for this
(`AI_REQUEST_TIMEOUT_SECONDS` lowered 30s → 20s, `docs/OPERATIONS.md` §13.2,
`docs/EDGE_CASES.md` §5.5). That stopgap narrows the failure window; it does
not close it, because the actual constraint — Render's own platform proxy
timeout — is outside this app's control and unverified. This plan is the fix
that removes the constraint instead of budgeting around it.

## 1. Prerequisite that blocks this entirely

🔴 **`docs/OPERATIONS.md` §13.9: the Celery worker has never been provisioned
in production.** `render.yaml` declares `code-analyzer-celery-worker`, but the
Render dashboard only has the web service. Today this is silently fine for
everything except GitHub PR review, because — per that same section —
"Auth, analysis, AI, chat, security scanning: unaffected, none of them touch
Celery." **The moment AI calls move onto Celery, that stops being true.**

Shipping this plan without first provisioning the worker does not reproduce
today's bug — it's strictly worse. Today, a slow AI request eventually fails
visibly (a 503, or the network error this plan exists to fix). With AI on an
unconsumed Celery queue, every AI request would appear to accept
(`202`-shaped "processing" response) and then silently never complete — the
exact silent-failure shape GitHub PR review has right now, but on a
user-facing, frequently-used feature instead of a backend integration.

**Do not start implementation until the worker is confirmed running in
production** (`docs/OPERATIONS.md` §13.9's "what to check" applies: confirm a
service page exists for `code-analyzer-celery-worker` in the Render
dashboard, not just in `render.yaml`, and that Redis is reachable from it).

## 2. Scope

**Phase 1 (this plan's primary scope):** the three `analyses/ai_views.py`
endpoints — `SuggestionsView`, `ExplanationView`, `RefactorView`. These are
exactly the request shape that broke (GET → run AI chain → save to the
`Analysis` row → return), they already share one result-caching pattern, and
they're the ones a large pasted file makes slow.

**Phase 2 (stretch, separate follow-up):** `chat/views.py`'s
`SendMessageView` (persisted per-analysis chat). Deliberately split out:
polling fits a "generate once, wait, show result" interaction (Suggestions/
Explanation/Refactor) much better than a chat turn, where the user is
actively watching for a reply. Chat is also throttled to 3 messages/user/day
(`chat/rate_limit.py`), so the concurrency pressure this plan exists to
relieve is much smaller there. If pursued, prefer Server-Sent Events over
polling for chat specifically — SCALABILITY.md's own framing ("Celery +
polling **or SSE**") already anticipates this split.

**Out of scope:** `ai/views.py`'s floating `ChatView`. Per
`.claude/memory/ai-chat.md`, it's implemented server-side but has no frontend
caller today — nothing to convert.

## 3. Data model

Add one small model rather than three new status columns on `Analysis` (one
per AI feature) — it's reusable across Suggestions/Explanation/Refactor and
leaves room for Phase 2 without another migration:

```python
# analyses/models.py

class AIGenerationJob(models.Model):
    class Kind(models.TextChoices):
        SUGGESTIONS = 'suggestions', 'Suggestions'
        EXPLANATION = 'explanation', 'Explanation'
        REFACTOR = 'refactor', 'Refactor'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    analysis = models.ForeignKey(Analysis, on_delete=models.CASCADE, related_name='ai_jobs')
    kind = models.CharField(max_length=20, choices=Kind.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # One in-flight job per (analysis, kind) - see §5's idempotency note.
            models.UniqueConstraint(
                fields=['analysis', 'kind'],
                condition=models.Q(status__in=['pending', 'running']),
                name='one_active_ai_job_per_analysis_kind',
            )
        ]
```

The actual result keeps living where it already does —
`Analysis.ai_suggestions` / `ai_explanation` / `ai_refactored_code` /
`ai_refactor_explanation` — a completed `AIGenerationJob` just means "look at
that field, it's populated." No change to how results are cached/returned
once ready, only to how the caller finds out they're ready.

## 4. Celery task

New `analyses/tasks.py` (mirrors the shape of
`github_integration/tasks.py::process_pull_request_webhook` — this codebase
already has one proven async pipeline to copy):

```python
@shared_task(bind=True, max_retries=2)
def run_ai_generation_job(self, job_id):
    job = AIGenerationJob.objects.select_related('analysis').get(pk=job_id)
    job.status = AIGenerationJob.Status.RUNNING
    job.save(update_fields=['status', 'updated_at'])

    try:
        result = _GENERATORS[job.kind](job.analysis)  # the existing prompt-building + generate_text() call, moved from the view
    except Exception as exc:
        job.status = AIGenerationJob.Status.FAILED
        job.error_message = str(exc)[:2000]
        job.save(update_fields=['status', 'error_message', 'updated_at'])
        return

    _SAVERS[job.kind](job.analysis, result)  # the existing analysis.save(update_fields=[...]) calls, unchanged
    job.status = AIGenerationJob.Status.COMPLETED
    job.save(update_fields=['status', 'updated_at'])
```

The prompt-building, `_call_ai`/`generate_text` call, and response parsing
(`_parse_suggestions`, `_parse_refactor_response`) already in
`analyses/ai_views.py` move into this task essentially unchanged — this is a
relocation of *where* the existing logic runs, not a rewrite of it. No new
per-provider timeout logic is needed: `AI_REQUEST_TIMEOUT_SECONDS` still
bounds each provider call, it just no longer has to fit under gunicorn's or
Render's request timeout, since a Celery task has no such ceiling.

## 5. Endpoint contract

Keep the existing `GET /api/analyses/{pk}/suggestions/` (and
`/explanation/`, `/refactor/`) URLs and views — no new endpoints, so the
frontend's `resources.js` call sites don't need new wrapper functions, just
updated handling of the response shape:

```python
class SuggestionsView(APIView):
    throttle_classes = [AIRateThrottle]

    def get(self, request, pk):
        analysis, error = _get_owned_completed_analysis(request, pk)
        if error:
            return error

        if analysis.ai_suggestions and not _wants_regenerate(request):
            return Response({'status': 'completed', 'suggestions': _normalize_suggestions(analysis.ai_suggestions), 'cached': True})

        job, created = AIGenerationJob.objects.get_or_create(
            analysis=analysis, kind=AIGenerationJob.Kind.SUGGESTIONS,
            status__in=[AIGenerationJob.Status.PENDING, AIGenerationJob.Status.RUNNING],
            defaults={'status': AIGenerationJob.Status.PENDING},
        )
        if created:
            run_ai_generation_job.delay(job.id)

        if job.status == AIGenerationJob.Status.FAILED:
            return Response({'detail': 'AI service is currently unavailable.'}, status=503)
        return Response({'status': job.status}, status=202)
```

**Idempotency is the important part here**, not the polling itself: the
unique constraint in §3 plus `get_or_create` means a user re-opening the tab,
a frontend retry, or a double-click doesn't queue a second identical job —
they all attach to the one already running and get the same `202` until it
resolves. This is the same shape as `github_integration/models.py`'s
`PullRequestAnalysis` get-or-create-by-PR pattern, applied per-analysis
per-kind instead of per-PR.

## 6. Frontend contract

`frontend/src/lib/resources.js`'s `getSuggestions`/`getExplanation`/
`getRefactoredCode` wrappers stay, but the pages that call them
(wherever the Suggestions/AI Explanation/Refactored Code tabs live) need a
poll loop instead of a single await:

- On `202 {status: 'pending'|'running'}` → show a generating/loading state,
  poll again after a fixed interval (start at ~2s, no backoff needed given
  daily AI quotas already cap volume — see `SCALABILITY.md` §3).
- On `200 {status: 'completed', ...}` → render normally, stop polling.
- On `503` → render the existing "AI service is currently unavailable"
  error, stop polling.
- Unmount/tab-switch must cancel the poll (existing `cancelled` flag pattern
  already used in `AuthContext.jsx`'s `restore()` effect).

No change needed to `api.js`'s `apiFetch`/CSRF/refresh machinery — this is a
page-level polling loop built on top of the existing `apiFetch`, not a new
transport mechanism.

## 7. Testing

- `analyses/tasks.py`: unit tests for the task directly (call it inline,
  Celery already runs eager in tests per `CELERY_TASK_ALWAYS_EAGER` —
  confirm this is set for `manage.py test`, matching how
  `github_integration`'s task tests work).
- View tests: assert first `GET` returns `202` + creates exactly one
  `AIGenerationJob`; assert a second concurrent `GET` doesn't create a
  second job (the idempotency property in §5); assert `GET` after the task
  completes returns `200` with the cached result, matching today's
  `cached: true` contract.
- Keep the existing `ai/tests.py` provider-fallback tests unchanged — they
  test `ai/client.py` directly, which isn't moving.
- Add a `RenderDeploymentConfigTests`-style guard (that file already exists
  in `core/tests.py`) asserting nothing new implicitly depends on a
  synchronous-request timeout budget, so a regression back toward blocking
  calls is caught in CI rather than production.

## 8. Explicitly deferred (not part of this plan)

- **SSE for chat** (Phase 2) — separate design, only needed if Phase 2 is
  pursued.
- **Circuit breaker on AI providers** (`SCALABILITY.md` backlog item 8) —
  complementary (stops paying a dead provider's timeout on every request)
  but independent; can land before, after, or never, without blocking this.
- **Removing `AI_REQUEST_TIMEOUT_SECONDS`'s 20s stopgap default** — once this
  ships, the value stops being load-bearing for the timeout-killed-connection
  problem (no request holds an HTTP connection open for AI generation
  anymore), but it should stay as the Celery task's own per-provider bound
  regardless. Not worth reverting to 30s just because the original pressure
  is gone.
