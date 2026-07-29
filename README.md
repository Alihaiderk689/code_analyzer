# Code Analyzer

A full-stack code analysis platform: paste, upload, or connect a GitHub repository and get real static analysis, AI-powered explanations/refactors, and security scanning — plus automatic AI code review on every pull request.

Built with **Django REST Framework** (backend) and **React + Vite** (frontend).

## Features

**Code analysis**
- Paste code or upload a file; get real static analysis (`ast` + `pyflakes` + `parso` for Python — syntax errors, undefined names, unused imports), plus sandboxed execution to catch runtime errors (`ZeroDivisionError`, `IndexError`, etc.) that static analysis can't predict.
- Security scanning via Bandit (Python) and a custom rules engine (hardcoded secrets, CSRF/DEBUG misconfiguration, missing auth, path traversal, unsafe file uploads), enriched with AI-written explanations and remediation.
- Performance-smell detection (`range(len(...))`, missing HTTP timeouts, `SELECT *`, blocking sleeps).
- AI-generated suggestions, plain-language explanations, and a refactored version of your code with a side-by-side diff.
- "Chat with your code" — ask follow-up questions about a specific analysis (rate-limited per day).
- PDF/JSON/HTML report export, analysis history, and a dashboard with quality trends.

**GitHub integration**
- Connect a GitHub account (OAuth) and monitor one repository at a time.
- Automatic AI code review posted as a comment on every pull request — quality, security, and performance issues, scored.
- Browse a monitored repo's full file tree and analyze any one file per day (rate-limited to control GitHub API/AI cost), with the same issues/score/source-code/chat/refactor experience as pasted code.

**Accounts & admin**
- JWT auth with email verification and password reset.
- Admin dashboard: user management, analysis oversight, platform stats.

## Tech stack

| | |
|---|---|
| Backend | Django 4.2, Django REST Framework, Simple JWT, Celery + Redis, PostgreSQL |
| Frontend | React 19, Vite, React Router |
| Analysis | `ast`, `pyflakes`, `parso`, Bandit, macOS Seatbelt sandbox (`sandbox-exec`) |
| AI | Groq API (Llama 3.3) |
| GitHub | OAuth Apps + webhooks (HMAC-verified) |

## Project structure

```
backend/
  accounts/            auth, profile, email verification
  analyses/             analysis engine, security/AI services, reports
  ai/                   floating chat + shared AI client/prompts
  chat/                 "Chat with your code" (per-analysis conversations)
  adminapi/             admin dashboard endpoints
  github_integration/   OAuth, webhooks, PR review, file-check pipeline
  core/                 health check, shared plumbing
  config/               settings, root URL conf, Celery app

frontend/
  src/pages/            route-level views (Dashboard, Report, GitHub, ...)
  src/components/       shared UI (AnalysisTabs, IssueGroups, AppNav, ...)
  src/lib/              API client, auth context, formatting helpers
```

## Getting started

### Prerequisites
- Python 3.11+, Node 18+
- PostgreSQL (local dev) — or a Supabase/Postgres URL for production
- Redis (only needed for GitHub PR review's async pipeline)
- macOS, if you want sandboxed runtime-error detection (`sandbox-exec`) — it degrades gracefully to static-only analysis elsewhere

### Backend

```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in SECRET_KEY, DATABASE_URL_DEV, etc. - see below

python manage.py migrate
python manage.py runserver
```

To use the GitHub integration locally, also run a Celery worker and point Redis/ngrok at it:
```bash
celery -A config worker --loglevel=info
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

## Environment variables

Copy `backend/.env.example` to `backend/.env` and fill in real values. Key groups:

- **`ENVIRONMENT`** — `development` or `production`; picks `DATABASE_URL_DEV` vs `DATABASE_URL_PROD` and derives `DEBUG` automatically.
- **Django** — `SECRET_KEY`, `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `FRONTEND_URL`.
- **Email** — SMTP creds for auth emails; leave blank to print emails to the console instead.
- **`GROQ_API_KEY`** — powers AI suggestions/explanation/refactor and chat. Get one at [console.groq.com/keys](https://console.groq.com/keys).
- **Celery/Redis** — only required for the GitHub PR-review pipeline (webhook → queue → analyze → comment).
- **GitHub OAuth** — `GITHUB_CLIENT_ID`/`GITHUB_CLIENT_SECRET` from a [GitHub OAuth App](https://github.com/settings/developers), plus a webhook secret and a Fernet key (`GITHUB_TOKEN_ENCRYPTION_KEY`) to encrypt stored access tokens at rest. In local dev, `GITHUB_WEBHOOK_BASE_URL` needs to be a publicly reachable URL (e.g. ngrok).

Never commit `.env` — only `.env.example` is tracked.

## Running tests

```bash
# Backend
cd backend && python manage.py test

# Frontend
cd frontend && npm test && npm run lint && npm run build
```

CI (`.github/workflows/ci.yml`) runs both suites against a real Postgres instance on every push/PR to `main`/`dev`.

## API

The backend exposes a REST API under `/api/` (auth, analysis, dashboard, AI, chat, admin, GitHub integration, webhooks). See `backend/postman_collection.json` for a ready-to-import collection covering the full surface.
