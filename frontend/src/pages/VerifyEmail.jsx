import { useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { verifyOtp, resendVerification } from '../lib/resources'
import { ApiError } from '../lib/api'
import { validateOtpCode } from '../lib/validation'

export default function VerifyEmail() {
  const location = useLocation()
  const navigate = useNavigate()
  const email = location.state?.email || ''

  const [code, setCode] = useState('')
  const [status, setStatus] = useState('entry') // 'entry' | 'success'
  const [fieldError, setFieldError] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [resendMsg, setResendMsg] = useState(false)
  const [resendBusy, setResendBusy] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')

    const codeError = validateOtpCode(code)
    setFieldError(codeError)
    if (codeError) return

    if (!email) {
      setError('We don’t have your email on this screen — please register or sign in again.')
      return
    }

    setSubmitting(true)
    try {
      await verifyOtp(email, code.trim())
      setStatus('success')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Verification failed. Please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  const handleResend = async () => {
    if (!email) {
      setError('We don’t have your email on this screen — please register or sign in again.')
      return
    }
    setResendBusy(true)
    setError('')
    try {
      await resendVerification(email)
    } finally {
      setResendBusy(false)
      setResendMsg(true)
      setTimeout(() => setResendMsg(false), 2500)
    }
  }

  return (
    <div className="auth-shell dotted-bg">
      <div className="auth-card" style={{ width: 400, textAlign: 'center' }}>
        <div
          style={{
            width: 52,
            height: 52,
            borderRadius: '50%',
            background: 'rgba(143,242,107,0.15)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            margin: '0 auto',
            fontSize: 24,
          }}
        >
          ✉️
        </div>

        {status === 'success' ? (
          <>
            <div style={{ fontFamily: 'var(--font-display)', fontSize: 26, fontWeight: 500, marginTop: 18 }}>
              Email verified
            </div>
            <div style={{ fontSize: 14, color: 'var(--color-text-secondary)', marginTop: 10, lineHeight: 1.5 }}>
              Your account is active. Sign in to continue.
            </div>
            <button className="btn btn-dark btn-block" style={{ marginTop: 22 }} onClick={() => navigate('/login')}>
              Continue to sign in
            </button>
          </>
        ) : (
          <>
            <div style={{ fontFamily: 'var(--font-display)', fontSize: 26, fontWeight: 500, marginTop: 18 }}>
              Verify your email
            </div>
            <div style={{ fontSize: 14, color: 'var(--color-text-secondary)', marginTop: 10, lineHeight: 1.5 }}>
              Enter the 6-digit code we sent to <strong>{email || 'your email'}</strong>. It expires in 10 minutes.
            </div>

            <form onSubmit={handleSubmit} style={{ marginTop: 20 }} noValidate>
              <input
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                placeholder="000000"
                className="field"
                style={{ textAlign: 'center', fontSize: 22, letterSpacing: 6 }}
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                onBlur={() => setFieldError(validateOtpCode(code))}
              />
              {fieldError && <div className="field-error">{fieldError}</div>}
              {resendMsg && <div className="msg-success" style={{ marginTop: 12 }}>Code resent.</div>}
              {error && <div className="msg-error" style={{ marginTop: 12 }}>{error}</div>}

              <button type="submit" className="btn btn-dark btn-block" style={{ marginTop: 16 }} disabled={submitting}>
                {submitting ? 'Verifying…' : 'Verify'}
              </button>
            </form>
            <button
              className="btn btn-outline btn-block"
              style={{ marginTop: 10 }}
              disabled={resendBusy}
              onClick={handleResend}
            >
              Resend code
            </button>
          </>
        )}
      </div>
    </div>
  )
}
