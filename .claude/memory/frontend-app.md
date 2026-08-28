# Frontend Application Layer

## Overview
Every page and component in the React/Vite SPA sits on a small, deliberately centralized plumbing layer: one fetch wrapper, one module of backend-endpoint functions, two app-wide contexts (auth, theme), one validation module mirroring backend rules, and a handful of shell/chrome components that wire routing and auth-gating together. The centralization is a policy, not an accident — `api.js` is the only place that knows about CSRF/cookies/refresh, `resources.js` is the only place that knows an endpoint URL, so a backend contract change or an auth-flow change touches one file instead of being scattered across every page.

## Key files
- `frontend/src/lib/api.js` — `apiFetch`, the single fetch wrapper: CSRF header injection, cookie credentials, network-failure-to-`ApiError` wrapping, automatic refresh-and-retry on 401, `primeCsrf()`, `reportClientError()`.
- `frontend/src/lib/resources.js` — every backend endpoint call, grouped by domain, built on top of `apiFetch`.
- `frontend/src/lib/AuthContext.jsx` — `AuthProvider`/`useAuth`: bootstraps session state on app load, exposes `user`, `isAdmin`, `initializing`, `offline`, `adminCheckFailed`, `login`, `loginWithGoogle`, `logout`, `refreshUser`.
- `frontend/src/lib/ThemeContext.jsx` — `ThemeProvider`/`useTheme`: light/dark theme state, synced with `localStorage` and OS preference.
- `frontend/src/lib/validation.js` — client-side mirrors of `backend/accounts/validators.py` (name/username/email/password/OTP rules).
- `frontend/src/lib/useCountdown.js` — hook that ticks a live "come back in..." countdown toward a server-supplied `resetAt` timestamp, then re-checks with the server.
- `frontend/src/lib/format.js` — display-formatting helpers/constants: score color/rounding, date/datetime formatting, issue-type and severity label/color maps.
- `frontend/src/App.jsx` — top-level `<Routes>` tree: marketing shell, guest-only routes, protected/user routes, admin routes.
- `frontend/src/components/AppNav.jsx` — signed-in top nav (links, theme toggle, avatar/settings, logout).
- `frontend/src/components/MarketingNav.jsx` — signed-out top nav (marketing links, sign in / start free).
- `frontend/src/components/MarketingLayout.jsx` — layout for `/`, `/login`, `/register`; picks `AppNav` vs `MarketingNav` based on auth state.
- `frontend/src/components/ProtectedRoute.jsx` — route guards: default export (`ProtectedRoute`, requires login + renders `AppNav`), `AdminRoute`, `UserRoute` (bounces admins away from regular-user pages), `GuestRoute` (bounces logged-in users away from login/register).
- `frontend/src/components/ErrorBoundary.jsx` — class-component React error boundary; catches render crashes, reports via `reportClientError`, shows a reload screen instead of a blank page.
- `frontend/src/components/ConnectivityBanner.jsx` — pings `/health/` once on mount; shows a persistent "can't reach the backend" banner if it fails.
- `frontend/vite.config.js` — Vite build config; injects a build-time CSP `<meta>` tag whose `connect-src`/`img-src` are derived from `VITE_API_BASE_URL`.
- `frontend/vitest.config.js` — standalone test config, Node environment, `src/**/*.test.js` only.

## How it works

