import { apiFetch } from './api'

// Diagnostics
export const checkHealth = () => apiFetch('/health/', { auth: false })

// Auth
export const registerUser = (email, password, password2) =>
  apiFetch('/auth/register/', { method: 'POST', auth: false, body: { email, password, password2 } })

export const loginUser = (email, password) =>
  apiFetch('/auth/login/', { method: 'POST', auth: false, body: { email, password } })

export const logoutUser = (refresh) =>
  apiFetch('/auth/logout/', { method: 'POST', body: { refresh } })

export const requestPasswordReset = (email) =>
  apiFetch('/auth/forgot-password/', { method: 'POST', auth: false, body: { email } })

export const resetPassword = (uid, token, new_password, new_password2) =>
  apiFetch('/auth/reset-password/', {
    method: 'POST',
    auth: false,
    body: { uid, token, new_password, new_password2 },
  })

export const verifyEmail = (uid, token) =>
  apiFetch(`/auth/verify-email/?uid=${encodeURIComponent(uid)}&token=${encodeURIComponent(token)}`, {
    auth: false,
  })

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
export const downloadReportJson = (id) => apiFetch(`/reports/${id}/json/`, { responseType: 'blob' })
export const getReportHtml = (id) => apiFetch(`/reports/${id}/html/`, { responseType: 'blob' })

// AI chat
export const chatWithAi = (message, history, analysisId) =>
  apiFetch('/ai/chat/', { method: 'POST', body: { message, history, analysis_id: analysisId ?? null } })

// Chat with Your Code - persisted, per-analysis conversation (distinct from the
// stateless floating assistant above).
export const startConversation = (analysisId) => apiFetch(`/chat/start/${analysisId}/`, { method: 'POST' })
export const getChatHistory = (conversationId) => apiFetch(`/chat/history/${conversationId}/`)
export const sendChatMessage = (conversationId, message) =>
  apiFetch('/chat/message/', { method: 'POST', body: { conversation_id: conversationId, message } })
export const clearChatHistory = (conversationId) =>
  apiFetch(`/chat/history/${conversationId}/`, { method: 'DELETE' })

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
