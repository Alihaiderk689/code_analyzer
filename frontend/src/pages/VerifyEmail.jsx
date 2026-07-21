import { useEffect, useState } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { verifyEmail, resendVerification } from '../lib/resources'
import { ApiError } from '../lib/api'

export default function VerifyEmail() {
  const [searchParams] = useSearchParams()
  const location = useLocation()
  const navigate = useNavigate()

  const uid = searchParams.get('uid')
  const token = searchParams.get('token')
  const email = location.state?.email || ''

  const [status, setStatus] = useState(uid && token ? 'verifying' : 'pending')
  const [errorMsg, setErrorMsg] = useState('')
  const [resendMsg, setResendMsg] = useState(false)
  const [resendBusy, setResendBusy] = useState(false)

  useEffect(() => {
    if (!uid || !token) return
    verifyEmail(uid, token)
      .then(() => setStatus('success'))
      .catch((err) => {
        setStatus('error')
        setErrorMsg(err instanceof ApiError ? err.message : 'Verification link is invalid or has expired.')
      })
  }, [uid, token])

  const handleResend = async () => {
    if (!email) {
      setErrorMsg('We don’t have your email on this screen — please sign in again to resend it.')
      return
    }
    setResendBusy(true)
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

        {status === 'verifying' && (
          <>
            <div style={{ fontFamily: 'var(--font-display)', fontSize: 26, fontWeight: 500, marginTop: 18 }}>
              Verifying your email…
            </div>
          </>
        )}

        {status === 'success' && (
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
        )}

        {status === 'error' && (
          <>
            <div style={{ fontFamily: 'var(--font-display)', fontSize: 26, fontWeight: 500, marginTop: 18 }}>
              Verification failed
            </div>
            <div className="msg-error" style={{ marginTop: 16 }}>
              {errorMsg}
            </div>
            <button
              className="btn btn-outline btn-block"
              style={{ marginTop: 16 }}
              disabled={resendBusy}
              onClick={handleResend}
            >
              Resend email
            </button>
          </>
        )}

        {status === 'pending' && (
          <>
            <div style={{ fontFamily: 'var(--font-display)', fontSize: 26, fontWeight: 500, marginTop: 18 }}>
              Verify your email
            </div>
            <div style={{ fontSize: 14, color: 'var(--color-text-secondary)', marginTop: 10, lineHeight: 1.5 }}>
              We've sent a verification link to <strong>{email || 'your email'}</strong>. Click the link to activate
              your account.
            </div>

            {resendMsg && <div className="msg-success" style={{ marginTop: 16 }}>Email resent.</div>}
            {errorMsg && <div className="msg-error" style={{ marginTop: 16 }}>{errorMsg}</div>}

            <button className="btn btn-dark btn-block" style={{ marginTop: 22 }} onClick={() => navigate('/login')}>
              I've verified — continue
            </button>
            <button
              className="btn btn-outline btn-block"
              style={{ marginTop: 10 }}
              disabled={resendBusy}
              onClick={handleResend}
            >
              Resend email
            </button>
          </>
        )}
      </div>
    </div>
  )
}