**`apiFetch` (`api.js`)** is the only function in the app that calls `fetch()` against the backend. Behavior:
- Builds the URL from `API_BASE_URL` (`VITE_API_BASE_URL` or `http://localhost:8000/api`) + the given path.
- Always sends `credentials: 'include'` so the httpOnly `access_token`/`refresh_token` cookies ride along; the frontend never touches raw tokens.
- Reads the JS-readable `csrftoken` cookie fresh from `document.cookie` on every call (`csrfHeaders()`) and sets `X-CSRFToken`; this is what the backend's `CookieJWTAuthentication` CSRF check reads for cookie-sourced auth.
- Sets `Content-Type: application/json` unless `isForm: true` (form uploads pass a `FormData` body untouched).
- `safeFetch()` wraps the raw `fetch()` call: a genuine network failure (offline, DNS, CORS, backend down) never becomes a rejected promise the caller has to special-case — it becomes `ApiError(status=0, ...)`, so callers checking `err.status` can tell "never got a response" apart from any real HTTP status.
- On a `401` (and `opts.auth !== false`), it calls `refreshAccessToken()` (POSTs `/auth/refresh/`, which reads the refresh cookie server-side — no body) and retries the original request once. Concurrent 401s share one in-flight refresh via a module-level `refreshPromise` so simultaneous requests don't fire N parallel refreshes. If refresh itself fails with a real error (not status 0), it's surfaced as `ApiError(401, { detail: 'Session expired...' })`; a status-0 failure during refresh is rethrown as-is so offline is still distinguishable from expired.
- Non-OK responses (after any retry) throw `ApiError(res.status, <parsed JSON body or null>)`. `ApiError.message` is derived by `extractMessage()`: DRF's `detail`/`non_field_errors` are shown verbatim (whole-request errors), otherwise the first field's first error is prefixed with its field name (`"email: ..."`) so it's locatable on a multi-field form.
- `responseType: 'blob'` (PDF/HTML report downloads) skips JSON parsing; a `204` response returns `null`.
- `primeCsrf()` — GETs `/auth/csrf/` to obtain the CSRF cookie; must run before the first mutating request of a session (see Gotchas).
- `reportClientError(message, error, context)` — the single funnel for client-side errors that would otherwise be silently swallowed (currently just `console.error`; framed as the one place to later wire a real error-tracking service). Used by `AuthContext` (offline/admin-check failures) and `ErrorBoundary` (render crashes).

**`resources.js`** is confirmed as the sole place every backend endpoint is called from — no page or component calls `apiFetch` directly. It's one flat file organized into commented groups matching backend app boundaries: Diagnostics (`checkHealth`), Auth (register/login/google/github/logout/password reset/OTP verify), profile (fetch/update/avatar/delete), Analyses (submit/upload/get/delete/reanalyze, AI suggestions/explanation/refactor), History, Search, Dashboard, Reports (blob downloads), Chat (persisted per-analysis conversation — distinct from the floating assistant), GitHub integration (status/repos/PRs/metrics/trends/file tree/file content/index status/quotas), and Admin (users/analyses/stats). Several GitHub/chat functions append a `tz_offset_minutes` query param/body field (via a shared `getTzOffsetMinutes()` = `Date.getTimezoneOffset()`) for the local-midnight quota reset described in the root CLAUDE.md.

