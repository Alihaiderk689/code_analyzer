import { useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../lib/AuthContext'
import { requestPasswordReset } from '../lib/resources'
import { ApiError } from '../lib/api'

export default function Login() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [forgotSent, setForgotSent] = useState(false)
  const [forgotBusy, setForgotBusy] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setSubmitting(true)
    try {
      const { isAdmin } = await login(email, password)
      navigate(isAdmin ? '/admin' : location.state?.from?.pathname || '/dashboard', { replace: true })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong. Please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  const handleForgot = async (e) => {
    e.preventDefault()
    if (!email) {
      setError('Enter your email above first, then click "Forgot password?".')
      return
    }
    setForgotBusy(true)
    try {
      await requestPasswordReset(email)
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
        <form onSubmit={handleSubmit} style={{ marginTop: 24, display: 'flex', flexDirection: 'column', gap: 12 }}>
          <input
            type="email"
            required
            placeholder="Email address"
            className="field"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
          <input
            type="password"
            required
            placeholder="Password"
            className="field"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
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
