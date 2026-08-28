import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useGoogleLogin } from '@react-oauth/google'
import { registerUser, getGithubAuthLoginUrl } from '../lib/resources'
import { useAuth } from '../lib/AuthContext'
import { ApiError } from '../lib/api'
import PasswordChecklist from '../components/PasswordChecklist'
import GitHubIcon from '../components/GitHubIcon'
import GoogleIcon from '../components/GoogleIcon'
import {
  validateName,
  validateEmail,
  validatePassword,
  validateConfirmPassword,
  normalizeEmail,
} from '../lib/validation'

export default function Register() {
  const navigate = useNavigate()
  const { loginWithGoogle } = useAuth()
  const [firstName, setFirstName] = useState('')
  const [lastName, setLastName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [password2, setPassword2] = useState('')
  const [passwordFocused, setPasswordFocused] = useState(false)
  const [fieldErrors, setFieldErrors] = useState({})
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [githubBusy, setGithubBusy] = useState(false)
  const [googleBusy, setGoogleBusy] = useState(false)

  // Classic OAuth popup flow (not Google Identity Services' rendered button)
  // so the account chooser always shows - see Login.jsx's identical comment.
  const googleLoginPopup = useGoogleLogin({
    onSuccess: async (tokenResponse) => {
      setError('')
      try {
        const { isAdmin } = await loginWithGoogle(tokenResponse.access_token)
        navigate(isAdmin ? '/admin' : '/dashboard', { replace: true })
      } catch (err) {
        setError(err instanceof ApiError ? err.message : 'Google sign-in failed. Please try again.')
      } finally {
        setGoogleBusy(false)
      }
    },
    onError: () => {
      setError('Google sign-in failed. Please try again.')
      setGoogleBusy(false)
    },
    onNonOAuthError: () => setGoogleBusy(false),
  })

  const handleGoogleLogin = () => {
    setError('')
    setGoogleBusy(true)
    googleLoginPopup()
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

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')

    const errors = {
      firstName: validateName(firstName, 'First name'),
      lastName: validateName(lastName, 'Last name'),
      email: validateEmail(email),
      password: validatePassword(password),
      password2: validateConfirmPassword(password, password2),
    }
    setFieldErrors(errors)
    if (Object.values(errors).some(Boolean)) return

    const cleanEmail = normalizeEmail(email)

    setSubmitting(true)
    try {
      await registerUser(cleanEmail, password, password2)
      sessionStorage.setItem('ca_pending_first_name', firstName.trim())
      sessionStorage.setItem('ca_pending_last_name', lastName.trim())
      // Router state alone strands anyone who reloads /verify-email - see the
      // key's definition there.
      sessionStorage.setItem('ca_pending_email', cleanEmail)
      navigate('/verify-email', { state: { email: cleanEmail } })
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
        <form onSubmit={handleSubmit} style={{ marginTop: 24, display: 'flex', flexDirection: 'column', gap: 12 }} noValidate>
          <input
            type="text"
            placeholder="First name"
            className="field"
            value={firstName}
            onChange={(e) => setFirstName(e.target.value)}
            onBlur={() => setFieldErrors((prev) => ({ ...prev, firstName: validateName(firstName, 'First name') }))}
          />
          {fieldErrors.firstName && <div className="field-error">{fieldErrors.firstName}</div>}

          <input
            type="text"
            placeholder="Last name"
            className="field"
            value={lastName}
            onChange={(e) => setLastName(e.target.value)}
            onBlur={() => setFieldErrors((prev) => ({ ...prev, lastName: validateName(lastName, 'Last name') }))}
          />
          {fieldErrors.lastName && <div className="field-error">{fieldErrors.lastName}</div>}

          <input
            type="email"
            placeholder="Email address"
            className="field"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            onBlur={() => {
              setEmail((prev) => normalizeEmail(prev))
              setFieldErrors((prev) => ({ ...prev, email: validateEmail(email) }))
            }}
          />
          {fieldErrors.email && <div className="field-error">{fieldErrors.email}</div>}

          <input
            type="password"
            placeholder="Password"
            className="field"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onFocus={() => setPasswordFocused(true)}
            onBlur={() => setFieldErrors((prev) => ({ ...prev, password: validatePassword(password) }))}
          />
          {passwordFocused && <PasswordChecklist password={password} />}
          {fieldErrors.password && <div className="field-error">{fieldErrors.password}</div>}

          <input
            type="password"
            placeholder="Confirm password"
            className="field"
            value={password2}
            onChange={(e) => setPassword2(e.target.value)}
            onBlur={() =>
              setFieldErrors((prev) => ({ ...prev, password2: validateConfirmPassword(password, password2) }))
            }
          />
          {fieldErrors.password2 && <div className="field-error">{fieldErrors.password2}</div>}

          {error && <div className="msg-error">{error}</div>}
          <button type="submit" className="btn btn-dark btn-block" style={{ marginTop: 6 }} disabled={submitting}>
            {submitting ? 'Creating account…' : 'Create account'}
          </button>
          <div style={{ fontSize: 12, color: 'var(--color-text-muted)', textAlign: 'center', marginTop: 4 }}>
            We'll send a 6-digit verification code to your email.
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, margin: '4px 0', color: 'var(--color-text-secondary-2)', fontSize: 12 }}>
            <div style={{ flex: 1, height: 1, background: 'var(--color-border)' }} />
            or continue with Google
            <div style={{ flex: 1, height: 1, background: 'var(--color-border)' }} />
          </div>
          <button
            type="button"
            onClick={handleGoogleLogin}
            disabled={googleBusy}
            style={{
              width: 308,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 8,
              padding: '10px 14px',
              borderRadius: 10,
              border: '1px solid var(--color-border-2)',
              background: 'var(--color-surface)',
              color: 'var(--color-text)',
              fontSize: 14,
              fontWeight: 500,
              cursor: 'pointer',
            }}
          >
            <GoogleIcon size={18} />
            {googleBusy ? 'Signing in…' : 'Sign in with Google'}
          </button>
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
          Already have an account?{' '}
          <Link to="/login" style={{ fontWeight: 600 }}>
            Sign in
          </Link>
        </div>
      </div>
    </div>
  )
}