**`AuthContext.jsx`** bootstraps session state once per app load in a `useEffect`: calls `primeCsrf()` first, then `fetchProfile()` — a 401 (even after `apiFetch`'s automatic refresh retry) means "not logged in" with nothing else to do, while an `ApiError(status===0)` means the server is unreachable and sets `offline: true` (rendered distinctly rather than dumping the user on a login form that can't work anyway). Because the profile endpoint never returns `is_staff`, admin status is determined by *probing* an admin-only endpoint (`adminStats()`) via `checkIsAdmin()`: success means admin, 401/403 means definitively not-admin, and any other failure (network/5xx) returns `indeterminate: true` and denies admin access anyway (fail closed) while surfacing `adminCheckFailed` so the ambiguous case is diagnosable rather than silently downgrading a real admin for the session. `login`/`loginWithGoogle` both re-run `checkIsAdmin()` and must unwrap its `{isAdmin, indeterminate}` return — the code carries an explicit comment noting a past bug where storing the object directly made `isAdmin` truthy for every account. `login()` also picks up a pending first/last name staged in `sessionStorage` by `Register.jsx` (best-effort `updateProfile` call; failure doesn't block login).

**`ThemeContext.jsx`** reads its initial state from `document.documentElement.dataset.theme`, which an inline script in `index.html` already set synchronously pre-mount (avoiding flash-of-wrong-theme) — the context doesn't duplicate that resolution logic. `toggleTheme`/`setTheme` write to both the DOM dataset and `localStorage` (wrapped in try/catch for private-browsing/quota failures). If the user never made an explicit choice (`localStorage` empty), a `matchMedia('(prefers-color-scheme: dark)')` listener keeps following the OS setting live.

**`validation.js`** duplicates regex/length rules from `backend/accounts/validators.py` field-for-field: `NAME_RE`/`validateName` ↔ `validate_person_name`, `USERNAME_RE`/`validateUsername` ↔ `validate_username_format`, `EMAIL_RE`/`validateEmail` + `normalizeEmail` ↔ `normalize_email`, `getPasswordChecks`/`validatePassword`/`isPasswordStrong` ↔ `validate_password_strength` (length 8-128, upper/lower/digit/special), `OTP_CODE_RE`/`validateOtpCode` ↔ `validate_otp_code` (6 digits). This is purely client-side UX (instant feedback, password-strength checklist); the backend re-validates everything regardless, so client/server drift is a UX bug, not a security hole — but it must still be kept in sync manually, there is no shared source of truth or codegen.

**`format.js`** and **`useCountdown.js`** are presentational/UI-state helpers, not data-fetching: `format.js` centralizes score-to-color thresholds, date formatting, and label/color lookup tables for analysis status, issue types, and severity levels (severity map is shared by GitHub PR file-analysis findings, both security and quality). `useCountdown(resetAt, onExpire)` ticks every second while a server-given ISO timestamp is in the future and calls `onExpire` once it passes (used for quota "come back in..." UI), re-checking with the server rather than trusting the client clock alone.

**Routing (`App.jsx`)** is a flat `<Routes>` tree, not nested per-page config. Three tiers: (1) `MarketingLayout` wraps `/` plus a `GuestRoute`-guarded `/login`/`/register` — layout itself picks `AppNav` vs `MarketingNav` based on live auth state so a logged-in user landing on `/` still sees the real app nav; (2) `/verify-email` and `/reset-password` are top-level, ungated (pre-auth flows reached via emailed links, not requiring an existing session); (3) everything else is wrapped in `ProtectedRoute` (requires `user`, renders `AppNav` + `Outlet`, redirects to `/login` with `state.from` otherwise), and inside that split further into `UserRoute` (bounces admins to `/admin`) for the ordinary-user pages (dashboard/analyze/history/report/chat/github/*) vs. `AdminRoute` (bounces non-admins to `/dashboard`) for `/admin`. `/settings` sits directly under `ProtectedRoute` without a `UserRoute`/`AdminRoute` split — both roles can reach it. All three guard components render a `page-loading` placeholder while `initializing` is true, so no route decision is made before the auth bootstrap in `AuthContext` resolves.

**App shell components**: `AppNav.jsx` — signed-in top nav; shows admin-only link when `isAdmin`, otherwise dashboard/analyze/history/github links, plus theme toggle and logout. `ProtectedRoute.jsx` — see routing above. `ErrorBoundary.jsx` — class-based (hooks can't do this), catches render/lifecycle crashes below it, reports via `reportClientError`, shows a reload screen instead of a blank white page. `ConnectivityBanner.jsx` — one-shot `/health/` probe on mount, shows a persistent red banner if the backend can't be reached (rendered at the very top of `App.jsx`, outside `<Routes>`, so it's visible on every page). `MarketingLayout.jsx` — swaps nav based on auth state for the marketing-facing routes. `MarketingNav.jsx` — signed-out nav with marketing anchor links and sign-in/register CTAs.

**Build/test tooling**: `vite.config.js` builds the SPA with `@vitejs/plugin-react`, plus a custom `inject-csp` plugin that writes a `Content-Security-Policy` `<meta>` tag at build time (derived from `VITE_API_BASE_URL`'s origin) as the very first thing in `<head>` — a `<meta>` CSP only governs elements after it in the DOM, and it can't express `frame-ancestors`/`report-to` (those need a real HTTP header, set at the hosting/proxy layer, out of this repo's scope). Lint is `oxlint` (`npm run lint`), not ESLint. Tests are `vitest run` (`npm test`), but `vitest.config.js` is deliberately standalone from `vite.config.js` — Node environment, no React/DOM plugin, `include: ['src/**/*.test.js']` only — so it exercises pure-JS lib modules (`api.js`, `format.js`) and nothing that imports JSX or touches the DOM. There is no component or page-level test setup (no jsdom, no React Testing Library) anywhere in this repo.

**Pages** (`frontend/src/pages/`, business logic covered by other memory files — listed here only for orientation):
- `Landing.jsx` — public marketing home page (features/languages/pricing).
- `Login.jsx` — email/password + Google + GitHub OAuth sign-in.
- `Register.jsx` — account creation, stages pending name/email for post-verification.
- `VerifyEmail.jsx` — OTP email verification step after registration.
- `ResetPassword.jsx` — password reset via emailed uid/token link.
- `Settings.jsx` — profile edit, password change, avatar upload, account deletion.
- `Dashboard.jsx` — signed-in user's analysis stats/summary landing page.
- `NewAnalysis.jsx` — paste-or-upload code submission for analysis.
- `History.jsx` — list/search/delete past analyses.
- `Report.jsx` — single analysis result view (issues, PDF/HTML export, reanalyze/delete).
- `AnalysisChat.jsx` — persisted per-analysis AI chat thread.
- `GitHub.jsx` — GitHub connect/disconnect and repository selection.
- `GitHubPullRequests.jsx` — list of monitored repos' PRs plus quality-trend metrics.
- `GitHubPullRequestDetail.jsx` — single PR's AI review detail.
- `GitHubRepositoryFiles.jsx` — repo file browser with per-file (and per-file-with-context) analysis.
- `Admin.jsx` — admin-only overview/users/analyses management tabs.

## Gotchas / non-obvious behavior
- `primeCsrf()` must run before the first mutating request of a session (it does, inside `AuthContext`'s bootstrap `useEffect`, on every app load) — without the `csrftoken` cookie it sets, every POST/PATCH/DELETE 403s with "CSRF token missing," because cookie-sourced JWT auth enforces CSRF (see root CLAUDE.md).
- `apiFetch`'s 401-retry only fires when `opts.auth !== false` — endpoints explicitly marked `auth: false` in `resources.js` (register, login, google, github-login-url, forgot-password, reset-password, verify-email, resend-verification, health check) never trigger a refresh attempt, by design, since there's no session to refresh yet.
- `ApiError.status === 0` is a distinct signal from any real HTTP status — it means the request never reached the server at all (network/CORS/DNS/offline). Code that conflates it with "not logged in" or a generic failure will misbehave (e.g. `AuthContext` explicitly branches on it to avoid showing a login form to someone who's actually just offline).
- `checkIsAdmin()`'s `{ isAdmin, indeterminate }` return must be unwrapped, not stored directly as a truthy/falsy admin flag — the code has an explicit historical-bug comment about this exact mistake routing every signed-in user to `/admin`.
- `validation.js` is a hand-maintained mirror of `backend/accounts/validators.py`; there is no shared schema or generation step, so any change to name/username/email/password/OTP rules on the backend must be manually ported here or the client-side checklist/errors silently drift out of sync with what the server actually accepts.
- Vitest (`vitest.config.js`) only covers `src/**/*.test.js` under plain Node — no DOM, no JSX, no component/page tests exist anywhere in the frontend. Any change to a page, context, or shell component has no automated regression coverage and needs manual browser verification; only pure-JS lib modules (`api.js`, `format.js`) get real test coverage.
- The CSP is injected as a `<meta>` tag at build time from `VITE_API_BASE_URL`'s origin — changing the API's origin in production requires a rebuild (the CSP isn't runtime-configurable), and directives needing a real HTTP header (`frame-ancestors`, `report-to`) can't be expressed this way at all.
- `MarketingLayout` re-derives which nav to show from live `AuthContext` state on every render — a logged-in user visiting `/` sees `AppNav`, not the signed-out marketing nav; this is deliberate, not a bug, per the comment in the file.
- `UserRoute` and `AdminRoute` are mutually exclusive redirects (admin → `/admin` only, non-admin → never `/admin`), but `/settings` bypasses both and is reachable by either role — don't assume every route under `ProtectedRoute` is role-split.

## Related features
- auth-accounts — CSRF/cookie/refresh mechanics live here as shared plumbing (`api.js`, `AuthContext.jsx`), but login/register/OTP/password-reset business logic and backend semantics belong to that memory file.
- ai-chat — the floating assistant and `AnalysisChat.jsx`'s persisted chat both call through `resources.js`/`apiFetch`, but prompt-building and chat business logic belong to that memory file.
- analysis-engine — `NewAnalysis.jsx`, `Report.jsx`, `History.jsx`, `Dashboard.jsx` are hosted by this routing/shell layer; the analysis pipeline itself belongs to that memory file.
- security-scanning — surfaced through the same `Report.jsx`/analysis pages; scanning internals belong to that memory file.
- github-integration — `GitHub.jsx`, `GitHubPullRequests.jsx`, `GitHubPullRequestDetail.jsx`, `GitHubRepositoryFiles.jsx` are hosted here and call the GitHub group in `resources.js`; the OAuth/webhook/PR-review pipeline belongs to that memory file.

## Last updated
2026-08-28 — initial creation.
