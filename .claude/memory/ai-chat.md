# AI Assistance & Chat

## Overview
Three call sites share one AI client (`ai/client.py`): the floating general-purpose
assistant (`ai/views.ChatView`), per-analysis suggestions/explanation/refactor
(`analyses/ai_views.py`), and per-analysis persisted chat (`chat/views.py`). All
three catch AI-call exceptions identically and return a 503 "AI service is
currently unavailable." Prompt-building for the two chat surfaces is centralized
in `ai/prompts.py` so they describe an `Analysis` identically; suggestions/
explanation/refactor build their own one-off prompts directly in `ai_views.py`.
Despite the name, the client is not Groq-only: it's a three-provider fallback
chain (Groq → Gemini → OpenRouter).

## Key files
- `backend/ai/client.py` — `_call_with_fallback`, `generate_text`, `generate_chat_reply`; the only code that talks to the AI providers.
- `backend/ai/prompts.py` — `BASE_CHAT_INSTRUCTION` and `build_analysis_context(analysis)`, shared by both chat surfaces.
- `backend/ai/views.py` — `ChatView`, the floating/general-purpose chat endpoint (`POST /api/ai/chat/`).
- `backend/ai/serializers.py` — `ChatRequestSerializer`/`ChatMessageSerializer` for the floating chat (client-supplied history, capped at 20 turns / 8000 chars each).
- `backend/analyses/ai_views.py` — `SuggestionsView`, `ExplanationView`, `RefactorView` (`GET /api/analysis/<pk>/suggestions|explanation|refactor/`).
- `backend/analyses/analysis_urls.py` — wires the three views above.
- `backend/chat/models.py` — `Conversation` (OneToOne with `Analysis`), `ChatMessage`.
- `backend/chat/views.py` — `StartConversationView`, `SendMessageView`, `ChatHistoryView`, `RateLimitStatusView`.
- `backend/chat/rate_limit.py` — `get_rate_limit_status`, `DAILY_MESSAGE_LIMIT = 3`, local-midnight quota reset logic.
- `backend/chat/serializers.py` — `SendMessageSerializer` (carries `tz_offset_minutes`).
- `backend/chat/urls.py` — mounted at `api/chat/` (`start/<id>/`, `message/`, `history/<id>/`, `limit/`).
- `backend/core/throttling.py` — `AIRateThrottle` (scope `'ai'`, `30/min` in `settings.py`), applied to all five AI-calling views.
- `backend/core/execution_budget.py` — `ExecutionBudget`/`BudgetExceeded`, an optional shared deadline `generate_text` can accept (only the GitHub repository-context path passes one; every other caller leaves it `None` and behaves exactly as before).
- `backend/config/settings.py` (~lines 263-286) — `AI_REQUEST_TIMEOUT_SECONDS` (default 30s, applied per-provider) and the three providers' API keys/models (`GROQ_*`, `GEMINI_*`, `OPENROUTER_*`).
- `frontend/src/pages/AnalysisChat.jsx` — page wrapper that loads the `Analysis` and renders `AnalysisChatPanel`.
- `frontend/src/components/AnalysisChatPanel.jsx` — the persisted per-analysis chat UI (history load, send, quota display/countdown, delete-chat).
- `frontend/src/components/AnalysisTabs.jsx` — renders the Suggestions/Explanation/Refactor tabs on the Report page, calling `getSuggestions`/`getExplanation`/`getRefactor`.
- `frontend/src/lib/resources.js` — `startConversation`, `getChatHistory`, `sendChatMessage`, `clearChatHistory`, `getChatLimitStatus`, `getSuggestions`, `getExplanation`, `getRefactor`.
- `frontend/src/lib/useCountdown.js` — drives the "resets in…" countdown shown when the daily quota is exhausted.

## How it works

