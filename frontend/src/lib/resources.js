import { apiFetch } from './api'

// Diagnostics
export const checkHealth = () => apiFetch('/health/', { auth: false })

// Auth
export const registerUser = (email, password, password2) =>
  apiFetch('/auth/register/', { method: 'POST', auth: false, body: { email, password, password2 } })

export const loginUser = (email, password) =>
  apiFetch('/auth/login/', { method: 'POST', auth: false, body: { email, password } })

// Serves both login and signup - the backend finds-or-creates the user from
// the verified Google access token, so the frontend doesn't need to know
// which one it'll turn out to be.
export const googleLogin = (accessToken) =>
  apiFetch('/auth/google/', { method: 'POST', auth: false, body: { access_token: accessToken } })

// Kicks off the redirect-based GitHub OAuth flow (no ID-token shortcut like
// Google's) - the frontend navigates the browser to the returned
// authorize_url; GitHub eventually redirects back with the session already
// established via cookies, same as every other login method.
export const getGithubAuthLoginUrl = () => apiFetch('/auth/github/', { auth: false })

// No refresh token to send - the backend reads it from its own httpOnly cookie.
export const logoutUser = () => apiFetch('/auth/logout/', { method: 'POST' })

export const requestPasswordReset = (email) =>
  apiFetch('/auth/forgot-password/', { method: 'POST', auth: false, body: { email } })

export const resetPassword = (uid, token, new_password, new_password2) =>
  apiFetch('/auth/reset-password/', {
    method: 'POST',
    auth: false,
    body: { uid, token, new_password, new_password2 },
  })

export const verifyOtp = (email, code) =>
  apiFetch('/auth/verify-email/', { method: 'POST', auth: false, body: { email, code } })

export const resendVerification = (email) =>
  apiFetch('/auth/resend-verification/', { method: 'POST', auth: false, body: { email } })

export const fetchProfile = () => apiFetch('/users/profile/')
export const updateProfile = (patch) => apiFetch('/users/profile/', { method: 'PATCH', body: patch })

export const changePassword = (old_password, new_password, new_password2) =>
  apiFetch('/auth/change-password/', { method: 'POST', body: { old_password, new_password, new_password2 } })

export const deleteAccount = (password) => apiFetch('/users/profile/', { method: 'DELETE', body: { password } })

export const uploadAvatar = (file) => {
  const form = new FormData()
  form.append('avatar', file)
  return apiFetch('/users/avatar/', { method: 'POST', isForm: true, body: form })
}

// Analyses
export const submitAnalysis = (name, code) =>
  apiFetch('/analysis/analyze/', { method: 'POST', body: { name, code } })

export const uploadAnalysis = (file, name) => {
  const form = new FormData()
  form.append('file', file)
  if (name) form.append('name', name)
  return apiFetch('/analysis/upload/', { method: 'POST', isForm: true, body: form })
}

export const getAnalysis = (id) => apiFetch(`/analysis/${id}/`)
export const deleteAnalysis = (id) => apiFetch(`/analysis/${id}/`, { method: 'DELETE' })
export const reanalyzeAnalysis = (id) => apiFetch(`/analysis/${id}/reanalyze/`, { method: 'POST' })

export const getSuggestions = (id, regenerate = false) =>
  apiFetch(`/analysis/${id}/suggestions/${regenerate ? '?regenerate=true' : ''}`)
export const getExplanation = (id, regenerate = false) =>
  apiFetch(`/analysis/${id}/explanation/${regenerate ? '?regenerate=true' : ''}`)
export const getRefactor = (id, regenerate = false) =>
  apiFetch(`/analysis/${id}/refactor/${regenerate ? '?regenerate=true' : ''}`)

// History
export const listHistory = () => apiFetch('/history/')
export const clearHistory = () => apiFetch('/history/clear/', { method: 'DELETE' })

// Search
export const searchAnalyses = (q) => apiFetch(`/search/?q=${encodeURIComponent(q)}`)

// Dashboard
export const getDashboard = () => apiFetch('/dashboard/')
export const getDashboardStats = () => apiFetch('/dashboard/stats/')
export const getDashboardRecent = (limit = 10) => apiFetch(`/dashboard/recent/?limit=${limit}`)
export const getDashboardLanguages = () => apiFetch('/dashboard/languages/')
export const getDashboardScores = () => apiFetch('/dashboard/scores/')

// Reports
export const downloadReportPdf = (id) => apiFetch(`/reports/${id}/pdf/`, { responseType: 'blob' })
export const getReportHtml = (id) => apiFetch(`/reports/${id}/html/`, { responseType: 'blob' })

// Chat with Your Code - persisted, per-analysis conversation (distinct from the
// stateless floating assistant above).
// The daily chat quota resets at the user's own local midnight, not server
// time - the backend has no way to know the device's timezone on its own, so
// every quota-relevant request reports it (JS Date.getTimezoneOffset()
// convention: UTC minus local, in minutes - e.g. -300 for UTC+5).
const getTzOffsetMinutes = () => new Date().getTimezoneOffset()

export const startConversation = (analysisId) => apiFetch(`/chat/start/${analysisId}/`, { method: 'POST' })
export const getChatHistory = (conversationId) => apiFetch(`/chat/history/${conversationId}/`)
export const sendChatMessage = (conversationId, message) =>
  apiFetch('/chat/message/', {
    method: 'POST',
    body: { conversation_id: conversationId, message, tz_offset_minutes: getTzOffsetMinutes() },
  })
