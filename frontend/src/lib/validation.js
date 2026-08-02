const NAME_RE = /^[A-Za-z][A-Za-z '-]*$/
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
const USERNAME_RE = /^[A-Za-z0-9][A-Za-z0-9_]*$/

export function validateName(value, label = 'This field') {
  const trimmed = (value || '').trim()
  if (!trimmed) return `${label} is required.`
  if (trimmed.length < 2 || trimmed.length > 50) return `${label} must be 2-50 characters.`
  if (!NAME_RE.test(trimmed)) return `${label} may only contain letters, spaces, hyphens, and apostrophes.`
  return ''
}

export function validateUsername(value) {
  const trimmed = (value || '').trim()
  if (!trimmed) return 'Username is required.'
  if (trimmed.length < 3 || trimmed.length > 20) return 'Username must be 3-20 characters.'
  if (trimmed.startsWith('_')) return 'Username cannot start with an underscore.'
  if (!USERNAME_RE.test(trimmed)) return 'Username may only contain letters, numbers, and underscores.'
  return ''
}

export function normalizeEmail(value) {
  return (value || '').trim().toLowerCase()
}

export function validateEmail(value) {
  const normalized = normalizeEmail(value)
  if (!normalized) return 'Email is required.'
  if (!EMAIL_RE.test(normalized)) return 'Enter a valid email address.'
  return ''
}

export function getPasswordChecks(value) {
  const password = value || ''
  return {
    length: password.length >= 8 && password.length <= 128,
    uppercase: /[A-Z]/.test(password),
    lowercase: /[a-z]/.test(password),
    number: /\d/.test(password),
    special: /[^A-Za-z0-9]/.test(password),
  }
}

export function isPasswordStrong(value) {
  return Object.values(getPasswordChecks(value)).every(Boolean)
}

export function validatePassword(value) {
  const checks = getPasswordChecks(value)
  if (!checks.length) return 'Password must be 8-128 characters.'
  if (!checks.uppercase) return 'Password must contain an uppercase letter.'
  if (!checks.lowercase) return 'Password must contain a lowercase letter.'
  if (!checks.number) return 'Password must contain a number.'
  if (!checks.special) return 'Password must contain a special character.'
  return ''
}

export function validateConfirmPassword(password, confirm) {
  if (!confirm) return 'Please confirm your password.'
  if (password !== confirm) return 'Passwords do not match.'
  return ''
}
