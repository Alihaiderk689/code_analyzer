# Feature memory index

Per-feature deep-dive documentation for this codebase, one level more detailed than the summaries in the root `CLAUDE.md`. See `CLAUDE.md`'s "Feature memory" section for the convention: read the relevant file before working on a feature, update it after changing that feature's files, add a new file (and a row here + in `CLAUDE.md`) for any feature area not yet covered.

| File | Covers |
|---|---|
| [auth-accounts.md](auth-accounts.md) | Cookie-JWT auth, CSRF double-submit, registration/email-OTP, Google/GitHub social login, password reset, throttling, profile/settings |
| [ai-chat.md](ai-chat.md) | `ai/client.py`'s provider fallback chain, floating chat, per-analysis suggestions/explanation/refactor, persisted per-analysis chat, chat daily quota |
| [analysis-engine.md](analysis-engine.md) | Static analysis engine (`ast`/pyflakes/textual checks), macOS Seatbelt runtime sandbox, `Analysis` lifecycle/status machine, quality score |
| [security-scanning.md](security-scanning.md) | Security Analysis Mode: Bandit + custom rules + AI remediation, `Analysis.security_report`, reuse by GitHub PR review |
| [github-integration.md](github-integration.md) | GitHub OAuth App connect flow, webhook → Celery → PR review pipeline, repo/PR/file browsing, token encryption, daily quotas |
| [infra-cross-cutting.md](infra-cross-cutting.md) | Settings/environment, the two rate-limiting systems, JSON error handling, structured logging, static/media serving, admin API, Docker topology |
| [frontend-app.md](frontend-app.md) | `api.js`/`resources.js` plumbing, AuthContext/ThemeContext, validation sync with backend, routing/shell components, build/test tooling |

Each file follows: Overview / Key files / How it works / Gotchas / Related features / Last updated.

## Known corrections vs. root CLAUDE.md

Research while building this index turned up two places where `CLAUDE.md`'s Architecture summaries had drifted from the code (now fixed there too — see its "AI: one client, three call sites" and "Static files" sections):
- `ai/client.py` is a **three-provider fallback chain** (Groq → Gemini → OpenRouter), not Groq-only.
- The media route in `config/urls.py` is **no longer `DEBUG`-gated** — it serves in every environment (changed for Render deploys, which have no nginx in front of gunicorn).

Also worth knowing: the floating/general-purpose chat backend (`ai.views.ChatView`) is fully implemented server-side but currently has **no frontend caller** — see `ai-chat.md`'s Gotchas.
