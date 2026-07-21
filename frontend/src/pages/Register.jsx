import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { registerUser } from '../lib/resources'
import { ApiError } from '../lib/api'

export default function Register() {
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [password2, setPassword2] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    if (password !== password2) {
      setError('Passwords do not match.')
      return
    }
    setSubmitting(true)
    try {
      await registerUser(email, password, password2)
      if (name.trim()) sessionStorage.setItem('ca_pending_name', name.trim())
      navigate('/verify-email', { state: { email } })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong. Please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="auth-shell dotted-bg">
      <div className="auth-card">
        <div style={{ fontFamily: 'var(--font-display)', fontSize: 30, fontWeight: 500 }}>Create your account</div>
        <div style={{ fontSize: 14, color: 'var(--color-text-secondary-2)', marginTop: 6 }}>
          Start analyzing code in seconds.
        </div>
        <form onSubmit={handleSubmit} style={{ marginTop: 24, display: 'flex', flexDirection: 'column', gap: 12 }}>
          <input
            type="text"
            placeholder="Full name"
            className="field"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
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
          <input
            type="password"
            required
            placeholder="Confirm password"
            className="field"
            value={password2}
            onChange={(e) => setPassword2(e.target.value)}
          />
          {error && <div className="msg-error">{error}</div>}
          <button type="submit" className="btn btn-dark btn-block" style={{ marginTop: 6 }} disabled={submitting}>
            {submitting ? 'Creating account…' : 'Create account'}
          </button>
          <div style={{ fontSize: 12, color: 'var(--color-text-muted)', textAlign: 'center', marginTop: 4 }}>
            We'll send a verification link to your email.
          </div>
        </form>
        <div style={{ textAlign: 'center', fontSize: 13, color: 'var(--color-text-secondary-2)', marginTop: 20 }}>
          Already have an account?{' '}
          <Link to="/login" style={{ fontWeight: 600 }}>
            Sign in
          </Link>
        </div>
      </div>
    </div>
  )
}
