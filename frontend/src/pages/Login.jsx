import { useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../lib/AuthContext'
import { requestPasswordReset } from '../lib/resources'
import { ApiError } from '../lib/api'
import { validateEmail, normalizeEmail } from '../lib/validation'

export default function Login() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [fieldErrors, setFieldErrors] = useState({})
  const [forgotSent, setForgotSent] = useState(false)
  const [forgotBusy, setForgotBusy] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')

    const errors = {
      email: validateEmail(email),
      password: password ? '' : 'Password is required.',
    }
    setFieldErrors(errors)
    if (Object.values(errors).some(Boolean)) return

    setSubmitting(true)
    try {
      const { isAdmin } = await login(normalizeEmail(email), password)
      navigate(isAdmin ? '/admin' : location.state?.from?.pathname || '/dashboard', { replace: true })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong. Please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  const handleForgot = async (e) => {
    e.preventDefault()
    const emailError = validateEmail(email)
    if (emailError) {
      setError('Enter your email above first, then click "Forgot password?".')
      setFieldErrors((prev) => ({ ...prev, email: emailError }))
      return
    }
    setForgotBusy(true)
    try {
      await requestPasswordReset(normalizeEmail(email))
      setForgotSent(true)
    } catch {
      setForgotSent(true) // backend never reveals whether the email exists; show the same message
    } finally {
      setForgotBusy(false)
    }
  }

  return (
    <div className="auth-shell dotted-bg">
      <div className="auth-card">
        <div style={{ fontFamily: 'var(--font-display)', fontSize: 30, fontWeight: 500 }}>Welcome back</div>
        <div style={{ fontSize: 14, color: 'var(--color-text-secondary-2)', marginTop: 6 }}>
          Sign in to view your analyses.
        </div>
        <form onSubmit={handleSubmit} style={{ marginTop: 24, display: 'flex', flexDirection: 'column', gap: 12 }} noValidate>
          <input
            type="email"
            placeholder="Email address"
            className="field"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            onBlur={() => setFieldErrors((prev) => ({ ...prev, email: validateEmail(email) }))}
          />
          {fieldErrors.email && <div className="field-error">{fieldErrors.email}</div>}
          <input
            type="password"
            placeholder="Password"
            className="field"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onBlur={() => setFieldErrors((prev) => ({ ...prev, password: password ? '' : 'Password is required.' }))}
          />
          {fieldErrors.password && <div className="field-error">{fieldErrors.password}</div>}
          <div style={{ textAlign: 'right' }}>
            <button
              type="button"
              onClick={handleForgot}
              disabled={forgotBusy}
              style={{ background: 'none', border: 'none', cursor: 'pointer', fontSize: 13, color: 'var(--color-text-secondary-2)', padding: 0 }}
            >
              Forgot password?
            </button>
          </div>
          {forgotSent && <div className="msg-success">Reset link sent to your email.</div>}
          {error && <div className="msg-error">{error}</div>}
          <button type="submit" className="btn btn-dark btn-block" style={{ marginTop: 6 }} disabled={submitting}>
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
        <div style={{ textAlign: 'center', fontSize: 13, color: 'var(--color-text-secondary-2)', marginTop: 20 }}>
          No account?{' '}
          <Link to="/register" style={{ fontWeight: 600 }}>
            Create one
          </Link>
        </div>
      </div>
    </div>
  )
}