export const clearChatHistory = (conversationId) =>
  apiFetch(`/chat/history/${conversationId}/`, { method: 'DELETE' })
export const getChatLimitStatus = () => apiFetch(`/chat/limit/?tz_offset_minutes=${getTzOffsetMinutes()}`)

// GitHub integration
export const getGitHubStatus = () => apiFetch('/github/status/')
export const getGitHubLoginUrl = () => apiFetch('/github/login/')
export const disconnectGitHub = () => apiFetch('/github/disconnect/', { method: 'POST' })

// Live list of every repo the user has on GitHub, each flagged is_monitored -
// distinct from listMonitoredGitHubRepositories, which is DB-only (no GitHub API call).
export const listGitHubRepositories = () => apiFetch('/github/repositories/')
export const selectGitHubRepository = (repositoryId, repositoryName) =>
  apiFetch('/github/repositories/select/', {
    method: 'POST',
    body: { repository_id: repositoryId, repository_name: repositoryName },
  })
export const listMonitoredGitHubRepositories = () => apiFetch('/github/repositories/monitored/')
export const deselectGitHubRepository = (pk) => apiFetch(`/github/repositories/${pk}/`, { method: 'DELETE' })

export const listGitHubPullRequests = ({ repositoryId, page } = {}) => {
  const params = new URLSearchParams()
  if (repositoryId) params.set('repository_id', repositoryId)
  if (page) params.set('page', page)
  const qs = params.toString()
  return apiFetch(`/github/pull-requests/${qs ? `?${qs}` : ''}`)
}
export const getGitHubPullRequest = (id) => apiFetch(`/github/pull-requests/${id}/`)
export const getGitHubMetrics = ({ repositoryId } = {}) => {
  const params = new URLSearchParams()
  if (repositoryId) params.set('repository_id', repositoryId)
  const qs = params.toString()
  return apiFetch(`/github/metrics/${qs ? `?${qs}` : ''}`)
}
export const getGitHubQualityTrends = ({ repositoryId, days } = {}) => {
  const params = new URLSearchParams()
  if (repositoryId) params.set('repository_id', repositoryId)
  if (days) params.set('days', days)
  const qs = params.toString()
  return apiFetch(`/github/pull-requests/trends/${qs ? `?${qs}` : ''}`)
}

// Browsing a repo's file tree is free (one GitHub API call, cached client-side
// per page load); analyzing a file costs the daily quota below.
export const getGitHubRepositoryTree = (pk) => apiFetch(`/github/repositories/${pk}/tree/`)

// Fetching a single file's raw source is also free - lets the file browser
// show code the moment it's clicked, before the user opts into spending
// today's quota on the "Analyze" button (analyzeGitHubFile, below).
export const getGitHubFileContent = (pk, path) =>
  apiFetch(`/github/repositories/${pk}/file/?path=${encodeURIComponent(path)}`)

// Status of the dependency-graph build that runs automatically after a repo
// is selected (see backend RepositoryIndexStatusView) - lets the file browser
// show "Understanding repository..." while it's running.
export const getGitHubRepositoryIndexStatus = (pk) => apiFetch(`/github/repositories/${pk}/index/`)
// There's no push webhook to keep the graph fresh automatically, so this is
// the way to refresh it after pushing new commits.
export const rebuildGitHubRepositoryIndex = (pk) => apiFetch(`/github/repositories/${pk}/reindex/`, { method: 'POST' })

export const getFileCheckQuota = () => apiFetch(`/github/file-checks/quota/?tz_offset_minutes=${getTzOffsetMinutes()}`)

export const analyzeGitHubFile = (pk, path) =>
  apiFetch(`/github/repositories/${pk}/analyze-file/`, {
    method: 'POST',
    body: { path, tz_offset_minutes: getTzOffsetMinutes() },
  })

// "Analyze with repo context" - like analyzeGitHubFile above, but also
// analyzes the file's direct dependency-graph neighbors (what it imports,
// what imports it), so the result shows a change's impact across files
// instead of just the one file in isolation. Costs more (several GitHub
// fetches, possibly several Groq calls), so it's tracked under its own
// separate daily quota - see getContextCheckQuota.
export const getContextCheckQuota = () => apiFetch(`/github/context-checks/quota/?tz_offset_minutes=${getTzOffsetMinutes()}`)

export const analyzeGitHubFileWithContext = (pk, path) =>
  apiFetch(`/github/repositories/${pk}/analyze-file-context/`, {
    method: 'POST',
    body: { path, tz_offset_minutes: getTzOffsetMinutes() },
  })

// Admin
export const adminListUsers = (q = '', isActive = null) => {
  const params = new URLSearchParams()
  if (q) params.set('q', q)
  if (isActive != null) params.set('is_active', String(isActive))
  const qs = params.toString()
  return apiFetch(`/admin/users/${qs ? `?${qs}` : ''}`)
}
export const adminDeleteUser = (pk) => apiFetch(`/admin/users/${pk}/`, { method: 'DELETE' })

export const adminListAnalyses = ({ status, language, ownerId } = {}) => {
  const params = new URLSearchParams()
  if (status) params.set('status', status)
  if (language) params.set('language', language)
  if (ownerId) params.set('owner_id', ownerId)
  const qs = params.toString()
  return apiFetch(`/admin/analysis/${qs ? `?${qs}` : ''}`)
}

export const adminStats = () => apiFetch('/admin/stats/')
