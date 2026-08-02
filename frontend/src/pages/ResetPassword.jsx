import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { resetPassword } from '../lib/resources'
import { ApiError } from '../lib/api'
import PasswordChecklist from '../components/PasswordChecklist'
import { validatePassword, validateConfirmPassword } from '../lib/validation'

export default function ResetPassword() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const uid = searchParams.get('uid')
  const token = searchParams.get('token')

  const [password, setPassword] = useState('')
  const [password2, setPassword2] = useState('')
  const [passwordFocused, setPasswordFocused] = useState(false)
  const [fieldErrors, setFieldErrors] = useState({})
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    if (!uid || !token) {
      setError('This reset link is missing information. Please request a new one.')
      return
    }

    const errors = {
      password: validatePassword(password),
      password2: validateConfirmPassword(password, password2),
    }
    setFieldErrors(errors)
    if (Object.values(errors).some(Boolean)) return

    setSubmitting(true)
    try {
      await resetPassword(uid, token, password, password2)
      setDone(true)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong. Please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="auth-shell dotted-bg">
      <div className="auth-card">
        <div style={{ fontFamily: 'var(--font-display)', fontSize: 30, fontWeight: 500 }}>Reset your password</div>

        {done ? (
          <>
            <div className="msg-success" style={{ marginTop: 20 }}>Password has been reset successfully.</div>
            <button className="btn btn-dark btn-block" style={{ marginTop: 16 }} onClick={() => navigate('/login')}>
              Continue to sign in
            </button>
          </>
        ) : (
          <form onSubmit={handleSubmit} style={{ marginTop: 24, display: 'flex', flexDirection: 'column', gap: 12 }} noValidate>
            <input
              type="password"
              placeholder="New password"
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
              placeholder="Confirm new password"
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
              {submitting ? 'Resetting…' : 'Reset password'}
            </button>
          </form>
        )}
      </div>
    </div>
  )
}
