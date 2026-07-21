import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../lib/AuthContext'
import { updateProfile, changePassword, deleteAccount, uploadAvatar } from '../lib/resources'
import { ApiError } from '../lib/api'

export default function Settings() {
  return (
    <div style={{ maxWidth: 640, margin: '0 auto', padding: '44px 40px 100px' }}>
      <div style={{ fontFamily: 'var(--font-display)', fontSize: 32, fontWeight: 500 }}>Settings</div>
      <div style={{ fontSize: 14, color: 'var(--color-text-secondary-2)', marginTop: 4 }}>
        Manage your profile, password, and account.
      </div>

      <AvatarSection />
      <ProfileSection />
      <PasswordSection />
      <DangerSection />
    </div>
  )
}

function Section({ title, children }) {
  return (
    <div className="card" style={{ padding: 24, marginTop: 24 }}>
      <div style={{ fontWeight: 600, fontSize: 15, marginBottom: 16 }}>{title}</div>
      {children}
    </div>
  )
}

function AvatarSection() {
  const { user, setUser } = useAuth()
  const fileInputRef = useRef(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const handlePick = async (e) => {
    const file = e.target.files[0]
    if (!file) return
    setBusy(true)
    setError('')
    try {
      const data = await uploadAvatar(file)
      setUser((prev) => ({ ...prev, avatar: data.avatar }))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not upload avatar.')
    } finally {
      setBusy(false)
    }
  }

  const initial = (user?.first_name || user?.username || 'U').charAt(0).toUpperCase()

  return (
    <Section title="Avatar">
      <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
        {user?.avatar ? (
          <img src={user.avatar} alt="Avatar" style={{ width: 56, height: 56, borderRadius: '50%', objectFit: 'cover' }} />
        ) : (
          <div
            style={{
              width: 56,
              height: 56,
              borderRadius: '50%',
              background: 'var(--color-accent)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontFamily: 'var(--font-mono)',
              fontSize: 20,
              fontWeight: 700,
            }}
          >
            {initial}
          </div>
        )}
        <button className="btn btn-outline" style={{ padding: '9px 16px', fontSize: 13 }} disabled={busy} onClick={() => fileInputRef.current?.click()}>
          {busy ? 'Uploading…' : 'Change avatar'}
        </button>
        <input ref={fileInputRef} type="file" accept="image/*" style={{ display: 'none' }} onChange={handlePick} />
      </div>
      {error && <div className="msg-error" style={{ marginTop: 12 }}>{error}</div>}
    </Section>
  )
}

function ProfileSection() {
  const { user, setUser } = useAuth()
  const [firstName, setFirstName] = useState(user?.first_name || '')
  const [lastName, setLastName] = useState(user?.last_name || '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    setSaved(false)
    try {
      const profile = await updateProfile({ first_name: firstName, last_name: lastName })
      setUser((prev) => ({ ...prev, ...profile }))
      setSaved(true)
      setTimeout(() => setSaved(false), 2500)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not save profile.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Section title="Profile">
      <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <input className="field" placeholder="First name" value={firstName} onChange={(e) => setFirstName(e.target.value)} />
        <input className="field" placeholder="Last name" value={lastName} onChange={(e) => setLastName(e.target.value)} />
        <div style={{ fontSize: 13, color: 'var(--color-text-secondary-2)' }}>{user?.email}</div>
        {saved && <div className="msg-success">Saved.</div>}
        {error && <div className="msg-error">{error}</div>}
        <button className="btn btn-dark" style={{ width: 'fit-content', padding: '10px 20px', fontSize: 13 }} disabled={busy}>
          {busy ? 'Saving…' : 'Save changes'}
        </button>
      </form>
    </Section>
  )
}

function PasswordSection() {
  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newPassword2, setNewPassword2] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setDone(false)
    if (newPassword !== newPassword2) {
      setError('New passwords do not match.')
      return
    }
    setBusy(true)
    try {
      await changePassword(oldPassword, newPassword, newPassword2)
      setOldPassword('')
      setNewPassword('')
      setNewPassword2('')
      setDone(true)
      setTimeout(() => setDone(false), 2500)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not change password.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Section title="Change password">
      <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <input
          type="password"
          className="field"
          placeholder="Current password"
          value={oldPassword}
          onChange={(e) => setOldPassword(e.target.value)}
          required
        />
        <input
          type="password"
          className="field"
          placeholder="New password"
          value={newPassword}
          onChange={(e) => setNewPassword(e.target.value)}
          required
        />
        <input
          type="password"
          className="field"
          placeholder="Confirm new password"
          value={newPassword2}
          onChange={(e) => setNewPassword2(e.target.value)}
          required
        />
        {done && <div className="msg-success">Password changed.</div>}
        {error && <div className="msg-error">{error}</div>}
        <button className="btn btn-dark" style={{ width: 'fit-content', padding: '10px 20px', fontSize: 13 }} disabled={busy}>
          {busy ? 'Updating…' : 'Update password'}
        </button>
      </form>
    </Section>
  )
}

function DangerSection() {
  const { logout } = useAuth()
  const navigate = useNavigate()
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const handleDelete = async (e) => {
    e.preventDefault()
    if (!window.confirm('Delete your account permanently? This cannot be undone.')) return
    setBusy(true)
    setError('')
    try {
      await deleteAccount(password)
      await logout()
      navigate('/')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not delete account.')
      setBusy(false)
    }
  }

  return (
    <Section title="Danger zone">
      <form onSubmit={handleDelete} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div style={{ fontSize: 13, color: 'var(--color-text-secondary)' }}>
          Deleting your account removes all of your analyses and cannot be undone.
        </div>
        <input
          type="password"
          className="field"
          placeholder="Confirm your password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        {error && <div className="msg-error">{error}</div>}
        <button
          className="btn"
          style={{ width: 'fit-content', padding: '10px 20px', fontSize: 13, background: 'var(--color-danger)', color: '#fff' }}
          disabled={busy}
        >
          {busy ? 'Deleting…' : 'Delete account'}
        </button>
      </form>
    </Section>
  )
}
