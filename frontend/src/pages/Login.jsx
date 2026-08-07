import { useEffect, useState } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { GoogleLogin } from '@react-oauth/google'
import { useAuth } from '../lib/AuthContext'
import { requestPasswordReset, getGithubAuthLoginUrl } from '../lib/resources'
import { ApiError } from '../lib/api'
import { validateEmail, normalizeEmail } from '../lib/validation'
import GitHubIcon from '../components/GitHubIcon'

// GitHub's OAuth flow is a full-page redirect, not an in-page callback like
// Google's - errors round-trip back here as a query param instead of a JS
// callback. Mirrors GitHub.jsx's CALLBACK_ERROR_MESSAGE, plus the two
// account-linking errors specific to the login flow.
const GITHUB_ERROR_MESSAGE = {
  access_denied: 'GitHub authorization was cancelled.',
  missing_code: 'GitHub did not return an authorization code. Please try again.',
  invalid_state: 'The GitHub authorization link expired or was invalid. Please try again.',
  not_configured: 'GitHub sign-in is not configured on the server yet.',
  github_error: 'GitHub returned an error while signing you in.',
  email_not_verified: "GitHub didn't return a verified email for your account. Verify an email on GitHub, or sign in with a password.",
  account_conflict: 'This GitHub account is already linked to a different account.',
}

export default function Login() {
  const { login, loginWithGoogle } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [searchParams, setSearchParams] = useSearchParams()

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [fieldErrors, setFieldErrors] = useState({})
  const [forgotSent, setForgotSent] = useState(false)
  const [forgotBusy, setForgotBusy] = useState(false)
  const [githubBusy, setGithubBusy] = useState(false)

  useEffect(() => {
    const githubError = searchParams.get('error')
    if (githubError) {
      setError(GITHUB_ERROR_MESSAGE[githubError] || 'Could not sign in with GitHub.')
      setSearchParams({}, { replace: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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

  const handleGoogleSuccess = async (credentialResponse) => {
    setError('')
    try {
      const { isAdmin } = await loginWithGoogle(credentialResponse.credential)
      navigate(isAdmin ? '/admin' : location.state?.from?.pathname || '/dashboard', { replace: true })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Google sign-in failed. Please try again.')
    }
  }

  const handleGithubLogin = async () => {
    setError('')
    setGithubBusy(true)
    try {
      const { authorize_url } = await getGithubAuthLoginUrl()
      window.location.href = authorize_url
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not start GitHub sign-in.')
      setGithubBusy(false)
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
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, margin: '4px 0', color: 'var(--color-text-secondary-2)', fontSize: 12 }}>
            <div style={{ flex: 1, height: 1, background: 'var(--color-border)' }} />
            or continue with Google
            <div style={{ flex: 1, height: 1, background: 'var(--color-border)' }} />
          </div>
          <GoogleLogin
            onSuccess={handleGoogleSuccess}
            onError={() => setError('Google sign-in failed. Please try again.')}
            width={308}
          />
          <button
            type="button"
            onClick={handleGithubLogin}
            disabled={githubBusy}
            style={{
              width: 308,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 8,
              padding: '10px 14px',
              borderRadius: 10,
              border: 'none',
              background: '#24292f',
              color: '#fff',
              fontSize: 14,
              fontWeight: 500,
              cursor: 'pointer',
            }}
          >
            <GitHubIcon size={18} />
            {githubBusy ? 'Redirecting…' : 'Sign in with GitHub'}
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