**The AI client (`ai/client.py`).** `_call_with_fallback(messages, budget=None)`
tries three providers in a fixed order — Groq (`_call_groq`, via the `groq` SDK,
`max_retries=0` and an explicit `AI_REQUEST_TIMEOUT_SECONDS` so one unresponsive
provider can't burn ~3 minutes of SDK-level retries before falling through),
then Gemini (`_call_gemini`, raw `requests.post` to the `generateContent` REST
endpoint, API key sent via the `x-goog-api-key` header — deliberately not
`?key=` in the query string, so the key never lands in a `requests.HTTPError`
message or the `exc_info=True` warning log), then OpenRouter (`_call_openrouter`,
`chat/completions`-compatible REST call). A provider with no API key configured
raises `RuntimeError` immediately and is skipped like any other failure — the
fallback treats "not configured" the same as "outage" or "rate limited."
`generate_text(prompt, system_instruction=None, budget=None)` builds a
single-turn `[system?, user]` message list; `generate_chat_reply(message,
history=None, system_instruction=None)` builds `[system?, ...history, user]` —
this is the one used by both `ai/views.ChatView` and `chat/views.SendMessageView`.
`budget` (an `ExecutionBudget`/`RequestBudget` from `core/execution_budget.py`)
is optional and only ever passed by the GitHub repository-context analysis path;
every other caller (both chat endpoints and all three `analyses/ai_views.py`
endpoints) calls with `budget=None`, so retries/timeouts/order are untouched for
them. When a budget is supplied and can't afford at least `MIN_AI_SLICE_SECONDS`
(8s) for the next provider, the chain raises `BudgetExceeded` rather than trying
a doomed short-timeout call.

**Prompt centralization (`ai/prompts.py`).** `BASE_CHAT_INSTRUCTION` is the
shared system-prompt opener for both chat surfaces. `build_analysis_context(analysis)`
renders an `Analysis` as text — name, language, quality score, LOC, its
`issues` list numbered 1..N (so the model's answers about "issue #3" match what
the UI shows), the full `source_code`, and (if set) `analysis.repo_context` for
GitHub-repo-backed analyses. Both `ai/views.ChatView` and
`chat/views.SendMessageView` call `BASE_CHAT_INSTRUCTION + build_analysis_context(analysis)`
to build the system instruction — this is the mechanism that keeps the two chat
surfaces describing an Analysis identically.

**Floating chat (`ai/views.ChatView`, `POST /api/ai/chat/`).** Stateless — no
DB persistence. Accepts `message`, an optional client-supplied `history` (capped
server-side to the last 20 turns via `ChatRequestSerializer.validate_history`,
each turn capped at 8000 chars), and an optional `analysis_id`. If `analysis_id`
is given, it's loaded scoped by `owner=request.user` (404 otherwise) and its
context is appended to the system instruction; otherwise it's a bare general
assistant. Throttled by `AIRateThrottle`. **No frontend code currently calls
this endpoint** — `frontend/src/lib/resources.js` has no wrapper for `/ai/chat/`
and no component references it; grepping the frontend for it turns up nothing.
The comment in `AnalysisChatPanel.jsx` ("distinct from the floating global
ChatWidget") and in `resources.js` ("distinct from the stateless floating
assistant above") describe an intended UI element that isn't wired up yet.

**Per-analysis suggestions/explanation/refactor (`analyses/ai_views.py`).**
All three (`SuggestionsView`, `ExplanationView`, `RefactorView`) share
`_get_owned_completed_analysis` (404 if not owned, 400 if `Analysis.status !=
COMPLETED`) and `_call_ai` (wraps `generate_text`, converts any exception to the
same 503 shape as the chat endpoints). Each is a `GET`, cache-first: if the
relevant `Analysis` field (`ai_suggestions`/`ai_explanation`/`ai_refactored_code`)
is already populated and `?regenerate=true` isn't passed, it returns the cached
value (`cached: true`) with no AI call. Suggestions are requested as a JSON
array (`_parse_suggestions`/`_normalize_suggestions` handle both the new
`{"category": "security"|"general", "text": ...}` shape and legacy flat-string
lists, falling back to treating each non-empty response line as an uncategorized
suggestion if the model doesn't return valid JSON). Refactor is requested as
`{"code": ..., "changes": [{"summary", "benefit"}, ...]}`
(`_parse_refactor_response`, falling back to treating the whole response as raw
code with no explanation on a parse failure); its cached form round-trips
`changes` through `analysis.ai_refactor_explanation` as a JSON string. All three
prompts append `_repo_context_block(analysis)` (from `analysis.repo_context`,
set only for GitHub-repo-file-backed analyses) so results account for how the
file is used elsewhere in its repo, not just its own contents.

**Persisted per-analysis chat (`chat/` app).** `Conversation` is `OneToOneField`
to `Analysis` (DB-enforced one conversation per analysis) and stores no
source/analysis data itself — every prompt re-reads `analysis` at send time.
`StartConversationView` (`POST /chat/start/<analysis_id>/`) get-or-creates the
`Conversation` for an owned analysis. `SendMessageView` (`POST /chat/message/`):
checks the quota first (`get_rate_limit_status`; 429 if `remaining <= 0`),
loads the `Conversation` scoped by `analysis__owner=request.user`, **saves the
user's `ChatMessage` immediately** (before calling the LLM, so the question is
never lost on AI failure), builds `history` from the previous `HISTORY_LIMIT =
20` messages (excluding the just-saved one, ordered oldest-first), calls
`generate_chat_reply`, and on success saves the assistant's reply as a second
`ChatMessage`. On AI failure it still returns 503 but with a message noting the
user's message was saved. `ChatHistoryView` GETs the full untruncated history
(no `HISTORY_LIMIT` cap — that only bounds what's sent to the LLM) and DELETEs
just the messages (not the `Conversation` row, so its id stays valid and a new
message doesn't need a fresh `StartConversationView` call).

**Daily quota (`chat/rate_limit.py`).** `DAILY_MESSAGE_LIMIT = 3`, global per
user across every analysis/conversation (not per-conversation — otherwise
trivially dodged by starting a new analysis), derived directly from `ChatMessage`
timestamps with no separate counter model. Resets at the user's own **local
midnight**, not a rolling 24h window or server time boundary: the client sends
`tz_offset_minutes` (JS `Date.getTimezoneOffset()` convention: UTC minus local,
so e.g. `-300` for UTC+5) on every quota-relevant request (`SendMessageView`,
`RateLimitStatusView`). `_clamp_offset` bounds it to `[-14*60, 12*60]` (real-world
UTC offset range) against malformed/malicious input. `_local_midnight_boundary`
computes the UTC instant of the most recent local midnight for that offset;
`get_rate_limit_status(user, tz_offset_minutes, now=None)` counts `ChatMessage`
rows with `role=USER` and `created_at__gte` that boundary, and returns
`{limit, used, remaining, reset_at}` — `reset_at` (next local midnight) is only
set once `used >= limit`, since that's the only time the frontend needs to
count down to it.

**Throttling.** `AIRateThrottle` (`core/throttling.py`, scope `'ai'`, `30/min`
in `settings.DEFAULT_THROTTLE_RATES`) is applied to `ai.views.ChatView`,
`chat.views.SendMessageView`, and all three of `SuggestionsView`/
`ExplanationView`/`RefactorView` — the DRF request-rate layer, separate from
and in addition to the daily-quota layer chat has on top.

**Frontend.** `AnalysisChatPanel.jsx` calls `startConversation` +
`getChatHistory` + `getChatLimitStatus` in parallel on mount, optimistically
appends the user's message locally before the network call resolves, then
replaces local state with the server's `getChatHistory` response on both
success and failure (since the backend persists the user message either way).
A 429 response's body (`err.data`, which mirrors `get_rate_limit_status`'s
shape) is used directly as the new `limitStatus` to show the countdown without
an extra round trip. `useCountdown(limitStatus?.reset_at, refreshLimitStatus)`
drives the "time until reset" display and re-fetches status once it elapses.
`AnalysisTabs.jsx` renders the Suggestions/Explanation/Refactor tabs via a
shared `useLazyAi`-style hook calling `getSuggestions`/`getExplanation`/
`getRefactor`, each accepting a `regenerate` boolean that maps to
`?regenerate=true`.

## Gotchas / non-obvious behavior
- All five AI-calling views (`ChatView`, `SendMessageView`, `SuggestionsView`,
  `ExplanationView`, `RefactorView`) catch AI exceptions the same way — a bare
  `except Exception` around the client call, returning HTTP 503 with
  `{"detail": "AI service is currently unavailable."}` (chat's version adds a
  note that the message was saved). Editing this contract in one call site
  without the others breaks consistency the frontend relies on.
- `ai/client.py` is **not** Groq-only despite the CLAUDE.md architecture note
  ("AI: one client") and module name — it's a three-provider fallback chain
  (Groq → Gemini → OpenRouter), each independently optional via its API key.
  A missing key just skips that provider silently (`RuntimeError` caught like
  any other failure).
- The floating/general-purpose chat backend (`ai.views.ChatView`, `POST
  /api/ai/chat/`) has **no current frontend caller** — no `resources.js`
  wrapper, no component reference. Comments in both `AnalysisChatPanel.jsx`
  and `resources.js` describe it as "the floating global ChatWidget" as if it
  exists in the UI; it doesn't yet. Don't assume a UI change to the floating
  widget will "just work" — it needs to be built first.
- The local-midnight quota reset depends entirely on the client-reported
  `tz_offset_minutes`; a client that never sends it (or a non-browser client)
  is treated as UTC. `SendMessageView` and `RateLimitStatusView` both default
  it to `0` when absent.
- History truncation differs by surface: the floating chat's `history` is
  fully client-supplied and capped server-side to 20 turns / 8000 chars each
  (an anti-abuse cap, since nothing else validates it); the persisted chat's
  `HISTORY_LIMIT = 20` only bounds what's sent to the LLM as context — the DB
  and `ChatHistoryView` always keep/return the complete conversation.
- `Conversation.messages` ordering is `Meta.ordering = ['created_at']`, and
  `SendMessageView` explicitly re-orders its `[:HISTORY_LIMIT]` slice (which is
  fetched `-created_at` to get the most recent N) back into chronological order
  via `reversed(...)` before building the prompt.
- `generate_text`'s `budget` parameter is a request-wide deadline used only by
  the GitHub repository-context analysis pipeline (multiple files, shared
  deadline across sandbox/Bandit/AI stages) — none of the suggestions/
  explanation/refactor/chat call sites in this feature pass one, so for them
  the fallback chain's per-provider timeout is always the full
  `AI_REQUEST_TIMEOUT_SECONDS`.
- Suggestion/refactor responses are parsed as JSON the model is asked (via
  prompt instructions, not a schema/function-calling constraint) to return —
  both `_parse_suggestions` and `_parse_refactor_response` have textual
  fallbacks for when the model doesn't comply, and `_normalize_suggestions`
  also upgrades pre-existing analyses cached before the "category" field
  existed (plain string lists) on the fly, with no data migration.
- `RefactorView`/`SuggestionsView`/`ExplanationView` require
  `Analysis.status == COMPLETED` (400 otherwise) — they read `analysis.issues`
  and `analysis.source_code`, which aren't reliably populated before that.

## Related features
- analysis-engine — the `Analysis` model/fields (`issues`, `source_code`,
  `repo_context`, `ai_suggestions`, `ai_explanation`, `ai_refactored_code`,
  `ai_refactor_explanation`) that every AI endpoint here reads from and caches
  onto.
- github-integration — `analysis.repo_context`, the `ExecutionBudget`/
  `RequestBudget` mechanism `generate_text` optionally accepts, and the PR
  review pipeline that is the other consumer of `ai/client.py`'s fallback
  chain logic (though not of the chat surfaces documented here).
- infra-cross-cutting — `core/throttling.py`'s `AIRateThrottle` and the
  general DRF throttle-scope pattern this feature reuses.
- frontend-app — `frontend/src/lib/api.js`'s `apiFetch` (CSRF/cookie/retry
  handling every call in `resources.js` relies on) and `AnalysisTabs.jsx`'s
  broader per-analysis tab structure that hosts the Suggestions/Explanation/
  Refactor tabs alongside the Chat link.

## Last updated
2026-08-28 — initial creation.
